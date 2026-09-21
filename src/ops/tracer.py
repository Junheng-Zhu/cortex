import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / ".trace.db"


@dataclass(frozen=True)
class RunEvent:
    event_type: str
    run_id: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)


class RunRecorder:
    """Records the protocol-level lifecycle of one agent run."""

    def __init__(self, run_id: str | None = None, persist: bool = False):
        self.run_id = run_id or str(uuid4())
        self.persist = persist
        self.events: list[RunEvent] = []
        if persist:
            init_db()

    def record(self, event_type: str, **data: Any) -> RunEvent:
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            timestamp=datetime.now().isoformat(),
            data=data,
        )
        self.events.append(event)
        if self.persist:
            _persist_event(event)
        return event


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                step_type TEXT NOT NULL,
                content TEXT,
                duration_ms REAL,
                tokens_used INTEGER,
                metadata TEXT
            )"""
        )


def _persist_event(event: RunEvent) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO traces
               (session_id, timestamp, step_type, content, duration_ms,
                tokens_used, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.run_id,
                event.timestamp,
                event.event_type,
                str(event.data.get("content", "")),
                event.data.get("duration_ms", 0),
                event.data.get("tokens_used", 0),
                json.dumps(event.data, default=str),
            ),
        )


def log_event(
    session_id: str,
    step_type: str,
    content: str = "",
    duration_ms: float = 0,
    tokens_used: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Backward-compatible adapter for the legacy loop and dashboard."""
    recorder = RunRecorder(session_id, persist=True)
    recorder.record(
        step_type,
        content=content,
        duration_ms=duration_ms,
        tokens_used=tokens_used,
        metadata=metadata or {},
    )


def get_recent_traces(limit: int = 50):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """SELECT id, session_id, timestamp, step_type, content,
                      duration_ms, tokens_used, metadata
               FROM traces ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
