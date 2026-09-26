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
        source = candidate.source.casefold().strip().replace("-", "_").replace(":", "_")
        if any(denied in source for denied in self.denied_sources):
            return MemoryDecision.DENY
        if candidate.kind not in self.allowed_kinds:
            return MemoryDecision.DENY
        if not candidate.content.strip():
            return MemoryDecision.DENY
        content = candidate.content.casefold()
        noise_markers = (
            "traceback (most recent call last)", "pytest", "short test summary info",
            "stdout:", "stderr:", "tokens used", "latency_ms", "git diff",
        )
        if any(marker in content for marker in noise_markers):
            return MemoryDecision.DENY
        return MemoryDecision.ALLOW

    def allows(self, candidate: MemoryCandidate) -> bool:
        return self.evaluate(candidate) is MemoryDecision.ALLOW
