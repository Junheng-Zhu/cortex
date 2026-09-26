"""Deterministic Memory v2 eval; eval output is never connected to MemoryStore."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cortex.context.manager import ContextManager
from cortex.llm.capabilities import ContextMode
from cortex.memory import InMemorySessionStore, InMemoryStore, MemoryManager
from cortex.memory.session_store import SessionEvent
from cortex.runtime.session import Session


def main() -> None:
    semantic = MemoryManager(InMemoryStore())
    semantic.remember(
        "The project database uses SQLite",
        "project",
        "cortex",
        kind="project_constraint",
    )
    episodic = InMemorySessionStore()
    episodic.append(
        SessionEvent(
            "prior",
            "run",
            "assistant",
            "Migration used blue-green rollout",
            metadata={"project_id": "cortex"},
        )
    )
    manager = ContextManager(memory_manager=semantic, session_store=episodic)
    session = Session(metadata={"project_id": "cortex"})

    generic = _context(manager, session, "Write a haiku")
    assert generic[-1]["content"] == "Write a haiku"

    semantic_context = _context(manager, session, "Which project database?")
    assert any("SQLite" in item["content"] for item in semantic_context)

    episodic_context = _context(manager, session, "Continue the previous blue-green migration")
    assert any("Episodic" in item["content"] for item in episodic_context)
    print("MEMORY V2 EVAL: PASSED")


def _context(manager: ContextManager, session: Session, query: str):
    state = type(
        "State",
        (),
        {
            "pending_input": [{"role": "user", "content": query}],
            "context_history": [],
            "previous_response_id": None,
            "compact_summary": "",
        },
    )()
    return manager.build_context(state, ContextMode.CLIENT_MANAGED, session)[0]


if __name__ == "__main__":
    main()
