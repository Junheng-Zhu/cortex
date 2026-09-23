from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Session:
    """Runtime-owned task session and its durable working-set metadata."""
    session_id: str = field(default_factory=lambda: uuid4().hex)
    goal: str | None = None
    plan: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    artifact_references: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
