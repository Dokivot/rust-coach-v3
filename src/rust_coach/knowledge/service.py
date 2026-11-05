import json
import re
import sqlite3
from time import perf_counter

from rust_coach.calculations.catalog import RuleCatalog
from rust_coach.calculations.requests import RAID_TARGETS
from rust_coach.contracts import AppError
from rust_coach.knowledge.bundle import SNAPSHOT, default_release
from rust_coach.knowledge.registry import KnowledgeRegistry, digest


LIMITATION = "依据固定演示快照 rustdata-pc-vanilla-2026-11-01；游戏 build 未认证，也没有独立人工复核，不能保证适用于最新补丁或修改服。"
ALIASES = {"explosive.timed": ("c4", "定时炸弹", "定时爆炸物", "timed explosive"),
           "explosives": ("炸药", "explosives"), "gunpowder": ("火药", "gunpowder", "gun powder"),
           "explosive.satchel": ("炸药包", "satchel charge"), "ammo.rocket.basic": ("火箭", "rocket")}
CLAIM_TERMS = {
    "rounding": (r"取整|为什么.*(?:一颗|一发|两颗|250|数量)|ceil|round", "rounding ceil health damage"),
    "targets": (r"多个|多面|两面|合并血|multiple|pool", "multiple targets health"),
    "optimization": (r"最省|最优|混搭|比较|optimal|cheapest", "optimization sulfur single method"),
    "inventory": (r"抵扣|已有|库存|截图|inventory|screenshot", "inventory deduction partial"),
    "technology": (r"工作台|蓝图|几级|workbench|blueprint", "technology workbench blueprint"),
    "batch": (r"批次|每批|产出|batch|yield", "batch yield recipe"),
    "mixing": (r"混合台|搅拌台|mixing", "mixing gunpowder recipe"),
    "fuel": (r"燃油|fuel", "fuel expansion ingredients"),
    "version": (r"版本|快照|更新|日期|来源|依据|version|snapshot|source", "version snapshot provenance"),
    "unknown_hp": (r"受损|残血|不知.*血|unknown.*health", "unknown damaged health"),
    "conditions": (r"硬面|软面|命中|溅射|维修|朝向|伤害条件|hard|soft|splash", "conditions side damage"),
    "feedback": (r"资料.*(?:冲突|不一样)|攻略.*(?:不同|不一样)|反馈|引用.*支持|conflict", "conflict feedback evidence"),
}


