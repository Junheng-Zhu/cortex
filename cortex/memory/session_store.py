import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cortex.runtime.session import Session
from .retrieval import lexical_terms


DEFAULT_SESSION_PATH = Path.cwd() / ".cortex" / "sessions.db"


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    run_id: str
    event_type: str
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionStore(Protocol):
    """Persistence boundary for episodic conversation memory."""

    def save(self, session: Session) -> None: ...
    def append(self, event: SessionEvent) -> None: ...
    def load(self, session_id: str) -> Session | None: ...
    def events(self, session_id: str, limit: int = 100) -> list[SessionEvent]: ...
    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[SessionEvent]: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._events: list[SessionEvent] = []

    def save(self, session: Session) -> None:
        self.sessions[session.session_id] = _copy_session(session)

    def append(self, event: SessionEvent) -> None:
        self._events.append(event)

    def load(self, session_id: str) -> Session | None:
        session = self.sessions.get(session_id)
        return _copy_session(session) if session else None

    def events(self, session_id: str, limit: int = 100) -> list[SessionEvent]:
        return [event for event in self._events if event.session_id == session_id][-limit:]

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[SessionEvent]:
        terms = _terms(query)
        events = [
            event
            for event in self._events
            if _in_scope(event, user_id, project_id)
        ]
        return _rank_events(events, terms, limit)


class SQLiteSessionStore:
    def __init__(self, path: str | Path = DEFAULT_SESSION_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    last_run_id TEXT, previous_response_id TEXT,
                    compact_summary TEXT NOT NULL, artifact_refs TEXT NOT NULL,
                    history TEXT NOT NULL, metadata TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS session_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL, metadata TEXT NOT NULL)"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, id)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.created_at.isoformat(),
                    session.last_run_id,
                    session.previous_response_id,
                    session.compact_summary,
                    _json(session.artifact_refs),
                    _json(session.history),
                    _json(session.metadata),
                ),
            )

    def append(self, event: SessionEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO session_events
                   (session_id, run_id, event_type, content, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.session_id,
                    event.run_id,
                    event.event_type,
                    event.content,
                    event.created_at.isoformat(),
                    _json(event.metadata),
                ),
            )

    def load(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        history = json.loads(row[6])
        if not history:
            history = [
                {"role": event.event_type, "content": event.content}
                for event in self.events(session_id)
                if event.event_type in {"user", "assistant"}
            ]
        return Session(
            session_id=row[0],
            created_at=datetime.fromisoformat(row[1]),
            last_run_id=row[2],
            previous_response_id=row[3],
            compact_summary=row[4],
            artifact_refs=json.loads(row[5]),
            history=history,
            metadata=json.loads(row[7]),
        )

    def events(self, session_id: str, limit: int = 100) -> list[SessionEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT session_id, run_id, event_type, content, created_at, metadata
                   FROM session_events WHERE session_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [_event(row) for row in reversed(rows)]

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[SessionEvent]:
        terms = _terms(query)
        if not terms:
            return []
        clauses = " OR ".join("lower(content) LIKE ?" for _ in terms)
        args = [f"%{term}%" for term in terms]
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT session_id, run_id, event_type, content, created_at, metadata
                    FROM session_events WHERE {clauses}
                    ORDER BY id DESC LIMIT ?""",
                [*args, max(limit * 100, 500)],
            ).fetchall()
        events = [_event(row) for row in rows]
        scoped = [
            event
            for event in events
            if _in_scope(event, user_id, project_id)
        ]
        return _rank_events(scoped, terms, limit)


def _terms(query: str) -> set[str]:
    return lexical_terms(query)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _event(row: tuple[Any, ...]) -> SessionEvent:
    return SessionEvent(
        session_id=row[0],
        run_id=row[1],
        event_type=row[2],
        content=row[3],
        created_at=datetime.fromisoformat(row[4]),
        metadata=json.loads(row[5]),
    )


def _in_scope(
    event: SessionEvent, user_id: str | None, project_id: str | None
) -> bool:
    if user_id is not None and event.metadata.get("user_id") != user_id:
        return False
    if project_id is not None and event.metadata.get("project_id") != project_id:
        return False
    return True


def _rank_events(
    events: list[SessionEvent], terms: set[str], limit: int
) -> list[SessionEvent]:
    """Rank lexical matches, preferring user-authored historical facts."""
    source_weights = {"user": 3, "assistant": 1, "tool": 0}
    ranked = []
    for event in events:
        lexical_score = sum(term in event.content.casefold() for term in terms)
        if lexical_score == 0:
            continue
        source_weight = source_weights.get(event.event_type, 0)
        if event.metadata.get("kind") == "user_fact":
            source_weight += 2
        ranked.append((lexical_score, source_weight, event.created_at, event))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    return [event for _, _, _, event in ranked[:limit]]


def _copy_session(session: Session) -> Session:
    return Session(
        session_id=session.session_id,
        created_at=session.created_at,
        last_run_id=session.last_run_id,
        previous_response_id=session.previous_response_id,
        compact_summary=session.compact_summary,
        artifact_refs=list(session.artifact_refs),
        temporary_chat=session.temporary_chat,
        history=[dict(item) for item in session.history],
        metadata=dict(session.metadata),
    )
