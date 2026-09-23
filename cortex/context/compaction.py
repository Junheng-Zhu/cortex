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
        self, items: list[dict[str, Any]], previous_summary: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        if len(items) <= self.recent_items:
            return items, previous_summary
        old, recent = items[: -self.recent_items], items[-self.recent_items :]
        fragments = [previous_summary] if previous_summary else []
        fragments.extend(self._text(item) for item in old)
        summary = "\n".join(fragments)[-self.summary_chars :]
        summary_item = {
            "role": "system",
            "content": "Compact summary of earlier session context:\n" + summary,
        }
        return [summary_item, *recent], summary
