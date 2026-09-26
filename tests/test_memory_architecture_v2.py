from pathlib import Path

from cortex.context.compaction import ContextCompactor
from cortex.context.manager import ContextManager
from cortex.llm.capabilities import ContextMode
from cortex.memory import (
    InMemorySessionStore,
    InMemoryStore,
    MemoryKind,
    MemoryManager,
    MemoryRetrievalGate,
    SQLiteSessionStore,
)
from cortex.memory.session_store import SessionEvent
from cortex.runtime.action import Action
from cortex.runtime.checkpoint import Checkpoint
from cortex.runtime.checkpoint import InMemoryCheckpointStore
from cortex.runtime.checkpoint import SQLiteCheckpointStore
from cortex.runtime.loop import AgentLoop
from cortex.runtime.session import Session
from cortex.runtime.state import AgentPhase, AgentState
from cortex.llm.protocol import LLMResponse, ToolCall
from cortex.tools.executor import ToolResult


def test_session_store_restores_session_and_searches_raw_episodes(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    session = Session(
        compact_summary="Goal: ship release",
        artifact_refs=["artifact://abc"],
        history=[{"role": "system", "content": "compacted"}],
        metadata={"project_id": "p1"},
    )
    store.append(SessionEvent(session.session_id, "r1", "user", "We selected SQLite for storage"))
    store.save(session)

    restored = SQLiteSessionStore(tmp_path / "sessions.db").load(session.session_id)

    assert restored is not None
    assert restored.compact_summary == "Goal: ship release"
    assert restored.artifact_refs == ["artifact://abc"]
    assert store.search("previous SQLite decision")[0].content == "We selected SQLite for storage"


def test_retrieval_gate_does_not_recall_on_unrelated_requests():
    manager = MemoryManager(InMemoryStore())
    manager.remember("Use SQLite", "project", "p1", kind="project_constraint")
    session = Session(metadata={"project_id": "p1"})
    state = type(
        "State",
        (),
        {
            "pending_input": [{"role": "user", "content": "Write a haiku"}],
            "context_history": [],
            "previous_response_id": None,
            "compact_summary": "",
        },
    )()

    context, _ = ContextManager(memory_manager=manager).build_context(
        state, ContextMode.CLIENT_MANAGED, session
    )

    assert context[-1:] == state.pending_input
    assert context[0]["name"] == "cortex_memory_runtime"
    assert "persistent memory capabilities" in context[0]["content"]
    assert MemoryRetrievalGate().plan("continue what we did last time").search_episodic


def test_episodic_retrieval_is_prioritized_for_history_language():
    store = InMemorySessionStore()
    store.append(SessionEvent("old", "r1", "assistant", "The migration used blue-green deployment"))
    session = Session()
    state = type(
        "State",
        (),
        {
            "pending_input": [{"role": "user", "content": "Continue the previous blue-green migration"}],
            "context_history": [],
            "previous_response_id": None,
            "compact_summary": "",
        },
    )()

    context, _ = ContextManager(session_store=store).build_context(
        state, ContextMode.CLIENT_MANAGED, session
    )

    memory_item = next(item for item in context if item.get("name") == "cortex_memory")
    assert "Episodic" in memory_item["content"]
    assert "blue-green" in memory_item["content"]
    assert "Do not call unrelated filesystem" in memory_item["content"]


def test_episodic_search_is_scope_isolated():
    store = InMemorySessionStore()
    store.append(
        SessionEvent(
            "other",
            "r1",
            "assistant",
            "Previous deployment used the private red cluster",
            metadata={"project_id": "p2"},
        )
    )
    store.append(
        SessionEvent(
            "mine",
            "r2",
            "assistant",
            "Previous deployment used the blue cluster",
            metadata={"project_id": "p1"},
        )
    )

    results = store.search("previous deployment cluster", project_id="p1")

    assert [event.session_id for event in results] == ["mine"]


def test_episodic_search_prefers_user_source_over_assistant_source():
    store = InMemorySessionStore()
    timestamp = SessionEvent("s1", "r1", "assistant", "database is SQLite").created_at
    store.append(
        SessionEvent(
            "s1", "r1", "assistant", "database is SQLite", created_at=timestamp
        )
    )
    store.append(
        SessionEvent(
            "s2", "r2", "user", "my database is Postgres", created_at=timestamp
        )
    )

    results = store.search("database", limit=2)

    assert [event.event_type for event in results] == ["user", "assistant"]


def test_checkpoint_restore_never_requeues_completed_actions():
    completed = Action("write_file", {"path": "done"}, status="SUCCEEDED")
    pending = Action("notify", {}, status="PENDING")
    state = AgentState(
        run_id="run-1",
        session_id="session-1",
        phase=AgentPhase.REFLECT,
        actions=[completed, pending],
        pending_actions=[pending],
        current_goal="deploy",
    )

    restored = Checkpoint.capture(state).restore(new_run_id="run-2", max_steps=10)

    assert [action.tool_name for action in restored.pending_actions] == ["notify"]
    assert restored.actions[0].tool_name == "write_file"
    assert restored.actions[0] not in restored.pending_actions
    assert restored.phase is AgentPhase.ACT


def test_sqlite_checkpoint_store_round_trip(tmp_path: Path):
    state = AgentState(
        run_id="run-1",
        session_id="session-1",
        phase=AgentPhase.REFLECT,
        current_goal="release",
        current_plan=["test", "deploy"],
        important_decisions=["use canary"],
    )
    checkpoint = Checkpoint.capture(state)
    store = SQLiteCheckpointStore(tmp_path / "checkpoints.db")
    store.save(checkpoint)

    restored = SQLiteCheckpointStore(tmp_path / "checkpoints.db").latest("session-1")

    assert restored == checkpoint
    assert restored.phase == "REFLECT"


def test_structured_compaction_keeps_required_sections():
    items = [{"role": "user", "content": f"message {index}"} for index in range(12)]
    compacted, summary = ContextCompactor(recent_items=2).compact(
        items,
        structure={
            "goal": "release",
            "completed": ["tests"],
            "decisions": ["SQLite"],
            "important_files": ["app.py"],
            "errors": ["timeout"],
            "pending": ["deploy"],
        },
    )

    for heading in ("Goal:", "Completed:", "Decisions:", "Important Files:", "Errors:", "Pending:"):
        assert heading in summary
    assert len(compacted) == 3


def test_memory_policy_rejects_noise_even_when_source_is_mislabeled():
    manager = MemoryManager(InMemoryStore())

    record = manager.remember(
        "pytest output: 10 passed; latency_ms=3",
        "project",
        "p1",
        kind=MemoryKind.DECISION.value,
        source="run_final",
    )

    assert record is None
    assert manager.store.list("project", "p1") == []


def test_loop_consolidates_stable_fact_and_persists_safe_tool_checkpoint():
    class LLM:
        def __init__(self):
            self.calls = 0

        def respond(self, **request):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall("call-1", "write", {})])
            return LLMResponse(content="done")

    class Executor:
        def list_tool_schemas(self):
            return []

        def execute(self, name, arguments):
            return ToolResult(name, 1, 0, True, None, None, "secret stdout")

    semantic_store = InMemoryStore()
    episodes = InMemorySessionStore()
    checkpoints = InMemoryCheckpointStore()
    session = Session(metadata={"user_id": "u1"})
    loop = AgentLoop(
        LLM(),
        Executor(),
        session=session,
        memory_manager=MemoryManager(semantic_store),
        session_store=episodes,
        checkpoint_store=checkpoints,
    )

    assert loop.run("I prefer compact answers") == "done"

    assert [record.content for record in semantic_store.list("user", "u1")] == [
        "I prefer compact answers"
    ]
    assert checkpoints.latest(session.session_id) is not None
    assert "secret stdout" not in " ".join(event.content for event in episodes.events(session.session_id))
    assert [event.event_type for event in episodes.events(session.session_id)] == [
        "user",
        "tool",
        "assistant",
    ]
    assert not any(
        str(item.get("name", "")).startswith("cortex_memory")
        for item in loop.last_state.context_history
    )


def test_temporary_loop_keeps_continuity_without_any_durable_memory():
    class LLM:
        def __init__(self):
            self.requests = []

        def respond(self, **request):
            self.requests.append(request)
            return LLMResponse(response_id=f"r{len(self.requests)}", content="ok")

    class Executor:
        def list_tool_schemas(self):
            return []

    semantic = InMemoryStore()
    episodes = InMemorySessionStore()
    checkpoints = InMemoryCheckpointStore()
    session = Session(temporary_chat=True, metadata={"user_id": "u1"})
    llm = LLM()
    loop = AgentLoop(
        llm,
        Executor(),
        session=session,
        memory_manager=MemoryManager(semantic),
        session_store=episodes,
        checkpoint_store=checkpoints,
        context_mode=ContextMode.SERVER_MANAGED,
    )

    loop.run("Remember that I prefer dark mode")
    loop.run("hello again")

    assert llm.requests[1]["previous_response_id"] == "r1"
    assert semantic.list("user", "u1") == []
    assert episodes.load(session.session_id) is None
    assert checkpoints.latest(session.session_id) is None
