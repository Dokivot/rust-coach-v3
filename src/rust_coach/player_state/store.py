import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from rust_coach.contracts import AppError
from rust_coach.player_state.domain import apply_update, project
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate


logger = logging.getLogger("rust_coach")


class PlayerStateStore:
    def __init__(self, conversation_store):
        self.store = conversation_store
        with self.store.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS player_scopes (id TEXT PRIMARY KEY, server_id TEXT NOT NULL,"
                "wipe_id TEXT NOT NULL, label TEXT NOT NULL, ruleset TEXT NOT NULL, revision INTEGER NOT NULL,"
                "snapshot TEXT NOT NULL, UNIQUE(server_id, wipe_id));"
                "CREATE TABLE IF NOT EXISTS player_observations (scope_id TEXT NOT NULL REFERENCES player_scopes(id),"
                "observation_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,"
                "snapshot TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope_id, observation_id),"
                "UNIQUE(scope_id, revision));"
                "CREATE TABLE IF NOT EXISTS conversation_player_scope (conversation_id TEXT PRIMARY KEY "
                "REFERENCES conversations(id) ON DELETE CASCADE, scope_id TEXT NOT NULL REFERENCES player_scopes(id));"
                "CREATE TABLE IF NOT EXISTS player_state_preferences (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE IF NOT EXISTS chat_state_events (conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
                "request_id TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(conversation_id, request_id));"
            )

    def list_scopes(self):
        with self.store.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, server_id, wipe_id, label, ruleset, revision FROM player_scopes ORDER BY rowid DESC")]

    def create(self, request: ScopeCreate):
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id FROM player_scopes WHERE server_id=? AND wipe_id=?",
                                          (request.server_id, request.wipe_id)).fetchone()
            if existing:
                raise AppError("scope_exists", "这个服务器与清档周期已有状态档案，请从列表选择。", 409)
            identifier = str(uuid4())
            scope = {"id": identifier, **request.model_dump()}
            snapshot = {"schema_version": 1, "scope": scope, "revision": 0, "facts": {}, "containers": {}}
            connection.execute("INSERT INTO player_scopes VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (identifier, request.server_id, request.wipe_id, request.label, request.ruleset,
                                0, json.dumps(snapshot, ensure_ascii=False)))
        return project(snapshot, datetime.now(timezone.utc))

    def get(self, scope_id, *, now=None, revision=None):
        with self.store.connection() as connection:
            row = connection.execute("SELECT snapshot FROM player_scopes WHERE id=?", (scope_id,)).fetchone()
            if not row:
                raise AppError("scope_not_found", "玩家状态档案不存在。", 404)
            snapshot = json.loads(row["snapshot"])
            if revision is not None and revision != snapshot["revision"]:
                if revision == 0:
                    snapshot = {**snapshot, "revision": 0, "facts": {}, "containers": {}, "invalidations": {}, "visual_observations": {}}
                else:
                    row = connection.execute("SELECT snapshot FROM player_observations WHERE scope_id=? AND revision=?",
                                             (scope_id, revision)).fetchone()
                    if not row:
                        raise AppError("revision_not_found", "状态版本不存在。", 404)
                    snapshot = json.loads(row["snapshot"])
        return project(snapshot, now or datetime.now(timezone.utc))

    def update(self, scope_id, request: StateUpdate, *, now=None, connection=None):
        now = now or datetime.now(timezone.utc)
        payload = request.model_dump_json()
        if connection is None:
            with self.store.connection() as transaction:
                transaction.execute("BEGIN IMMEDIATE")
                return self.update(scope_id, request, now=now, connection=transaction)
        if connection is not None:
            row = connection.execute("SELECT revision, snapshot FROM player_scopes WHERE id=?", (scope_id,)).fetchone()
            if not row:
                raise AppError("scope_not_found", "玩家状态档案不存在。", 404)
            replay = connection.execute("SELECT payload, snapshot FROM player_observations WHERE scope_id=? AND observation_id=?",
                                        (scope_id, str(request.observation_id))).fetchone()
            if replay:
                old_payload, new_payload = json.loads(replay["payload"]), json.loads(payload)
                old_payload["changes"].setdefault("visual_observations", [])
                if old_payload != new_payload:
                    raise AppError("observation_conflict", "这条观测标识已经用于其他内容，请重新提交。", 409)
                return project(json.loads(replay["snapshot"]), now)
            if row["revision"] != request.expected_revision:
                raise AppError("state_revision_conflict", "状态已被其他页面更新，请刷新后核对并重新提交。", 409)
            snapshot = apply_update(json.loads(row["snapshot"]), request, now)
            snapshot["revision"] += 1
            serialized = json.dumps(snapshot, ensure_ascii=False)
            connection.execute("UPDATE player_scopes SET revision=?, snapshot=? WHERE id=?",
                               (snapshot["revision"], serialized, scope_id))
            connection.execute("INSERT INTO player_observations VALUES (?, ?, ?, ?, ?, ?)",
                               (scope_id, str(request.observation_id), snapshot["revision"], payload, serialized, now.isoformat()))
        logger.info("player_state_updated scope_id=%s revision=%s observation_id=%s", scope_id,
                    snapshot["revision"], request.observation_id)
        return project(snapshot, now)

    def binding(self, conversation_id):
        self.store.get(conversation_id)
        with self.store.connection() as connection:
            row = connection.execute("SELECT scope_id FROM conversation_player_scope WHERE conversation_id=?", (conversation_id,)).fetchone()
        return row["scope_id"] if row else None

    def bind(self, conversation_id, scope_id):
        self.store.get(conversation_id)
        if scope_id:
            self.get(scope_id)
        with self.store.connection() as connection:
            if scope_id:
                connection.execute("INSERT INTO conversation_player_scope VALUES (?, ?) ON CONFLICT(conversation_id) "
                                   "DO UPDATE SET scope_id=excluded.scope_id", (conversation_id, scope_id))
            else:
                connection.execute("DELETE FROM conversation_player_scope WHERE conversation_id=?", (conversation_id,))
            connection.execute("INSERT INTO player_state_preferences VALUES ('active_scope', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (scope_id,))

    def inherit_binding(self, conversation_id):
        with self.store.connection() as connection:
            row = connection.execute("SELECT value FROM player_state_preferences WHERE key='active_scope'").fetchone()
        if row and row["value"]:
            self.bind(conversation_id, row["value"])

    def apply_chat_event(self, conversation_id, request, scope_id, event):
        payload = json.dumps({"message": request.message, "scope_id": scope_id,
                              "request": request.model_dump(mode="json")}, sort_keys=True, ensure_ascii=False)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute("SELECT payload, receipt FROM chat_state_events WHERE conversation_id=? AND request_id=?",
                                        (conversation_id, str(request.request_id))).fetchone()
            if replay:
                old = json.loads(replay["payload"])
                saved_receipt = json.loads(replay["receipt"])
                same_detached = saved_receipt["event"] == "scope_changed" and scope_id is None and old["request"] == request.model_dump(mode="json")
                if replay["payload"] != payload and not same_detached:
                    raise AppError("request_conflict", "此请求已写入状态事件，但内容或档案发生变化，请使用新请求标识。", 409)
                return saved_receipt
            if event is None:
                return None
            if event == "scope_changed":
                connection.execute("DELETE FROM conversation_player_scope WHERE conversation_id=?", (conversation_id,))
                connection.execute("INSERT INTO player_state_preferences VALUES ('active_scope', NULL) "
                                   "ON CONFLICT(key) DO UPDATE SET value=NULL")
                receipt = {"event": event, "scope_id": scope_id, "revision": None,
                    "observation_id": str(request.request_id), "source": "explicit_user_report",
                    "message": "已解除当前对话的旧服务器档案关联，旧档案和历史保留。请在玩家状态页选择新服务器与清档，再继续计算。"}
                connection.execute("INSERT INTO chat_state_events VALUES (?, ?, ?, ?)",
                    (conversation_id, str(request.request_id), payload, json.dumps(receipt, ensure_ascii=False)))
                return receipt
            if not scope_id:
                raise AppError("player_scope_required", "请先在玩家状态页关联服务器和清档档案，才能持久记录背包丢失；本次尚未写入。", 422)
            row = connection.execute("SELECT revision FROM player_scopes WHERE id=?", (scope_id,)).fetchone()
            observed_at = datetime.now(timezone.utc)
            update = StateUpdate(observation_id=uuid5(NAMESPACE_URL, "chat-state:" + conversation_id + ":" + str(request.request_id)),
                                 expected_revision=row["revision"], observed_at=observed_at,
                                 source="text_confirmed", changes={"event": event})
            snapshot = self.update(scope_id, update, now=observed_at, connection=connection)
            receipt = {"event": event, "scope_id": scope_id, "previous_revision": row["revision"],
                       "revision": snapshot["revision"], "observation_id": str(update.observation_id),
                       "observed_at": observed_at.isoformat(), "source": "explicit_user_report",
                       "message": "已将旧背包标记为失效并保留历史，当前背包已清零。其他容器没有清零。"
                       if event in {"death", "backpack_lost"} else "已保存当前状态事件，受影响记录已失效；历史记录保留。"}
            connection.execute("INSERT INTO chat_state_events VALUES (?, ?, ?, ?)",
                               (conversation_id, str(request.request_id), payload, json.dumps(receipt, ensure_ascii=False)))
        return receipt

    @staticmethod
    def reference(snapshot):
        canonical = {key: snapshot[key] for key in ("scope", "revision", "facts", "containers", "evaluated_at")}
        if snapshot.get("visual_observations"):
            canonical["visual_observations"] = snapshot["visual_observations"]
        return {"scope_id": snapshot["scope"]["id"], "revision": snapshot["revision"],
                "evaluated_at": snapshot["evaluated_at"],
                "sha256": hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()}
