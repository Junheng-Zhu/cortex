from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Action:
    """A tool invocation chosen by the agent."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Provider protocol identifier.  This must be echoed back to the provider,
    # while action_id is the runtime identifier used to join trace records.
    call_id: str = ""
    action_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "PENDING"
