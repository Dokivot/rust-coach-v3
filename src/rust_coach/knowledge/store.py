import hashlib
import json
from datetime import datetime, timedelta, timezone


class EvidenceStore:
    def __init__(self, conversations):
        self.conversations = conversations
        with conversations.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS evidence_documents ("
                "id TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS evidence_queries ("
                "cache_key TEXT PRIMARY KEY, expires_at TEXT NOT NULL, document_ids TEXT NOT NULL);"
            )

    def put(self, document):
        identity = document["url"] + document["text"] + document["fetched_at"]
        identifier = hashlib.sha256(identity.encode()).hexdigest()
        document = {**document, "id": identifier,
                    "content_hash": hashlib.sha256(document["text"].encode()).hexdigest()}
        with self.conversations.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO evidence_documents VALUES (?, ?, ?, ?)",
                               (identifier, document["url"], document["fetched_at"], json.dumps(document, ensure_ascii=False)))
        return document

    def get(self, identifier):
        with self.conversations.connection() as connection:
            row = connection.execute("SELECT payload FROM evidence_documents WHERE id = ?", (identifier,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def cached(self, key, now=None):
        now = now or datetime.now(timezone.utc)
        with self.conversations.connection() as connection:
            row = connection.execute("SELECT * FROM evidence_queries WHERE cache_key = ?", (key,)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= now:
            return []
        documents = [self.get(identifier) for identifier in json.loads(row["document_ids"])]
        return [document for document in documents if document]

    def cache(self, key, documents, ttl_hours):
        if not documents:
            return
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        with self.conversations.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO evidence_queries VALUES (?, ?, ?)",
                               (key, expires.isoformat(), json.dumps([document["id"] for document in documents])))
