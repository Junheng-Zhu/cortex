import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .models import MemoryRecord


DEFAULT_MEMORY_PATH = Path.cwd() / ".cortex" / "memory.db"


class MemoryStore(Protocol):
    def put(self, record: MemoryRecord) -> bool: ...
    def list(self, scope_type: str, scope_id: str | None = None) -> list[MemoryRecord]: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []

    def put(self, record: MemoryRecord) -> bool:
        if any(r.scope_type == record.scope_type and r.scope_id == record.scope_id and r.content == record.content for r in self._records):
            return False
        self._records.append(record)
        return True

    def list(self, scope_type: str, scope_id: str | None = None) -> list[MemoryRecord]:
        # Accept the former "scope" API as a useful compatibility shorthand.
        if scope_id is None and ":" in scope_type:
            scope_type, scope_id = scope_type.split(":", 1)
        now = datetime.now(timezone.utc)
        return [
            r
            for r in self._records
            if r.scope_type == scope_type
            and (scope_id is None or r.scope_id == scope_id)
            and (r.expires_at is None or r.expires_at > now)
        ]


class SQLiteMemoryStore:
    """Durable SQLite memory store with exact-content scope deduplication."""

    def __init__(self, path: str | Path = DEFAULT_MEMORY_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
                content TEXT NOT NULL, session_id TEXT, run_id TEXT,
                source TEXT NOT NULL, importance REAL NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT, metadata TEXT NOT NULL,
                UNIQUE(scope_type, scope_id, content))""")

    def _connect(self):
        return sqlite3.connect(self.path)

    def put(self, record: MemoryRecord) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.memory_id, record.kind, record.scope_type, record.scope_id,
                 record.content, record.session_id, record.run_id, record.source,
                 record.importance, record.created_at.isoformat(),
                 record.expires_at.isoformat() if record.expires_at else None,
                 json.dumps(record.metadata, ensure_ascii=False, default=str)),
            )
            return cursor.rowcount == 1

    def list(self, scope_type: str, scope_id: str | None = None) -> list[MemoryRecord]:
        if scope_id is None and ":" in scope_type:
            scope_type, scope_id = scope_type.split(":", 1)
        sql = "SELECT * FROM memories WHERE scope_type = ? AND (expires_at IS NULL OR expires_at > ?)"
        args: list[str] = [scope_type, datetime.now(timezone.utc).isoformat()]
        if scope_id is not None:
            sql += " AND scope_id = ?"
            args.append(scope_id)
        sql += " ORDER BY importance DESC, created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [MemoryRecord(memory_id=r[0], kind=r[1], scope_type=r[2], scope_id=r[3], content=r[4], session_id=r[5], run_id=r[6], source=r[7], importance=r[8], created_at=datetime.fromisoformat(r[9]), expires_at=datetime.fromisoformat(r[10]) if r[10] else None, metadata=json.loads(r[11])) for r in rows]
