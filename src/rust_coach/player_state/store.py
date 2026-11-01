import hashlib
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

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
                    snapshot = {**snapshot, "revision": 0, "facts": {}, "containers": {}, "invalidations": {}}
                else:
                    row = connection.execute("SELECT snapshot FROM player_observations WHERE scope_id=? AND revision=?",
                                             (scope_id, revision)).fetchone()
                    if not row:
                        raise AppError("revision_not_found", "状态版本不存在。", 404)
                    snapshot = json.loads(row["snapshot"])
        return project(snapshot, now or datetime.now(timezone.utc))

    def update(self, scope_id, request: StateUpdate, *, now=None):
        now = now or datetime.now(timezone.utc)
        payload = request.model_dump_json()
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision, snapshot FROM player_scopes WHERE id=?", (scope_id,)).fetchone()
            if not row:
                raise AppError("scope_not_found", "玩家状态档案不存在。", 404)
            replay = connection.execute("SELECT payload, snapshot FROM player_observations WHERE scope_id=? AND observation_id=?",
                                        (scope_id, str(request.observation_id))).fetchone()
            if replay:
                if json.loads(replay["payload"]) != json.loads(payload):
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

    @staticmethod
    def reference(snapshot):
        canonical = {key: snapshot[key] for key in ("scope", "revision", "facts", "containers", "evaluated_at")}
        return {"scope_id": snapshot["scope"]["id"], "revision": snapshot["revision"],
                "evaluated_at": snapshot["evaluated_at"],
                "sha256": hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()}
