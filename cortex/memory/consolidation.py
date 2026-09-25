import re
from dataclasses import dataclass

from .models import MemoryKind
from .policy import MemoryCandidate
from .scopes import MemoryScope


@dataclass(frozen=True)
class ScopedMemoryCandidate:
    candidate: MemoryCandidate
    scope_type: str
    scope_id: str


class MemoryConsolidator:
    """Extract a deliberately small set of deterministic semantic candidates."""

    explicit_patterns = (
        re.compile(r"(?:please\s+)?remember(?:\s+that)?\s+(.+)", re.I),
        re.compile(r"请?记住[：,:]?\s*(.+)"),
    )

    def explicit(self, text: str, metadata: dict[str, object]) -> list[ScopedMemoryCandidate]:
        for pattern in self.explicit_patterns:
            match = pattern.search(text.strip())
            if match:
                content = match.group(1).strip().rstrip("。")
                kind = self._classify(content)
                scope_type, scope_id = self._scope(kind, metadata)
                return [ScopedMemoryCandidate(MemoryCandidate(content, kind, "explicit"), scope_type, scope_id)]
        return []

    def after_run(
        self,
        user_request: str,
        final_answer: str,
        important_decisions: list[str],
        metadata: dict[str, object],
    ) -> list[ScopedMemoryCandidate]:
        candidates: list[ScopedMemoryCandidate] = []
        if not self.explicit(user_request, metadata):
            kind = self._stable_request_kind(user_request)
            if kind:
                candidates.append(self._candidate(user_request.strip(), kind, "run_final", metadata))
        for decision in important_decisions[:3]:
            candidates.append(
                self._candidate(decision.strip(), MemoryKind.DECISION.value, "run_final", metadata)
            )
        for line in final_answer.splitlines():
            label, separator, content = line.partition(":")
            labels = {
                "decision": MemoryKind.DECISION.value,
                "preference": MemoryKind.PREFERENCE.value,
                "user fact": MemoryKind.USER_FACT.value,
                "project constraint": MemoryKind.PROJECT_CONSTRAINT.value,
            }
            if separator and label.strip().casefold() in labels and content.strip():
                candidates.append(
                    self._candidate(content.strip(), labels[label.strip().casefold()], "run_final", metadata)
                )
        return candidates[:4]

    def _candidate(self, content: str, kind: str, source: str, metadata: dict[str, object]) -> ScopedMemoryCandidate:
        scope_type, scope_id = self._scope(kind, metadata)
        return ScopedMemoryCandidate(MemoryCandidate(content, kind, source), scope_type, scope_id)

    @staticmethod
    def _classify(content: str) -> str:
        text = content.casefold()
        if any(word in text for word in ("prefer", "喜欢", "偏好")):
            return MemoryKind.PREFERENCE.value
        if any(word in text for word in ("project", "must", "项目", "必须", "约束")):
            return MemoryKind.PROJECT_CONSTRAINT.value
        if any(word in text for word in ("decided", "decision", "决定")):
            return MemoryKind.DECISION.value
        return MemoryKind.USER_FACT.value

    def _stable_request_kind(self, text: str) -> str | None:
        lowered = text.casefold()
        if re.search(r"\b(i|we) prefer\b|我(?:喜欢|偏好)", lowered):
            return MemoryKind.PREFERENCE.value
        if re.search(r"\bproject\b.*\b(must|requires?)\b|项目.*(?:必须|要求)", lowered):
            return MemoryKind.PROJECT_CONSTRAINT.value
        if re.search(r"\bwe (?:decided|will use)\b|我们决定", lowered):
            return MemoryKind.DECISION.value
        if re.search(r"\bmy\s+\w+\s+is\b|我的.+是", lowered):
            return MemoryKind.USER_FACT.value
        return None

    @staticmethod
    def _scope(kind: str, metadata: dict[str, object]) -> tuple[str, str]:
        if kind in (MemoryKind.USER_FACT.value, MemoryKind.PREFERENCE.value):
            return MemoryScope.USER.value, str(metadata.get("user_id", "default"))
        return MemoryScope.PROJECT.value, str(metadata.get("project_id", "default"))
