"""Explicit, provider-independent evaluation task contracts."""

from dataclasses import dataclass, field
from typing import Literal

ExpectedOutcome = Literal[
    "COMPLETED",
    "BLOCKED",
    "TIMEOUT",
    "PERMISSION_DENIED",
    "RECOVERED",
    "RUNTIME_ERROR",
]


@dataclass(frozen=True)
class Task:
    task_id: str
    input: str
    required_tools: list[str] = field(default_factory=list)
    expected_tool_sequence: list[str] | None = None
    forbidden_tools: list[str] = field(default_factory=list)
    expected_outcome: ExpectedOutcome = "COMPLETED"
    expected_error_type: str | None = None
    excluded_permissions: list[str] = field(default_factory=list)
