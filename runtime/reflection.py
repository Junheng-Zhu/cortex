from dataclasses import dataclass


@dataclass
class ReflectionResult:
    """The agent's assessment of the most recent observation."""

    status: str
    summary: str
    next_hint: str | None = None
