import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / ".trace.db"
SENSITIVE_KEYS = {"api_key", "authorization", "cookie", "password", "secret", "token"}
MAX_TRACE_STRING = 2_000
MAX_TRACE_ITEMS = 50


def minimize_trace_value(value: Any, depth: int = 0) -> Any:
    """Bound trace payloads and redact common credential fields."""
    if depth >= 6:
        return "<max-depth>"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_TRACE_ITEMS:
                result["<truncated>"] = len(value) - MAX_TRACE_ITEMS
                break
            result[str(key)] = (
                "<redacted>"
                if str(key).lower() in SENSITIVE_KEYS
                else minimize_trace_value(item, depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [
            minimize_trace_value(item, depth + 1) for item in value[:MAX_TRACE_ITEMS]
        ]
        if len(value) > MAX_TRACE_ITEMS:
            items.append(f"<{len(value) - MAX_TRACE_ITEMS} items truncated>")
        return items
    if isinstance(value, str):
        return value[:MAX_TRACE_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_TRACE_STRING]


@dataclass(frozen=True)
class RunEvent:
    event_type: str
    run_id: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str | None = None
    success: bool | None = None
    steps: int = 0
    latency_ms: float = 0
    llm_latency_ms: float = 0
    tool_duration_ms: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    termination_reason: str | None = None


class RunRecorder:
    """Records the protocol-level lifecycle of one agent run."""

    def __init__(self, run_id: str | None = None, persist: bool = False):
        self.run_id = run_id or str(uuid4())
        self._initial_run_id_available = run_id is not None
        self.persist = persist
        self.events: list[RunEvent] = []
        self.runs: list[RunSummary] = []
        self.current_run: RunSummary | None = None
        if persist:
            init_db()

    def start_run(self) -> RunSummary:
        """Start a fresh run, retaining earlier events and summaries."""
        if self._initial_run_id_available and not self.runs:
            self._initial_run_id_available = False
        else:
            self.run_id = str(uuid4())
        summary = RunSummary(
            run_id=self.run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self.runs.append(summary)
        self.current_run = summary
        return summary

    def finish_run(
        self,
        *,
        success: bool,
        steps: int,
        latency_ms: float,
        termination_reason: str,
    ) -> RunSummary:
        if self.current_run is None:
            raise RuntimeError("cannot finish a run that was not started")
        summary = self.current_run
        summary.finished_at = datetime.now(timezone.utc).isoformat()
        summary.success = success
        summary.steps = steps
        summary.latency_ms = latency_ms
        summary.termination_reason = termination_reason
        if self.persist:
            _persist_run(summary)
        return summary

    def record(self, event_type: str, **data: Any) -> RunEvent:
        data = minimize_trace_value(data)
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            timestamp=datetime.now().isoformat(),
            data=data,
        )
        self.events.append(event)
        if self.current_run is not None:
            if event_type == "llm_call":
                self.current_run.llm_latency_ms += float(data.get("duration_ms", 0))
                self.current_run.input_tokens += int(data.get("input_tokens", 0))
                self.current_run.output_tokens += int(data.get("output_tokens", 0))
                self.current_run.total_tokens += int(data.get("total_tokens", 0))
            elif event_type == "action":
                self.current_run.tool_duration_ms += float(data.get("duration_ms", 0))
        if self.persist:
            _persist_event(event)
        return event

    @staticmethod
    def preview(output: Any, limit: int = 500) -> str:
        rendered = "" if output is None else str(output)
        return rendered[:limit]

    @staticmethod
    def output_length(output: Any) -> int:
        return len("" if output is None else str(output))


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                success INTEGER,
                steps INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                llm_latency_ms REAL NOT NULL,
                tool_duration_ms REAL NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                termination_reason TEXT
            )"""
        )


def _persist_run(run: RunSummary) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.started_at,
                run.finished_at,
                run.success,
                run.steps,
                run.latency_ms,
                run.llm_latency_ms,
                run.tool_duration_ms,
                run.input_tokens,
                run.output_tokens,
                run.total_tokens,
                run.termination_reason,
            ),
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


def get_recent_runs(limit: int = 50):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
