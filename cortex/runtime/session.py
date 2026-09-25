from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class SessionConfig:
    """Controls session-level persistence without coupling memory and traces."""

    temporary_chat: bool = False
    persist_trace: bool = True


@dataclass
class Session:
    """A conversation shared by many independent agent runs.

    Conversation history and the provider cursor belong to the session.  A run's
    ``AgentState`` only contains the mutable state needed to execute that run.
    """

    session_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_run_id: str | None = None
    previous_response_id: str | None = None
    compact_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    temporary_chat: bool = False
    history: list[dict[str, Any]] = field(default_factory=list, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Compatibility with the old working-set name.
    @property
    def artifact_references(self) -> list[str]:
        return self.artifact_refs

    @classmethod
    def from_config(cls, config: SessionConfig, **values: Any) -> "Session":
        return cls(temporary_chat=config.temporary_chat, **values)
