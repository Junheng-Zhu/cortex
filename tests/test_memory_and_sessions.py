from pathlib import Path

from cortex.context.manager import ContextManager
from cortex.llm.capabilities import ContextMode
from cortex.llm.protocol import LLMResponse
from cortex.memory import (
    InMemoryStore,
    MemoryDecision,
    MemoryKind,
    MemoryManager,
    MemoryWritePolicy,
    SQLiteMemoryStore,
)
from cortex.memory.policy import MemoryCandidate
from cortex.runtime.loop import AgentLoop
from cortex.runtime.session import Session, SessionConfig


class NoTools:
    def list_tool_schemas(self):
        return []


class CapturingLLM:
    def __init__(self, mode=ContextMode.SERVER_MANAGED):
        self.capabilities = type("Capabilities", (), {"context_mode": mode})()
        self.requests = []

    def respond(self, **request):
        self.requests.append(request)
        return LLMResponse(response_id=f"response-{len(self.requests)}", content="ok")


def test_session_owns_cursor_and_each_run_gets_fresh_state():
    session = Session()
    llm = CapturingLLM()
    agent = AgentLoop(llm, NoTools(), session=session)

    agent.run("first")
    first_state = agent.last_state
    agent.run("second")

    assert first_state is not agent.last_state
    assert first_state.run_id != agent.last_state.run_id
    assert first_state.session_id == agent.last_state.session_id == session.session_id
    assert llm.requests[1]["previous_response_id"] == "response-1"
    assert session.previous_response_id == "response-2"
    assert session.last_run_id == agent.last_state.run_id


def test_policy_rejects_transient_sources_and_temporary_chat_blocks_writes():
    policy = MemoryWritePolicy()
    for source in ("pytest", "eval", "tool_output", "trace", "temporary", "stack_trace"):
        assert policy.evaluate(MemoryCandidate("noise", MemoryKind.DECISION.value, source)) is MemoryDecision.DENY

    manager = MemoryManager(InMemoryStore(), policy)
    assert manager.remember("Use Python 3.12", "project", "cortex", kind="project_constraint", temporary_chat=True) is None
    assert manager.store.list("project", "cortex") == []


def test_sqlite_store_persists_and_deduplicates_exact_scope_content(tmp_path: Path):
    path = tmp_path / ".cortex" / "memory.db"
    manager = MemoryManager(SQLiteMemoryStore(path))
    first = manager.remember("Prefer concise output", "user", "u1", kind="preference")
    second = manager.remember("Prefer concise output", "user", "u1", kind="preference")

    assert first is not None
    assert second is not None and second.memory_id == first.memory_id
    assert len(SQLiteMemoryStore(path).list("user", "u1")) == 1


def test_context_injects_lexical_memory_for_matching_scopes_only():
    manager = MemoryManager(InMemoryStore())
    manager.remember("The project database is SQLite", "project", "p1", kind="project_constraint")
    manager.remember("The project database is Postgres", "project", "p2", kind="project_constraint")
    session = Session(metadata={"project_id": "p1"})
    state = type("State", (), {
        "pending_input": [{"role": "user", "content": "Which database?"}],
        "context_history": [], "previous_response_id": None, "compact_summary": "",
    })()

    context, _ = ContextManager(memory_manager=manager).build_context(state, ContextMode.CLIENT_MANAGED, session)

    assert "SQLite" in context[0]["content"]
    assert "Postgres" not in context[0]["content"]


def test_temporary_session_does_not_recall_durable_memory():
    manager = MemoryManager(InMemoryStore())
    manager.remember("User prefers dark mode", "user", "u1", kind="preference")
    session = Session.from_config(SessionConfig(temporary_chat=True), metadata={"user_id": "u1"})
    state = type("State", (), {
        "pending_input": [{"role": "user", "content": "dark mode"}],
        "context_history": [], "previous_response_id": None, "compact_summary": "",
    })()

    context, _ = ContextManager(memory_manager=manager).build_context(state, ContextMode.CLIENT_MANAGED, session)
    assert context == state.pending_input