class LocalKnowledge:
    def __init__(self, store):
        self.catalog = RuleCatalog()
        self.aliases = {identifier.lower(): identifier for identifier in self.catalog.recipes}
        self.aliases.update({self.catalog.items[identifier]["name"].lower(): identifier for identifier in self.catalog.recipes})
        self.aliases.update({alias: identifier for identifier, aliases in ALIASES.items() for alias in aliases})
        self.aliases.update(RAID_TARGETS)
        self.entity_pattern = re.compile("|".join(
            (r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])") if alias.isascii() else re.escape(alias)
            for alias in sorted(self.aliases, key=len, reverse=True)), re.I)
        self.registry = KnowledgeRegistry(store)
        self.registry.install(default_release(), self.catalog.sha256)
        self.reference()

    def reference(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.registry.snapshot()
        if snapshot["release"]["rules_sha256"] != self.catalog.sha256:
            raise AppError("knowledge_rules_mismatch", "当前启用知识包与计算规则不一致，请重新发布匹配的演示包。", 409)
        return {key: snapshot[key] for key in ("release_id", "release_sha256", "registry_revision")}

    def need(self, message, previous=None):
        lowered = message.lower()
        entities = [self.aliases[match[0].lower()] for match in self.entity_pattern.finditer(lowered)]
        entities = list(dict.fromkeys(entities))
        if previous and re.search(r"它|这个|刚才|为什么|解释|依据|来源", message):
            previous_input = previous.get("input") or previous
            if not entities:
                entities.extend(previous_input.get("goals", {}))
            target = previous_input.get("target")
            if target and not any(entity in self.catalog.targets for entity in entities):
                entities.append(target)
        claim_text = self.entity_pattern.sub(" ", message)
        claims = [claim for claim, (pattern, _) in CLAIM_TERMS.items() if re.search(pattern, claim_text, re.I)]
        if entities and re.search(r"配方|材料|recipe|ingredients", message, re.I):
            claims.append("recipe")
        if entities and re.search(r"满血|生命值|血量.*多少|多少.*血|伤害.*多少|多少.*伤害|\bhp\b|\bhealth\b|\bdamage\b", message, re.I):
            claims.append("raid")
        scope = "snapshot"
        if re.search(r"修改服|倍率服|主机版|手游|staging|测试分支|预发布|旧版本|老版本|以前版本|console|modded|mobile|old version", message, re.I):
            scope = "scope_mismatch"
        elif re.search(r"最新|当前版本|现在版本|这次更新|本次补丁|latest|current (?:patch|version)", message, re.I):
            scope = "latest_unverified"
        date_pattern = r"(?<!\d)(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})(?:日)?(?!\d)"
        dates = re.findall(date_pattern, message)
        if dates and any(tuple(map(int, date)) != (2026, 11, 1) for date in dates):
            scope = "scope_mismatch"
        remaining = re.sub(date_pattern, "", message)
        if re.search(r"20\d{2}(?:年|[-/.]\d{1,2})|\d{1,2}月|\b\d{1,2}/\d{1,2}\b|"
                     r"(?:版本|补丁|patch)\s*[vV]?\d+|\bv\d+", remaining, re.I):
            scope = "scope_mismatch"
        if re.search(r"\bbuild\s*[:#号]?\s*\d+|构建(?:号|版本)?\s*[:：]?\s*\d+", message, re.I):
            scope = "scope_mismatch"
        if re.search(r"伤害翻倍|必定成功|自动扣|保证.*最新|guarantee", message, re.I):
            claims = ["unsupported_claim"]
        return {"entities": entities, "claims": claims, "scope": scope,
                "query_terms": " ".join([*entities, *(CLAIM_TERMS[claim][1] for claim in claims if claim in CLAIM_TERMS)])}

    def handles(self, message, previous=None):
        if message.startswith("/calc"):
            return False
        if message.startswith("/knowledge"):
            return True
        need = self.need(message, previous)
        if need["scope"] != "snapshot" and (need["entities"] or re.search(r"Rust|游戏|配方|拆墙|版本|补丁", message, re.I)):
            return True
        return bool((need["entities"] or previous) and re.search(
            r"为什么|解释|原理|依据|来源|配方|满血|生命值|血量.*多少|多少.*血|伤害.*多少|多少.*伤害|"
            r"几级工作台|需要.*工作台|蓝图.*条件|why|explain|recipe|source|\bhp\b|\bhealth\b|\bdamage\b", message, re.I))

    def eligible(self, snapshot):
        accepted, excluded = [], []
        for document in snapshot["release"]["documents"]:
            reason = None
            if document["id"] in snapshot["revoked"]:
                reason = "revoked"
            elif document["snapshot"] != SNAPSHOT or document["platform"] != "pc" or document["ruleset"] != "pc_vanilla" or document["branch"] != "demo_snapshot":
                reason = "scope_mismatch"
            elif document["admission"] != "snapshot_checked":
                reason = document["admission"]
            if reason:
                excluded.append({"id": document["id"], "reason": reason})
            else:
                accepted.append(document)
        return accepted, excluded

    def retrieve(self, message, previous=None, *, max_chars=6000):
        started = perf_counter()
        snapshot = self.registry.snapshot()
        self.reference(snapshot)
        need = self.need(message, previous)
        accepted, excluded = self.eligible(snapshot)
        reference = {key: snapshot[key] for key in ("release_id", "release_sha256", "registry_revision")}
        result = {**reference, "snapshot": SNAPSHOT, "need": need, "documents": [], "excluded": excluded,
                  "missing": [], "status": "supported_snapshot", "cache_hit": False, "strategy": "exact+eligible_fts",
                  "limitations": [LIMITATION], "model_calls": 0, "network_calls": 0, "rule_dependencies": []}
        if need["scope"] != "snapshot":
            return {**result, "status": need["scope"], "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
        required = []
        required_claims = [claim for claim in need["claims"] if claim in CLAIM_TERMS]
        for entity in need["entities"]:
            required.append(("raid:" if entity in self.catalog.targets else "recipe:") + entity)
        if "mixing" in required_claims:
            required.append("recipe:mix")
        if "raid" in need["claims"] and not any(entity in self.catalog.targets for entity in need["entities"]):
            return {**result, "status": "missing_evidence", "missing": ["伤害或生命值对应的具体墙门目标"], "elapsed_ms": 0}
        if not need["claims"] or "unsupported_claim" in need["claims"]:
            return {**result, "status": "missing_evidence", "missing": ["明确需要解释的配方、目标或机制"], "elapsed_ms": 0}
        key = digest({**reference, "need": need, "max_chars": max_chars, "strategy_version": "local-fts-v3"})
        documents = {document["id"]: document for document in accepted}
        ranked = []
        with self.registry.store.connection() as connection:
            if required_claims:
                try:
                    self.registry.validate_index(snapshot, connection)
                except sqlite3.DatabaseError:
                    return {**result, "status": "index_unavailable", "missing": ["本地全文索引缺失或不完整，请重建索引"],
                            "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
            cached = None
            try:
                row = connection.execute("SELECT payload FROM knowledge_queries WHERE cache_key=?", (key,)).fetchone()
                if row:
                    cached = json.loads(row["payload"])
                    if not isinstance(cached, list) or any(not isinstance(identifier, str) or identifier not in documents for identifier in cached):
                        raise ValueError("invalid_query_cache")
                    covered_claims = {claim for identifier in cached if documents[identifier]["kind"] == "explanation"
                                      for claim in documents[identifier]["claims"]}
                    if set(required_claims) - covered_claims:
                        raise ValueError("incomplete_query_cache")
            except (ValueError, sqlite3.DatabaseError):
                cached = None
                result["cache_warning"] = "缓存不可用，已重新检索有效证据"
            if cached is not None:
                ranked = cached
                result["cache_hit"] = True
            else:
                tokens = list(dict.fromkeys(re.findall(r"[a-zA-Z_]{2,}", need["query_terms"])))[:30]
                if tokens and documents and required_claims:
                    match = " OR ".join('"' + token + '"' for token in tokens)
                    try:
                        placeholders = ",".join("?" for identifier in documents)
                        rows = connection.execute(
                            "SELECT unit_id FROM knowledge_fts WHERE knowledge_fts MATCH ? AND release_id=? "
                            f"AND unit_id IN ({placeholders}) ORDER BY bm25(knowledge_fts), unit_id",
                            (match, snapshot["release_id"], *documents)).fetchall()
                    except sqlite3.OperationalError:
                        return {**result, "status": "index_unavailable", "missing": ["本地全文索引不可用"], "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
                    ranked = [row["unit_id"] for row in rows if row["unit_id"] in documents]
                try:
                    connection.execute("INSERT OR REPLACE INTO knowledge_queries VALUES (?,?)", (key, json.dumps(ranked)))
                except sqlite3.DatabaseError:
                    result["cache_warning"] = "缓存不可用，本次结果未缓存"
        used = 0
        covered = set()
        ordered = list(dict.fromkeys([*(identifier for identifier in required if identifier in documents), *ranked]))
        for identifier in ordered:
            document = documents[identifier]
            claims = set(document["claims"]) & set(required_claims)
            explanatory = document["kind"] == "explanation" and claims - covered
            crosscheck = (document["kind"] == "captured_excerpt" and "version" in required_claims
                          and set(document["entities"]) & set(need["entities"]))
            if identifier not in required and not explanatory and not crosscheck:
                continue
            if len(result["documents"]) >= 5 or used + len(document["text"]) > max_chars:
                continue
            result["documents"].append(document)
            used += len(document["text"])
            if document["kind"] == "explanation":
                covered.update(claims)
        selected = {document["id"] for document in result["documents"]}
        result["missing"] = [identifier for identifier in required if identifier not in selected]
        result["missing"].extend("claim:" + claim for claim in required_claims if claim not in covered)
        if not selected or result["missing"]:
            result["status"] = "missing_evidence"
        if result["status"] == "supported_snapshot" and "rounding" in required_claims:
            try:
                derivation = self.derive_rounding(message, need, previous)
                if derivation:
                    result["derivation"] = derivation
                    result["rule_dependencies"] = derivation["knowledge"]["rule_dependencies"]
            except (AppError, ValueError) as error:
                result.update(status="missing_evidence", missing=[getattr(error, "message", "无法可靠确定解释中的目标或血量")])
        result.update(context_chars=used, elapsed_ms=round((perf_counter() - started) * 1000, 3))
        self.registry.validate(result)
        return result

    def derive_rounding(self, message, need, previous):
        from rust_coach.calculations.engine import Calculator
        from rust_coach.calculations.schemas import CalculationRequest

        prior = (previous.get("input") or previous) if previous else {}
        targets = [entity for entity in need["entities"] if entity in self.catalog.targets]
        health_values = re.findall(r"([零〇一二两三四五六七八九十百千万\d.+-]+)\s*(?:点血|血|HP\b)", message, re.I)
        if len(health_values) > 1 or any(not value.isascii() or not value.isdigit() for value in health_values):
            raise ValueError("ambiguous_health_for_explanation")
        health = int(health_values[0]) if health_values else None
        if len(targets) != 1 or "targets" in need["claims"]:
            return None
        target = targets[0]
        same_target = prior.get("target") == target
        if health is None and not (same_target and prior.get("hp") is not None):
            return None
        methods = [entity for entity in need["entities"] if entity in self.catalog.items]
        counts = re.findall(r"([零〇一二两三四五六七八九十百千万\d.+-]+)\s*(?:面|扇)(?:墙|门|石墙|铁墙|木墙|装甲墙|金属墙)?", message)
        if len(counts) > 1 or any(not value.isascii() or not value.isdigit() for value in counts):
            raise ValueError("ambiguous_target_count_for_explanation")
        side = prior.get("side", "hard") if same_target else "hard"
        if re.search(r"软面|\bsoft\b", message, re.I):
            side = "soft"
        elif re.search(r"硬面|\bhard\b", message, re.I):
            side = "hard"
        payload = {"operation": "raid", "target": target,
            "hp": health if health is not None else prior["hp"],
            "methods": methods or (prior.get("methods", []) if same_target else []),
            "count": int(counts[0]) if counts else prior.get("count", 1) if same_target else 1, "side": side}
        return Calculator(self).calculate(CalculationRequest.model_validate(payload))

    def calculation_evidence(self, request):
        snapshot = self.registry.snapshot()
        self.reference(snapshot)
        accepted, excluded = self.eligible(snapshot)
        if request.operation == "craft":
            dependencies = ["recipe:" + item for item in self.catalog.order(request.goals, request.expand_fuel or "lowgradefuel" in request.goals)
                            if self.catalog.recipe(item, request.expand_fuel or "lowgradefuel" in request.goals)]
        else:
            dependencies = ["raid:" + str(request.target)]
            target = self.catalog.targets.get(request.target, {})
            methods = request.methods or list(target.get("methods", {}))
            dependencies.extend("recipe:" + item for item in self.catalog.order(methods, request.expand_fuel)
                                if self.catalog.recipe(item, request.expand_fuel))
        if request.gunpowder_recipe == "mixing_table":
            dependencies = ["recipe:mix" if identifier == "recipe:gunpowder" else identifier for identifier in dependencies]
        available = {document["id"]: document for document in accepted}
        missing = [identifier for identifier in dependencies if identifier not in available]
        if missing:
            raise AppError("knowledge_evidence_unavailable", "本次计算依赖的知识已撤回、冲突或缺失：" + "、".join(missing), 422)
        return {**{key: snapshot[key] for key in ("release_id", "release_sha256", "registry_revision")},
                "snapshot": SNAPSHOT, "status": "supported_snapshot", "rule_dependencies": list(dict.fromkeys(dependencies)),
                "documents": [], "excluded": excluded, "limitations": [LIMITATION], "verification": "snapshot_only"}

    def render(self, result):
        if result["status"] == "latest_unverified":
            return "当前采用固定演示快照，无法认证最新补丁是否相同。可以继续按该快照计算，但不会把旧数值说成当前正式服规则。\n\n" + LIMITATION
        if result["status"] == "scope_mismatch":
            return "该问题指定的版本、平台或服务器规则不属于当前演示包，未采用其中的数值。请改为询问本演示快照，或另行核对目标版本。\n\n" + LIMITATION
        if result["status"] == "index_unavailable":
            return "本地知识索引不可用，本轮没有生成机制解释；请恢复索引后重试。已有规则计算服务需分别检查其依赖。"
        if result["status"] == "missing_evidence":
            return "当前演示包没有足够的有效证据支持完整结论，暂不补猜。缺少：" + "、".join(result["missing"]) + "。\n\n" + LIMITATION
        body = "\n\n".join(document["text"] for document in result["documents"])
        if result.get("derivation"):
            calculated = result["derivation"]["result"]
            formulas = "；".join(self.catalog.items[option["item"]]["name"] + "：" + option["formula"] for option in calculated["options"])
            body = (f"按{calculated['target_count']}个{calculated['target_name']}、每个{calculated['hp_each']}HP、"
                    f"{calculated['side']}面，确定性计算为：{formulas}。数量以此公式为准；这不证明当前库存足够。\n\n" + body)
        return "以下是演示包内能支持的已收录信息，未覆盖的主张不作推断：\n\n" + body + "\n\n" + LIMITATION
