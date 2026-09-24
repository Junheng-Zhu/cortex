from dataclasses import dataclass, field
from typing import Any

from cortex.llm.capabilities import ContextMode
from cortex.memory.manager import MemoryManager
from cortex.memory.scopes import MemoryScope

from .compaction import ContextCompactor
from .budget import ContextBudget


@dataclass
class ContextManager:
    """Build the session working set supplied to each model request."""

    max_chars: int = 60_000
    compactor: ContextCompactor = field(default_factory=ContextCompactor)
    memory_manager: MemoryManager | None = None
    memory_limit: int = 3

    def build_context(
        self,
        state: Any,
        mode: ContextMode = ContextMode.CLIENT_MANAGED,
        session: Any | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        memory_context = self._memory_context(state, session)
        if mode is ContextMode.SERVER_MANAGED:
            cursor = session.previous_response_id if session is not None else state.previous_response_id
            return [*memory_context, *state.pending_input], cursor

        items = [*memory_context, *state.context_history, *state.pending_input]
        if ContextBudget(self.max_chars).exceeded(items):
            summary = session.compact_summary if session is not None else state.compact_summary
            items, summary = self.compactor.compact(
                items, summary
            )
            state.compact_summary = summary
            if session is not None:
                session.compact_summary = summary
        return items, None

    def _memory_context(self, state: Any, session: Any | None) -> list[dict[str, Any]]:
        if self.memory_manager is None or session is None or session.temporary_chat:
            return []
        user_items = [item for item in state.pending_input if item.get("role") == "user"]
        if not user_items:
            return []
        query = " ".join(str(item.get("content", "")) for item in user_items)
        scopes = [(MemoryScope.SESSION.value, session.session_id)]
        for scope, key in ((MemoryScope.USER.value, "user_id"), (MemoryScope.PROJECT.value, "project_id")):
            if session.metadata.get(key):
                scopes.append((scope, str(session.metadata[key])))
        records = []
        for scope_type, scope_id in scopes:
            records.extend(self.memory_manager.recall(query, scope_type, scope_id, self.memory_limit))
        records = records[: self.memory_limit]
        if not records:
            return []
        content = "Relevant long-term memory (use only when applicable):\n" + "\n".join(
            f"- {record.content}" for record in records
        )
        return [{"role": "system", "content": content, "name": "cortex_memory"}]
