from dataclasses import dataclass
from enum import Enum

from .models import MemoryKind


class MemoryDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    kind: str
    source: str = "explicit"


class MemoryWritePolicy:
    """Allow only stable, deliberately classified long-term knowledge."""

    denied_sources = {
        "pytest", "eval", "tool_output", "tool_observation", "shell",
        "stdout", "stderr", "trace", "stack_trace", "exception",
        "temporary", "token_usage", "latency", "git_status", "git_diff",
        "reflection_continue",
    }
    allowed_kinds = {
        MemoryKind.USER_FACT.value,
        MemoryKind.PREFERENCE.value,
        MemoryKind.PROJECT_CONSTRAINT.value,
        MemoryKind.DECISION.value,
    }

    def evaluate(self, candidate: MemoryCandidate) -> MemoryDecision:
        source = candidate.source.casefold().strip()
        if source in self.denied_sources or source.startswith(("pytest", "eval", "trace")):
            return MemoryDecision.DENY
        if candidate.kind not in self.allowed_kinds:
            return MemoryDecision.DENY
        if not candidate.content.strip():
            return MemoryDecision.DENY
        return MemoryDecision.ALLOW

    def allows(self, candidate: MemoryCandidate) -> bool:
        return self.evaluate(candidate) is MemoryDecision.ALLOW
