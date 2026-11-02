import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from rust_coach.contracts import AppError
from rust_coach.memory.schemas import CONCEPTS, LABELS, VALUES


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self, conversations):
        self.conversations = conversations
        with conversations.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS coach_profiles (player_id TEXT PRIMARY KEY, data TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS coach_memory_events (id TEXT PRIMARY KEY, player_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, memory_key TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at TEXT NOT NULL, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, data TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS coach_memory_scope ON coach_memory_events(player_id, scope, status, created_at);"
            )
            existing = connection.execute("SELECT 1 FROM coach_profiles WHERE player_id='local'").fetchone()
            if not existing:
                self.save(connection, {"player_id": "local", "revision": 0, "epoch": 0, "enabled": True,
                                       "history_floor": "", "items": {}})

    def profile(self, connection=None):
        if connection is None:
            with self.conversations.connection() as connection:
                return self.profile(connection)
        return json.loads(connection.execute("SELECT data FROM coach_profiles WHERE player_id='local'").fetchone()["data"])

    def save(self, connection, profile):
        connection.execute("INSERT INTO coach_profiles(player_id,data) VALUES ('local',?) "
                           "ON CONFLICT(player_id) DO UPDATE SET data=excluded.data", (json.dumps(profile, ensure_ascii=False),))

    def apply(self, connection, candidates, source, *, expected_epoch, origin="deterministic"):
        profile = self.profile(connection)
        if profile["epoch"] != expected_epoch or not profile["enabled"]:
            return []
        applied = []
        for candidate in candidates:
            key = "learning." + candidate.key if candidate.kind == "learning" else candidate.key
            identifier = str(uuid5(NAMESPACE_URL, ":".join([source["conversation_id"], source["request_id"],
                candidate.kind, candidate.key, candidate.signal, candidate.quote])))
            if connection.execute("SELECT 1 FROM coach_memory_events WHERE id=?", (identifier,)).fetchone():
                continue
            previous = profile["items"].get(key)
            older = previous and previous["updated_at"] > source["created_at"]
            if older and candidate.kind != "learning":
                continue
            scope = (source.get("scope_id") or "unscoped") if candidate.kind == "event" else "global"
            data = {"value": candidate.quote if candidate.kind == "event" or candidate.key in {"schedule", "routine"} else candidate.value,
                    "quote": candidate.quote, "topics": source.get("concepts", []) or (["base_entry"] if candidate.key == "raided" else []), "signal": candidate.signal,
                    "origin": origin, "verification": "discussion_record" if candidate.signal == "discussed" else
                        "model_assessed" if candidate.signal == "demonstrated" else "user_reported",
                    "source": {key: source[key] for key in ("conversation_id", "request_id", "created_at")}}
            connection.execute("INSERT INTO coach_memory_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, "local", candidate.kind, key, scope, "active", source["created_at"],
                 source["conversation_id"], source["request_id"], json.dumps(data, ensure_ascii=False)))
            if candidate.kind != "event":
                value = data["value"]
                if candidate.kind == "learning":
                    value = dict(previous["value"]) if previous else {"demonstrations": []}
                    if candidate.signal == "demonstrated":
                        digest = hashlib.sha256(candidate.quote.encode()).hexdigest()
                        demonstrations = value["demonstrations"]
                        lesson_id = source.get("lesson_id", source["request_id"])
                        if not any(item["digest"] == digest or item.get("lesson_id") == lesson_id for item in demonstrations):
                            demonstrations.append({"digest": digest, "request_id": source["request_id"], "lesson_id": lesson_id, "event_id": identifier})
                        value["demonstrations"] = demonstrations[-3:]
                    if candidate.signal == "discussed":
                        if not older:
                            value["last_discussed"] = data["source"]
                        value.setdefault("last_signal", "discussed")
                    elif source["created_at"] >= value.get("last_evidence_at", ""):
                        value["last_signal"] = candidate.signal
                        value["last_evidence_at"] = source["created_at"]
                updated_at = previous["updated_at"] if older else source["created_at"]
                profile["items"][key] = {"kind": candidate.kind, "value": value, "event_id": identifier,
                    "updated_at": updated_at, "source": data["source"], "verification": data["verification"],
                    "expires_at": None if candidate.kind == "preference" else
                        (datetime.fromisoformat(updated_at) + timedelta(days=90)).isoformat()}
            applied.append(identifier)
        if applied:
            profile["revision"] += 1
            self.save(connection, profile)
        return applied

    def active_profile(self):
        profile = self.profile()
        if not profile["enabled"]:
            return {**profile, "items": {}}
        now = now_iso()
        return {**profile, "items": {key: item for key, item in profile["items"].items()
            if not item["expires_at"] or item["expires_at"] > now}}

    def history_exclusions(self, conversation_id):
        with self.conversations.connection() as connection:
            rows = connection.execute("SELECT DISTINCT request_id FROM coach_memory_events WHERE conversation_id=? AND status='forgotten'",
                                      (conversation_id,)).fetchall()
            forgotten = {row["id"] for row in connection.execute("SELECT id FROM coach_memory_events WHERE status='forgotten'")}
            turns = connection.execute("SELECT request_id,memory FROM turns WHERE conversation_id=? AND memory IS NOT NULL", (conversation_id,)).fetchall()
        excluded = {row["request_id"] for row in rows}
        for turn in turns:
            memory = json.loads(turn["memory"])
            references = set(memory.get("used_event_ids", [])) | set(memory.get("used_profile_refs", {}).values())
            if memory.get("control") or references.intersection(forgotten):
                excluded.add(turn["request_id"])
        return excluded

    def context(self, message, conversation_id, scope_id, concepts, *, limit_chars=3200):
        profile = self.active_profile()
        context = {"revision": profile["revision"], "preferences": {}, "habits": {}, "learning": {}, "events": [],
                   "policy": "历史仅供个性化与复盘，不是当前库存、已执行动作或游戏规则；当前明确要求优先。"}
        if not profile["enabled"]:
            return context
        for key, item in profile["items"].items():
            if item["kind"] == "preference":
                context["preferences"][key] = item
            elif item["kind"] == "habit":
                context["habits"][key] = item
            elif key.removeprefix("learning.") in concepts:
                context["learning"][key] = item
        historical = any(term in message for term in ("之前", "上次", "以前", "历史", "复盘", "记得", "昨晚", "昨天", "last time"))
        scopes = [scope_id] if scope_id else ["unscoped"]
        cross_scope = historical and any(term in message for term in ("跨服", "所有服务器", "所有档", "上一档"))
        with self.conversations.connection() as connection:
            rows = connection.execute("SELECT * FROM coach_memory_events WHERE player_id='local' AND kind='event' "
                "AND status='active' " + ("" if cross_scope else "AND scope=? ") + "ORDER BY created_at DESC LIMIT 100",
                () if cross_scope else tuple(scopes)).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if (concepts or not historical) and not set(concepts).intersection(data["topics"]):
                continue
            item = {"id": row["id"], "scope": row["scope"], "reported_at": row["created_at"],
                    "kind": row["memory_key"], "quote": data["quote"], "source": data["source"], "verification": data["verification"]}
            if len(json.dumps({**context, "events": [*context["events"], item]}, ensure_ascii=False)) > limit_chars:
                break
            context["events"].append(item)
            if len(context["events"]) >= 3:
                break
        for category in ("learning", "habits", "preferences"):
            while len(json.dumps(context, ensure_ascii=False)) > limit_chars and context[category]:
                context[category].pop(next(iter(context[category])))
        return context

    def inspect(self):
        profile = self.profile()
        with self.conversations.connection() as connection:
            rows = connection.execute("SELECT * FROM coach_memory_events WHERE status='active' ORDER BY created_at DESC LIMIT 100").fetchall()
            pending = connection.execute("SELECT COUNT(*) FROM turns WHERE json_extract(memory,'$.extraction.status')='pending'").fetchone()[0]
        return {**profile, "events": [{"id": row["id"], "kind": row["kind"], "key": row["memory_key"],
            "scope": row["scope"], "reported_at": row["created_at"], **json.loads(row["data"])} for row in rows],
            "pending": pending, "events_limit": 100}

    def control(self, action, target=None, connection=None):
        if connection is None:
            with self.conversations.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.control(action, target, connection)
        profile = self.profile(connection)
        if action in {"on", "off"}:
            profile["enabled"] = action == "on"
            profile["epoch"] += 1
        elif action == "forget":
            if target == "all":
                profile["items"] = {}
                profile["history_floor"] = now_iso()
                connection.execute("UPDATE coach_memory_events SET status='forgotten', data='{}'")
            else:
                rows = connection.execute("SELECT id,memory_key,kind FROM coach_memory_events WHERE status='active' AND (id=? OR memory_key=?)",
                                          (target, target)).fetchall()
                if not rows:
                    raise AppError("memory_not_found", "没有找到这条记忆；输入 /memory 查看可用标识。", 404)
                keys = {row["memory_key"] for row in rows if row["kind"] != "event"}
                for key in keys:
                    profile["items"].pop(key, None)
                    connection.execute("UPDATE coach_memory_events SET status='forgotten',data='{}' WHERE memory_key=?", (key,))
                for row in rows:
                    connection.execute("UPDATE coach_memory_events SET status='forgotten',data='{}' WHERE id=?", (row["id"],))
            profile["epoch"] += 1
        else:
            raise ValueError("Unknown memory action")
        profile["revision"] += 1
        self.save(connection, profile)
        return profile

    def forget_conversation(self, conversation_id, connection):
        rows = connection.execute("SELECT id,memory_key,kind FROM coach_memory_events WHERE conversation_id=?", (conversation_id,)).fetchall()
        profile = self.profile(connection)
        for row in rows:
            item = profile["items"].get(row["memory_key"])
            if item and (item["source"]["conversation_id"] == conversation_id or row["kind"] == "learning" and
                    row["id"] in {entry["event_id"] for entry in item["value"]["demonstrations"]}):
                profile["items"].pop(row["memory_key"])
        connection.execute("UPDATE coach_memory_events SET status='forgotten',data='{}' WHERE conversation_id=?", (conversation_id,))
        profile["epoch"] += 1
        profile["revision"] += 1
        self.save(connection, profile)

    def render(self):
        snapshot = self.inspect()
        lines = ["长期记忆：" + ("开启" if snapshot["enabled"] else "关闭（已有内容暂不使用）")]
        for key, item in snapshot["items"].items():
            if item["kind"] == "learning":
                value = item["value"]
                text = {"needs_help": "仍需引导", "self_reported": "自报理解，尚非独立掌握", "demonstrated": "有解释证据，非实战认证",
                        "discussed": "讨论过，尚无理解证据"}[value["last_signal"]]
                label = CONCEPTS[key.removeprefix("learning.")][0]
            else:
                label, text = LABELS.get(key, key), VALUES.get(item["value"], item["value"])
            stale = "（已过使用期）" if item["expires_at"] and item["expires_at"] <= now_iso() else ""
            lines.append(f"• {label}：{text}{stale} [{key}]；记录于 {item['updated_at'][:10]}")
        for event in snapshot["events"]:
            if event["kind"] == "event":
                lines.append(f"• 历史自报：{event['quote']} [{event['id']}]；范围 {event['scope']}；记录于 {event['reported_at'][:10]}")
        if len(lines) == 1:
            lines.append("还没有保存的长期记忆。")
        if snapshot["pending"]:
            lines.append("新记忆还在整理，稍后查看可获得更新。")
        lines.append("可说“以后回答简短些”来设置偏好；/memory remember 个人经历 立即提取并保存；/memory set verbosity detailed 更正详略；/memory forget 标识 删除一条；/memory forget all 清空；/memory off 关闭；/memory on 开启。")
        return "\n".join(lines)
