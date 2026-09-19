from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Action:
    """A tool invocation chosen by the agent."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "PENDING"
