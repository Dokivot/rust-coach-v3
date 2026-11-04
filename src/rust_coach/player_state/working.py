import copy
import re
from datetime import datetime, timedelta, timezone

from rust_coach.player_state.domain import project
from rust_coach.player_state.schemas import FIELD_DEFINITIONS


def current_assertion(message):
    return not re.search(r"[？?\"“”‘’]|吗[。！!]*$|如果|假如|假设|万一|要是|假装|举例|比如|昨天|前天|上次|曾经|以前|并非|不是|没有死|没死|就好了|希望|但愿|可能|也许", message)


def explicit_event(message):
    if re.search(r"[\"“”‘’]|如果|假如|假设|万一|要是|假装|举例|比如|昨天|前天|上次|曾经|以前|"
                 r"说错|口误|不对|并非|不是|没(?:有)?(?:死|丢|掉|被抢|被抄)|背包还在", message):
        return None
    for clause in re.split(r"[，,。；;\n]", message):
        clause = clause.strip()
        if not current_assertion(clause):
            continue
        if re.fullmatch(r"我(?:刚刚|刚|现在|已经)?(?:换服了|换服务器了|新开档了)[！!]*", clause):
            return "scope_changed"
        if re.match(r"(?:我(?:刚刚|刚|现在|已经)?|刚刚|刚)(?:死了|死亡了|阵亡了|被杀了)(?:$|[！!\s])", clause):
            return "death"
        if re.match(r"我的?(?:背包|包)(?:刚刚|刚|已经)?(?:丢了|掉了|被抢了|没了)(?:$|[！!\s])", clause):
            return "backpack_lost"
        if re.match(r"(?:我的基地|我家)(?:刚刚|刚|已经)?(?:被抄家了|被拆了|被抄了)(?:$|[！!\s])", clause):
            return "raided"
    return None


def text_facts(message):
    facts = {}
    if re.search(r"[\"“”‘’]|如果|假如|假设|万一|要是|假装|举例|比如|昨天|前天|上次|曾经|以前", message):
        return facts
    message = re.sub(r"^/(?:coach|search)\s+", "", message.strip())
    for clause in re.split(r"[，,。；;\n]", message):
        clause = clause.strip()
        if not current_assertion(clause):
            continue
        if re.match(r"(?:我)?(?:现在)?(?:已经有|已有|有了|有|建好了)(?:一个)?基地", clause):
            facts["base.has_base"] = True
        if re.search(r"^(?:我)?(?:现在|目前)?(?:没有|还没建)基地", clause):
            facts["base.has_base"] = False
        workbench = re.match(r"(?:我)?(?:现在)?(?:已有|已经有|有)([0-3一二三零])级工作台", clause)
        if workbench:
            level = workbench[1]
            facts["technology.workbench"] = int(level) if level.isascii() else "零一二三".index(level)
        minutes = re.match(r"(?:我)?(?:现在)?(?:还(?:能|可以)在线|还剩|剩余在线)(\d{1,5})分钟", clause)
        if minutes:
            facts["session.online_minutes"] = int(minutes[1])
    return facts


def effective_state(snapshot, message, previous=None, now=None):
    now = now or datetime.now(timezone.utc)
    persistent = project(snapshot, now) if snapshot else None
    scope_id = snapshot["scope"]["id"] if snapshot else None
    records = {}
    conflicts = []
    if previous and previous.get("scope_id") == scope_id:
        for key, record in previous.get("temporary_facts", {}).items():
            invalidated_at = (snapshot or {}).get("invalidations", {}).get(key)
            newer = (snapshot or {}).get("facts", {}).get(key)
            if (datetime.fromisoformat(record["expires_at"]) > now and
                    (not invalidated_at or datetime.fromisoformat(record["observed_at"]) > datetime.fromisoformat(invalidated_at)) and
                    (not newer or datetime.fromisoformat(newer["observed_at"]) <= datetime.fromisoformat(record["observed_at"]))):
                records[key] = record
    for key, value in text_facts(message).items():
        records[key] = {"value": value, "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=FIELD_DEFINITIONS[key][3])).isoformat(),
            "source": "current_text", "verification": "user_reported", "status": "fresh"}
    effective = copy.deepcopy(persistent) if persistent else {
        "scope": {"id": None, "ruleset": "pc_vanilla", "label": "本轮临时状态"},
        "revision": 0, "facts": {}, "containers": {}, "evaluated_at": now.isoformat()}
    for key, record in records.items():
        existing = effective["facts"].get(key)
        if existing and existing["status"] == "fresh" and existing["value"] != record["value"]:
            conflicts.append({"field": key, "profile_value": existing["value"], "text_value": record["value"],
                              "resolution": "current_text_for_this_task_only"})
        effective["facts"][key] = record
    effective = project(effective, now)
    view = {"scope_id": scope_id, "revision": snapshot["revision"] if snapshot else None,
            "temporary_facts": records, "conflicts": conflicts,
            "facts": {key: {"value": record["value"], "source": "text" if key in records else "profile",
                             "observed_at": record["observed_at"], "status": record["status"]}
                      for key, record in effective["facts"].items()},
            "inventory_policy": "one_source_with_provenance", "persisted": False}
    return effective if snapshot or records else None, view
