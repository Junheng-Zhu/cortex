from dataclasses import dataclass, field
from typing import Any

from cortex.llm.capabilities import ContextMode
from cortex.memory.manager import MemoryManager
from cortex.memory.retrieval_gate import MemoryRetrievalGate
from cortex.memory.scopes import MemoryScope
from cortex.memory.session_store import SessionStore

from .compaction import ContextCompactor
from .budget import ContextBudget


@dataclass
class ContextManager:
    """Build the session working set supplied to each model request."""

    max_chars: int = 60_000
    compactor: ContextCompactor = field(default_factory=ContextCompactor)
    memory_manager: MemoryManager | None = None
    session_store: SessionStore | None = None
    retrieval_gate: MemoryRetrievalGate = field(default_factory=MemoryRetrievalGate)
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
            items, summary = self.compactor.compact(items, summary, self._structure(state))
            state.compact_summary = summary
            if session is not None:
                session.compact_summary = summary
        return items, None

    def _memory_context(self, state: Any, session: Any | None) -> list[dict[str, Any]]:
        if session is None or session.temporary_chat:
            return []
        user_items = [item for item in state.pending_input if item.get("role") == "user"]
        if not user_items:
            return []
        query = " ".join(str(item.get("content", "")) for item in user_items)
        plan = self.retrieval_gate.plan(query)
        snippets: list[str] = []
        if plan.search_episodic and self.session_store is not None:
            events = self.session_store.search(
                query,
                self.memory_limit,
                user_id=(str(session.metadata["user_id"]) if "user_id" in session.metadata else None),
                project_id=(str(session.metadata["project_id"]) if "project_id" in session.metadata else None),
            )
            snippets.extend(
                f"Episodic ({event.event_type}, session {event.session_id}): {event.content}"
                for event in events
            )
        if self.memory_manager is not None:
            for scope in plan.semantic_scopes:
                key = "user_id" if scope is MemoryScope.USER else "project_id"
                scope_id = str(session.metadata.get(key, "default"))
                records = self.memory_manager.recall(query, scope.value, scope_id, self.memory_limit)
                snippets.extend(f"Semantic ({scope.value}): {record.content}" for record in records)
        snippets = snippets[: self.memory_limit]
        if not snippets:
            return []
        content = "Relevant long-term memory (use only when applicable):\n" + "\n".join(
            f"- {snippet}" for snippet in snippets
        )
        return [{"role": "system", "content": content, "name": "cortex_memory"}]

    @staticmethod
    def _structure(state: Any) -> dict[str, Any]:
        actions = getattr(state, "actions", [])
        observations = getattr(state, "observations", [])
        pending_actions = getattr(state, "pending_actions", [])
        return {
            "goal": getattr(state, "current_goal", None),
            "completed": [action.tool_name for action in actions if action.status == "SUCCEEDED"],
            "decisions": getattr(state, "important_decisions", []),
            "important_files": getattr(state, "artifact_references", []),
            "errors": [observation.error for observation in observations if observation.error],
            "pending": [action.tool_name for action in pending_actions],
        }
