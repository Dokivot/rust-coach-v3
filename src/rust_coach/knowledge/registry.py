import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from rust_coach.contracts import AppError
from rust_coach.knowledge.schemas import KnowledgeRelease


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_identity(document):
    return digest({key: document[key] for key in ("kind", "text", "source", "source_hash", "snapshot", "platform", "ruleset", "branch")})


def index_body(document):
    return " ".join([document["title"], document["text"], document["terms"], *document["entities"], *document["claims"]])


def checked_payload(row):
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError) as error:
        raise AppError("knowledge_integrity_error", "知识包无法读取，已停止采用该包。", 503) from error
    if digest(payload) != row["sha256"]:
        raise AppError("knowledge_integrity_error", "知识包完整性检查失败，已停止采用该包。", 503)
    return payload


class KnowledgeRegistry:
    def __init__(self, store):
        self.store = store
        with store.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS knowledge_releases (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS knowledge_active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), release_id TEXT NOT NULL, revision INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS knowledge_units (release_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(release_id,id));"
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(release_id UNINDEXED, unit_id UNINDEXED, body);"
                "CREATE TABLE IF NOT EXISTS knowledge_revocations (release_id TEXT NOT NULL, unit_id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(release_id,unit_id));"
                "CREATE TABLE IF NOT EXISTS knowledge_queries (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS knowledge_feedback (conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, request_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(conversation_id,request_id));"
                "CREATE TABLE IF NOT EXISTS knowledge_audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);"
            )

    def install(self, data, rules_hash):
        from rust_coach.knowledge.bundle import default_release

        release = KnowledgeRelease.model_validate(data)
        if release.rules_sha256 != rules_hash:
            raise AppError("knowledge_rules_mismatch", "知识包与当前规则快照哈希不一致，没有启用。", 409)
        payload = release.model_dump(mode="json")
        canonical = KnowledgeRelease.model_validate(default_release())
        rules = {document.id: document.model_dump() for document in canonical.documents if document.kind == "rule"}
        for document in release.documents:
            if document.kind == "rule" or document.id in rules:
                content = document.model_dump(exclude={"admission"})
                expected = {key: value for key, value in rules.get(document.id, {}).items() if key != "admission"}
                if content != expected:
                    raise AppError("knowledge_rule_evidence_mismatch", "结构化规则证据与冻结目录不一致，拒绝安装。", 409)
        identity = digest(payload)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT sha256 FROM knowledge_releases WHERE id=?", (release.id,)).fetchone()
            if existing and existing["sha256"] != identity:
                raise AppError("knowledge_release_immutable", "同名知识包内容不可覆盖，请使用新的发布编号。", 409)
            if not existing:
                connection.execute("INSERT INTO knowledge_releases VALUES (?,?,?)", (release.id, identity, json.dumps(payload, ensure_ascii=False)))
                for document in release.documents:
                    connection.execute("INSERT INTO knowledge_units VALUES (?,?,?)", (release.id, document.id, document.model_dump_json()))
                    connection.execute("INSERT INTO knowledge_fts VALUES (?,?,?)", (release.id, document.id,
                        index_body(document.model_dump())))
            active = connection.execute("SELECT * FROM knowledge_active WHERE singleton=1").fetchone()
            if active is None:
                revision = active["revision"] + 1 if active else 1
                connection.execute("INSERT OR REPLACE INTO knowledge_active VALUES (1,?,?)", (release.id, revision))
        return {"release_id": release.id, "release_sha256": identity}

    def activate(self, release_id, expected_hash, review_note):
        if not review_note.strip():
            raise ValueError("publication_review_note_required")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM knowledge_releases WHERE id=?", (release_id,)).fetchone()
            if not row or row["sha256"] != expected_hash or digest(json.loads(row["payload"])) != expected_hash:
                raise AppError("knowledge_release_hash_mismatch", "发布包不存在或与已核对的哈希不同，未切换。", 409)
            current = self.snapshot(connection)
            if json.loads(row["payload"])["rules_sha256"] != current["release"]["rules_sha256"]:
                raise AppError("knowledge_rules_mismatch", "目标包不匹配固定规则，未切换。", 409)
            connection.execute("UPDATE knowledge_active SET release_id=?,revision=revision+1 WHERE singleton=1", (release_id,))
            audit = {"action": "activate", "from": current["release_id"], "to": release_id,
                     "sha256": expected_hash, "review_note": review_note[:2000], "reviewer": "local_operator",
                     "independent_game_review": False, "at": datetime.now(timezone.utc).isoformat()}
            connection.execute("INSERT INTO knowledge_audit(payload) VALUES (?)", (json.dumps(audit, ensure_ascii=False),))
        return self.snapshot()

    def snapshot(self, connection=None):
        if connection is None:
            with self.store.connection() as transaction:
                transaction.execute("BEGIN")
                return self.snapshot(transaction)
        row = connection.execute("SELECT r.payload,r.sha256,a.revision FROM knowledge_active a JOIN knowledge_releases r ON r.id=a.release_id WHERE a.singleton=1").fetchone()
        if not row:
            raise AppError("knowledge_unavailable", "没有可用的演示知识包，请先完成本地发布。", 503)
        release = checked_payload(row)
        revoked = {item["unit_id"]: item["reason"] for item in connection.execute(
            "SELECT unit_id,reason FROM knowledge_revocations WHERE release_id=?", (release["id"],))}
        withdrawn = {}
        packages = {release["id"]: {document["id"]: document for document in release["documents"]}}
        for item in connection.execute("SELECT r.id,r.payload,r.sha256,v.unit_id,v.reason FROM knowledge_revocations v "
                                       "JOIN knowledge_releases r ON r.id=v.release_id"):
            if item["id"] not in packages:
                packages[item["id"]] = {document["id"]: document for document in checked_payload(item)["documents"]}
            document = packages[item["id"]].get(item["unit_id"])
            if document is None:
                raise AppError("knowledge_integrity_error", "撤回记录与知识包不一致，已停止采用。", 503)
            withdrawn[evidence_identity(document)] = item["reason"]
        for document in release["documents"]:
            identity = evidence_identity(document)
            if identity in withdrawn:
                revoked[document["id"]] = withdrawn[identity]
        return {"release": release, "release_id": release["id"], "release_sha256": row["sha256"],
                "registry_revision": row["revision"], "revoked": revoked}

    def validate_index(self, snapshot, connection):
        rows = connection.execute("SELECT unit_id,body FROM knowledge_fts WHERE release_id=?", (snapshot["release_id"],)).fetchall()
        expected = {document["id"]: index_body(document) for document in snapshot["release"]["documents"]}
        if len(rows) != len(expected) or {row["unit_id"]: row["body"] for row in rows} != expected:
            raise sqlite3.DatabaseError("incomplete_knowledge_index")

    def rebuild_index(self):
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self.snapshot(connection)
            connection.execute("DELETE FROM knowledge_fts WHERE release_id=?", (snapshot["release_id"],))
            for document in snapshot["release"]["documents"]:
                connection.execute("INSERT INTO knowledge_fts VALUES (?,?,?)",
                                   (snapshot["release_id"], document["id"], index_body(document)))
            connection.execute("DELETE FROM knowledge_queries")
        return {"release_id": snapshot["release_id"], "indexed_documents": len(snapshot["release"]["documents"])}

    def validate(self, reference, connection=None):
        current = self.snapshot(connection)
        if any(reference.get(key) != current[key] for key in ("release_id", "release_sha256", "registry_revision")):
            raise AppError("knowledge_changed", "回答期间知识包或撤回记录已变化，旧结果未发布，请重试。", 409)

    def revoke(self, release_id, unit_id, reason):
        if not reason.strip():
            raise ValueError("revocation_reason_required")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute("SELECT 1 FROM knowledge_units WHERE release_id=? AND id=?", (release_id, unit_id)).fetchone()
            if not exists:
                raise AppError("knowledge_document_not_found", "该知识包中没有指定证据。", 404)
            connection.execute("INSERT OR REPLACE INTO knowledge_revocations VALUES (?,?,?)", (release_id, unit_id, reason[:1000]))
            connection.execute("UPDATE knowledge_active SET revision=revision+1 WHERE singleton=1")
            connection.execute("INSERT INTO knowledge_audit(payload) VALUES (?)", (json.dumps({"action": "revoke",
                "release_id": release_id, "document_id": unit_id, "reason": reason[:1000]}, ensure_ascii=False),))

    def review_feedback(self, conversation_id, request_id, disposition, note):
        if disposition not in {"rejected", "needs_release"} or not note.strip():
            raise ValueError("feedback_review_required")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM knowledge_feedback WHERE conversation_id=? AND request_id=?",
                                     (conversation_id, request_id)).fetchone()
            if not row:
                raise AppError("knowledge_feedback_not_found", "反馈不存在。", 404)
            payload = {**json.loads(row["payload"]), "status": disposition, "review_note": note[:2000],
                       "reviewer": "local_operator"}
            connection.execute("UPDATE knowledge_feedback SET payload=? WHERE conversation_id=? AND request_id=?",
                               (json.dumps(payload, ensure_ascii=False), conversation_id, request_id))
        return payload

    def feedback(self, conversation_id, request, reference):
        allowed = {item["id"] for item in reference.get("documents", [])} | set(reference.get("rule_dependencies", []))
        if not set(request.document_ids) <= allowed:
            raise AppError("knowledge_feedback_invalid", "只能反馈该回答实际引用的知识。", 422)
        payload = {**request.model_dump(), "release_id": reference["release_id"], "status": "pending_review",
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "verification": "user_report_not_fact"}
        with self.store.connection() as connection:
            old = connection.execute("SELECT payload FROM knowledge_feedback WHERE conversation_id=? AND request_id=?",
                                     (conversation_id, request.request_id)).fetchone()
            if old:
                previous = json.loads(old["payload"])
                if any(previous[key] != payload[key] for key in request.model_dump()):
                    raise AppError("request_conflict", "反馈标识已经用于另一项反馈。", 409)
                return previous
            connection.execute("INSERT INTO knowledge_feedback VALUES (?,?,?)", (conversation_id, request.request_id, json.dumps(payload, ensure_ascii=False)))
        return payload
