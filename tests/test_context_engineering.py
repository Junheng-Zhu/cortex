from pathlib import Path

from cortex.runtime.loop import AgentLoop
from cortex.llm.protocol import LLMResponse, ToolCall
from cortex.context.artifacts import ArtifactStore
from cortex.context.manager import ContextManager
from cortex.context.artifacts import ObservationPolicy
from cortex.llm.capabilities import ContextMode
from cortex.tools.builtin.artifacts import ReadArtifactChunkTool
from cortex.tools.executor import ToolResult
from cortex.tools.builtin.artifacts import ReadArtifactChunkInput


class LargeResultExecutor:
    def list_tool_schemas(self):
        return [{"type": "function", "name": "large"}]

    def execute(self, name, arguments):
        return ToolResult(name, 1, 0, True, None, None, "界" * 100)


class ScriptedLLM:
    def __init__(self):
        self.requests = []

    def respond(self, **request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return LLMResponse(
                tool_calls=[ToolCall("large-1", "large", {})],
                output_items=[
                    {
                        "type": "function_call",
                        "call_id": "large-1",
                        "name": "large",
                        "arguments": {},
                    }
                ],
            )
        return LLMResponse(content="done")


def test_large_observation_is_externalized_and_can_be_paged(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    llm = ScriptedLLM()
    loop = AgentLoop(
        llm,
        LargeResultExecutor(),
        artifact_store=store,
        observation_policy=ObservationPolicy(store, inline_limit=20, preview_chars=8),
    )

    assert loop.run("get it") == "done"
    observation = loop.last_state.observations[0]
    assert observation.preview == "界" * 8
    assert observation.size_chars == 100
    assert observation.truncated is True
    assert Path(observation.artifact_path).read_text(encoding="utf-8") == "界" * 100
    assert "Use read_artifact_chunk" in llm.requests[1]["input"][-1]["output"]

    page = ReadArtifactChunkTool(store).execute(
        ReadArtifactChunkInput(artifact_id=observation.artifact_ref, offset=9, limit=7)
    )
    assert page["content"] == "界" * 7
    assert page["next_offset"] == 16
    assert page["eof"] is False


def test_small_observation_stays_inline(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    observation = ObservationPolicy(store, inline_limit=20).success("a", "echo", "ok")

    assert observation.output == "ok"
    assert observation.artifact_ref is None
    assert list(tmp_path.iterdir()) == []


def test_context_manager_compacts_old_client_managed_items():
    class State:
        context_history = [{"role": "user", "content": str(i) * 20} for i in range(12)]
        pending_input = [{"role": "user", "content": "latest"}]
        previous_response_id = None
        compact_summary = ""

    state = State()
    context, cursor = ContextManager(max_chars=10).build_context(
        state, ContextMode.CLIENT_MANAGED
    )

    assert cursor is None
    assert context[0]["content"].startswith("Compact summary")
    assert context[-1]["content"] == "latest"
    assert state.compact_summary
