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
    preview: str | None = None
    artifact_id: str | None = None
    artifact_ref: str | None = None
    artifact_path: str | None = None
    size_chars: int | None = None
    truncated: bool = False
