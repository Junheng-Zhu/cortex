import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .action import Action
from .state import AgentPhase, AgentState


DEFAULT_CHECKPOINT_PATH = Path.cwd() / ".cortex" / "checkpoints.db"


@dataclass(frozen=True)
class Checkpoint:
    session_id: str
    run_id: str
    goal: str | None
    plan: list[str]
    completed_actions: list[dict[str, Any]]
    pending_actions: list[dict[str, Any]]
    important_decisions: list[str]
    artifact_refs: list[str]
    phase: str
    skill_versions: dict[str, str] = field(default_factory=dict)
    schema_version: int = 2
    pending_input: list[dict[str, Any]] = field(default_factory=list)
    context_history: list[dict[str, Any]] = field(default_factory=list)
    skill_pending_disclosures: list[str] = field(default_factory=list)
    skill_accepted_disclosures: list[str] = field(default_factory=list)
    skill_searches: int = 0
    skill_search_limit: int = 1
    skill_local_queries: int = 0
    checkpoint_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def capture(cls, state: AgentState) -> "Checkpoint":
        completed = [action for action in state.actions if action.status in {"SUCCEEDED", "FAILED"}]
        return cls(
            session_id=state.session_id,
            run_id=state.run_id,
            goal=state.current_goal,
            plan=list(state.current_plan),
            completed_actions=[asdict(action) for action in completed],
            pending_actions=[asdict(action) for action in state.pending_actions],
            important_decisions=list(state.important_decisions),
            artifact_refs=list(state.artifact_references),
            phase=state.phase.value,
            skill_versions=dict(state.skill_versions),
            pending_input=list(state.pending_input),
            context_history=list(state.context_history),
            skill_pending_disclosures=sorted(state.skill_pending_disclosures),
            skill_accepted_disclosures=sorted(state.skill_accepted_disclosures),
            skill_searches=state.skill_searches,
            skill_search_limit=state.skill_search_limit,
            skill_local_queries=state.skill_local_queries,
        )

    def restore(self, *, new_run_id: str, max_steps: int) -> AgentState:
        completed = [Action(**action) for action in self.completed_actions]
        # Only calls which were still pending at checkpoint time may execute.
        pending = [Action(**action) for action in self.pending_actions if action["status"] == "PENDING"]
        return AgentState(
            run_id=new_run_id,
            session_id=self.session_id,
            phase=AgentPhase.ACT if pending else AgentPhase.DECIDE,
            actions=[*completed, *pending],
            pending_actions=pending,
            max_steps=max_steps,
            current_goal=self.goal,
            current_plan=list(self.plan),
            important_decisions=list(self.important_decisions),
            artifact_references=list(self.artifact_refs),
            step_count=len(completed),
            skill_versions=dict(self.skill_versions),
            pending_input=list(self.pending_input),
            context_history=list(self.context_history),
            skill_pending_disclosures=set(self.skill_pending_disclosures),
            skill_accepted_disclosures=set(self.skill_accepted_disclosures),
            skill_searches=self.skill_searches,
            skill_search_limit=self.skill_search_limit,
            skill_local_queries=self.skill_local_queries,
        )


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...
    def load(self, checkpoint_id: str) -> Checkpoint | None: ...
    def latest(self, session_id: str) -> Checkpoint | None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: list[Checkpoint] = []

    def save(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)

    def load(self, checkpoint_id: str) -> Checkpoint | None:
        return next((item for item in self.checkpoints if item.checkpoint_id == checkpoint_id), None)

    def latest(self, session_id: str) -> Checkpoint | None:
        return next((item for item in reversed(self.checkpoints) if item.session_id == session_id), None)


class SQLiteCheckpointStore:
    def __init__(self, path: str | Path = DEFAULT_CHECKPOINT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL)"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id, created_at)"
            )

    def save(self, checkpoint: Checkpoint) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?, ?)",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.session_id,
                    checkpoint.run_id,
                    checkpoint.created_at.isoformat(),
                    json.dumps(asdict(checkpoint), ensure_ascii=False, default=str),
                ),
            )

    def load(self, checkpoint_id: str) -> Checkpoint | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        return self._decode(row[0]) if row else None

    def latest(self, session_id: str) -> Checkpoint | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """SELECT payload FROM checkpoints WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        return self._decode(row[0]) if row else None

    @staticmethod
    def _decode(payload: str) -> Checkpoint:
        values = json.loads(payload)
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        return Checkpoint(**values)
