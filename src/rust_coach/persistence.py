import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rust_coach.contracts import AppError, Conversation, Turn


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY);"
                "CREATE TABLE IF NOT EXISTS turns ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                "conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
                "request_id TEXT NOT NULL, user TEXT NOT NULL, assistant TEXT NOT NULL,"
                "created_at TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,"
                "UNIQUE(conversation_id, request_id));"
                "CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, sequence);"
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(turns)")}
            if "calculation" not in columns:
                connection.execute("ALTER TABLE turns ADD COLUMN calculation TEXT")
            if "player_state_ref" not in columns:
                connection.execute("ALTER TABLE turns ADD COLUMN player_state_ref TEXT")
            if "advice" not in columns:
                connection.execute("ALTER TABLE turns ADD COLUMN advice TEXT")

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self) -> Conversation:
        conversation_id = str(uuid4())
        with self.connection() as connection:
            connection.execute("INSERT INTO conversations(id) VALUES (?)", (conversation_id,))
        return Conversation(id=conversation_id, turns=[])

    def get(self, conversation_id: str) -> Conversation:
        with self.connection() as connection:
            exists = connection.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not exists:
                raise AppError("conversation_not_found", "会话不存在，请创建新对话。", 404)
            rows = connection.execute(
                "SELECT request_id, user, assistant, created_at, truncated, calculation, player_state_ref, advice FROM turns "
                "WHERE conversation_id = ? ORDER BY sequence", (conversation_id,)
            ).fetchall()
        return Conversation(id=conversation_id, turns=[Turn(**(dict(row) | {
            "calculation": json.loads(row["calculation"]) if row["calculation"] else None,
            "player_state_ref": json.loads(row["player_state_ref"]) if row["player_state_ref"] else None,
            "advice": json.loads(row["advice"]) if row["advice"] else None,
        })) for row in rows])

    def append(self, conversation_id: str, request_id: str, user: str, assistant: str, truncated: bool,
               calculation: dict | None = None, player_state_ref: dict | None = None, advice: dict | None = None) -> Turn:
        turn = Turn(request_id=request_id, user=user, assistant=assistant,
                    created_at=datetime.now(timezone.utc).isoformat(), truncated=truncated, calculation=calculation,
                    player_state_ref=player_state_ref, advice=advice)
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO turns(conversation_id, request_id, user, assistant, created_at, truncated, calculation, player_state_ref, advice) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, request_id, user, assistant, turn.created_at, truncated,
                 json.dumps(calculation, ensure_ascii=False) if calculation else None,
                 json.dumps(player_state_ref, ensure_ascii=False) if player_state_ref else None,
                 json.dumps(advice, ensure_ascii=False) if advice else None),
            )
        return turn

    def delete(self, conversation_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
