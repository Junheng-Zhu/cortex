from dataclasses import dataclass, field
from typing import Any

from src.core.models import ContextMode

from .compactor import ContextCompactor


@dataclass
class ContextManager:
    """Build the session working set supplied to each model request."""

    max_chars: int = 60_000
    compactor: ContextCompactor = field(default_factory=ContextCompactor)

    def build_context(
        self, state: Any, mode: ContextMode = ContextMode.CLIENT_MANAGED
    ) -> tuple[list[dict[str, Any]], str | None]:
        if mode is ContextMode.SERVER_MANAGED:
            return list(state.pending_input), state.previous_response_id

        items = [*state.context_history, *state.pending_input]
        if sum(len(str(item)) for item in items) > self.max_chars:
            items, state.compact_summary = self.compactor.compact(
                items, state.compact_summary
            )
        return items, None
