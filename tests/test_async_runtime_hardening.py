import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.execution import DockerBackend, DockerBackendConfig, ExecutionRequest
from cortex.llm.protocol import LLMResponse, ToolCall
from cortex.runtime.action import Action
from cortex.runtime.checkpoint import Checkpoint, InMemoryCheckpointStore
from cortex.runtime.loop import AgentLoop
from cortex.runtime.state import AgentPhase, AgentState
from cortex.tools.base import ConcurrencyPolicy, ExecutionStrategy
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry
from tests.test_async_tool_runtime import AsyncProbeTool, DelayInput, SerialProbeTool


class SideEffectTool(SerialProbeTool):
    name = "side_effect"

    def __init__(self):
        self.calls = 0

    async def aexecute(self, input):
        self.calls += 1
        return input.value


def runtime_with(*tools, limit=4):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolExecutor({Permission.READ}, registry, max_tool_concurrency=limit)


@pytest.mark.asyncio
async def test_failed_parallel_wave_stops_later_serial_side_effect():
    side_effect = SideEffectTool()
    agent = AgentLoop(object(), runtime_with(AsyncProbeTool(), side_effect))
    state = AgentState(max_steps=5, phase=AgentPhase.ACT, pending_actions=[
        Action("probe", {"fail": True}, call_id="failed"),
        Action("side_effect", {"value": "must-not-run"}, call_id="side"),
    ])
    state.actions = list(state.pending_actions)
    await agent.aact(state)
    assert side_effect.calls == 0
    assert state.phase is AgentPhase.FINAL  # RuntimeError reflects as ABORT


@pytest.mark.asyncio
async def test_unknown_tool_is_observed_and_replanned_in_arun():
    class Client:
        calls = 0

        def respond(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall("missing-call", "missing", {})])
            return LLMResponse(content="recovered")

    agent = AgentLoop(Client(), runtime_with(AsyncProbeTool()))
    assert await agent.arun("unknown") == "recovered"
    assert agent.last_state.observations[0].error_type == "ToolNotFoundError"
    assert agent.last_state.reflections[0].status == "REPLAN"


@pytest.mark.asyncio
async def test_async_batch_never_exceeds_remaining_step_budget():
    tool = SideEffectTool()
    agent = AgentLoop(object(), runtime_with(tool))
    actions = [Action("side_effect", {"value": str(i)}, call_id=str(i))
               for i in range(5)]
    state = AgentState(max_steps=10, step_count=9, phase=AgentPhase.ACT,
                       actions=list(actions), pending_actions=list(actions))
    await agent.aact(state)
    assert tool.calls == 1
    assert state.step_count == 10
    assert state.phase is AgentPhase.FINAL


@pytest.mark.asyncio
async def test_checkpoint_restores_completed_observation_without_reexecution():
    tool = SideEffectTool()
    store = InMemoryCheckpointStore()
    agent = AgentLoop(object(), runtime_with(tool), checkpoint_store=store)
    action = Action("side_effect", {"value": "durable"}, call_id="durable-call")
    state = AgentState(run_id="run", session_id=agent.session.session_id,
                       max_steps=3, phase=AgentPhase.ACT,
                       actions=[action], pending_actions=[action])
    await agent.aact(state)
    restored = store.latest(state.session_id).restore(new_run_id="resumed", max_steps=3)
    assert tool.calls == 1
    assert not restored.pending_actions
    assert restored.actions[0].status == "SUCCEEDED"
    assert restored.observations[0].output == "durable"
    assert restored.pending_input[0]["call_id"] == "durable-call"


class TrackingTool(AsyncProbeTool):
    def __init__(self, policy=ConcurrencyPolicy.PARALLEL_SAFE):
        self.concurrency_policy = policy
        self.active = 0
        self.peak = 0

    async def aexecute(self, input: DelayInput):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(input.delay)
            return input.value
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_concurrent_batches_share_executor_concurrency_limit():
    tool = TrackingTool()
    runtime = runtime_with(tool, limit=2)
    calls = [("probe", {"delay": 0.05})] * 3
    await asyncio.gather(runtime.aexecute_many(calls), runtime.aexecute_many(calls))
    assert tool.peak == 2


@pytest.mark.asyncio
async def test_serial_tool_never_overlaps_across_concurrent_batches():
    tool = TrackingTool(ConcurrencyPolicy.SERIAL)
    runtime = runtime_with(tool, limit=4)
    await asyncio.gather(
        runtime.aexecute_many([("probe", {"delay": 0.05})]),
        runtime.aexecute_many([("probe", {"delay": 0.05})]),
    )
    assert tool.peak == 1


class RacingContainer:
    def __init__(self, ident, blocking=False):
        self.id = ident
        self.blocking = blocking
        self.removed = False
        self.release = threading.Event()

    def exec_run(self, *_args, **_kwargs):
        if self.blocking:
            self.release.wait(2)
            raise RuntimeError("old exec stopped")
        return SimpleNamespace(exit_code=0, output=(b"new", b""))

    def remove(self, force=False):
        self.removed = force
        self.release.set()


@pytest.mark.asyncio
async def test_docker_stale_cancelled_execution_cannot_remove_recreated_container(tmp_path):
    old = RacingContainer("old", blocking=True)
    new = RacingContainer("new")
    containers = SimpleNamespace(run=lambda *_args, **_kwargs: old)
    client = SimpleNamespace(containers=containers)
    backend = DockerBackend(DockerBackendConfig(workspace=tmp_path), client=client)
    task = asyncio.create_task(backend.aexecute(
        ExecutionRequest("sleep", Path("."), 10)
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    containers.run = lambda *_args, **_kwargs: new
    result = await backend.aexecute(ExecutionRequest("echo new", Path("."), 1))
    await asyncio.sleep(0.05)  # let the stale SDK thread finish its exception path
    assert result.stdout == "new"
    assert backend._container is new
    assert not new.removed
