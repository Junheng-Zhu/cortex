from dataclasses import dataclass
from typing import Any


@dataclass
class Observation:
    """The normalized result of executing one action."""

    action_id: str
    success: bool
    output: Any = None
    error: str | None = None
    error_type: str | None = None
