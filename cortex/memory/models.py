from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class MemoryKind(str, Enum):
    USER_FACT = "user_fact"
    PREFERENCE = "preference"
    PROJECT_CONSTRAINT = "project_constraint"
    DECISION = "decision"
    OTHER = "other"


@dataclass(frozen=True)
class MemoryRecord:
    content: str
    scope_type: str
    scope_id: str
    kind: str = MemoryKind.OTHER.value
    memory_id: str = field(default_factory=lambda: uuid4().hex)
    session_id: str | None = None
    run_id: str | None = None
    source: str = "explicit"
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.memory_id

    @property
    def scope(self) -> str:
        """Legacy display form; storage always uses type and id separately."""
        return f"{self.scope_type}:{self.scope_id}"
