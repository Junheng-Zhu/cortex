import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ContextCompactor:
    """Deterministically condense old request items into a bounded summary."""

    summary_chars: int = 3_000
    recent_items: int = 8

    @staticmethod
    def _text(item: dict[str, Any]) -> str:
        try:
            return json.dumps(item, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(item)

    def compact(
        self,
        items: list[dict[str, Any]],
        previous_summary: str = "",
        structure: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        recent_count = min(self.recent_items, max(0, len(items) - 1))
        if recent_count:
            old, recent = items[:-recent_count], items[-recent_count:]
        else:
            old, recent = items, []
        structure = structure or {}
        transcript = "\n".join(self._text(item) for item in old)
        sections = {
            "Goal": structure.get("goal") or "Not specified",
            "Completed": structure.get("completed") or [],
            "Decisions": structure.get("decisions") or [],
            "Important Files": structure.get("important_files") or [],
            "Errors": structure.get("errors") or [],
            "Pending": structure.get("pending") or [],
        }
        lines = []
        if previous_summary:
            lines.append(previous_summary)
        for title, value in sections.items():
            rendered = value if isinstance(value, str) else "; ".join(map(str, value))
            lines.append(f"{title}: {rendered or 'None'}")
        prefix = "\n".join(lines) + "\nEarlier Context: "
        remaining = max(0, self.summary_chars - len(prefix))
        summary = prefix + transcript[-remaining:] if remaining else prefix[: self.summary_chars]
        summary_item = {
            "role": "system",
            "content": "Compact summary of earlier session context:\n" + summary,
        }
        return [summary_item, *recent], summary
