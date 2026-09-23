"""Minimal, provider-independent evaluation task contract."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Task:
    task_id: str
    input: str
    expected_tools: list[str] = field(default_factory=list)
    expected_success: bool = True
