import asyncio
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from cortex.app.bootstrap import build_agent
from cortex.llm.protocol import LLMResponse, ToolCall
from cortex.runtime.checkpoint import InMemoryCheckpointStore
from cortex.runtime.execution_checkpoint import (
    InMemoryExecutionCheckpointStore, WorkspaceRecoveryRuntime,
)
from cortex.runtime.loop import AgentLoop
from cortex.runtime.workspace import ShadowGitSnapshotStore, WorkspaceOperation
from cortex.tools.base import (
    ConcurrencyPolicy, ExecutionStrategy, SideEffectPolicy, Tool,
)
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def respond(self, **_request):
        return next(self.responses)


def call(name, arguments):
    return LLMResponse(tool_calls=[ToolCall("call", name, arguments)])


def shell_agent(tmp_path: Path, command: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = InMemoryCheckpointStore()
    executions = InMemoryExecutionCheckpointStore()
    client = ScriptedClient(call("shell", {"command": command}),
                            LLMResponse(content="done"))
    agent = build_agent(
        client, execution_workspace=workspace, skills_enabled=False,
        checkpoint_store=agents, execution_checkpoint_store=executions,
        workspace_snapshot_root=tmp_path / "snapshots",
    )
    return agent, agent.workspace_recovery_runtime, executions, workspace


@pytest.mark.parametrize(
    ("initial", "command", "operation", "expected"),
    [
        ("old", "printf new > target", WorkspaceOperation.MODIFIED, "old"),
        ("old", "python -c \"from pathlib import Path; Path('target').unlink()\"",
         WorkspaceOperation.DELETED, "old"),
        (None, "printf new > target", WorkspaceOperation.CREATED, None),
    ],
)
def test_sync_shell_automatically_tracks_and_rolls_back(
    tmp_path, initial, command, operation, expected,
):
    agent, recovery, executions, workspace = shell_agent(tmp_path, command)
    if initial is not None:
        (workspace / "target").write_text(initial)

    assert agent.run("mutate") == "done"

    assert recovery.ledger.records[-1].operation is operation
    event = next(item for item in agent.recorder.events
                 if item.event_type == "workspace_mutation")
    assert event.data["action_id"] == recovery.ledger.records[-1].action_id
    assert event.data["changed_file_count"] == 1
    assert event.data[f"{operation.value.lower()}_count"] == 1
    pre = next(item for item in executions.checkpoints
               if item.reason == "before_mutation")
    restored = recovery.rollback(pre.execution_checkpoint_id)
    assert bool((workspace / "target").exists()) is (expected is not None)
    if expected is not None:
        assert (workspace / "target").read_text() == expected
    assert not restored.observations


class MutateInput(BaseModel):
    path: str
    delay: float = 0.0
    fail: bool = False


class MetadataMutatingTool(Tool):
    name = "metadata_mutator"
    description = "mutates a test workspace"
    input_model = MutateInput
    permission = Permission.WRITE
    timeout = 2
    max_retries = 2
    retryable = True
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE
    side_effect_policy = SideEffectPolicy.WORKSPACE_REVERSIBLE

    def __init__(self, workspace: Path, activity=None):
        self.workspace = workspace
        self.activity = activity

    def execute(self, input):
        if self.activity:
            self.activity["active"] += 1
            self.activity["peak"] = max(self.activity["peak"], self.activity["active"])
        try:
            time.sleep(input.delay)
            (self.workspace / input.path).write_text("changed")
            if input.fail:
                raise RuntimeError("failed after write")
            return "changed"
        finally:
            if self.activity:
                self.activity["active"] -= 1

    async def aexecute(self, input):
        if self.activity:
            self.activity["active"] += 1
            self.activity["peak"] = max(self.activity["peak"], self.activity["active"])
        try:
            await asyncio.sleep(input.delay)
            (self.workspace / input.path).write_text("changed")
            if input.fail:
                raise RuntimeError("failed after write")
            return "changed"
        finally:
            if self.activity:
                self.activity["active"] -= 1


class ReadOnlyProbeTool(Tool):
    name = "read_probe"
    description = "read-only async probe"
    input_model = MutateInput
    permission = Permission.READ
    timeout = 2
    max_retries = 0
    retryable = False
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE
    side_effect_policy = SideEffectPolicy.NONE

    def execute(self, input):
        time.sleep(input.delay)
        return input.path

    async def aexecute(self, input):
        await asyncio.sleep(input.delay)
        return input.path

def recovery_executor(tmp_path, tool, *, recovery=None):
    registry = ToolRegistry()
    registry.register(tool)
    if recovery is None:
        recovery = WorkspaceRecoveryRuntime(
            ShadowGitSnapshotStore(tool.workspace, tmp_path / "snapshots"),
            InMemoryCheckpointStore(), InMemoryExecutionCheckpointStore(),
        )
    return ToolExecutor(set(Permission), registry, recovery_runtime=recovery), recovery


@pytest.mark.asyncio
async def test_async_metadata_driven_tool_is_automatically_recovered(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, recovery = recovery_executor(
        tmp_path, MetadataMutatingTool(workspace)
    )
    client = ScriptedClient(
        call("metadata_mutator", {"path": "created", "delay": 0}),
        LLMResponse(content="done"),
    )

    assert await AgentLoop(client, executor).arun("mutate") == "done"
    assert (workspace / "created").read_text() == "changed"
    assert recovery.ledger.records[-1].operation is WorkspaceOperation.CREATED
    assert recovery.ledger.records[-1].wave_id
    committed = recovery.execution_checkpoints.load(
        recovery.ledger.records[-1].execution_checkpoint_id
    )
    logical = recovery.agent_checkpoints.load(committed.agent_checkpoint_id)
    assert logical.observations[-1]["success"] is True


@pytest.mark.asyncio
async def test_read_only_parallel_wave_creates_no_snapshots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshots = ShadowGitSnapshotStore(workspace, tmp_path / "snapshots")
    recovery = WorkspaceRecoveryRuntime(
        snapshots, InMemoryCheckpointStore(), InMemoryExecutionCheckpointStore()
    )
    registry = ToolRegistry()
    registry.register(ReadOnlyProbeTool())
    executor = ToolExecutor({Permission.READ}, registry, recovery_runtime=recovery)

    results = await executor.aexecute_many([
        ("read_probe", {"delay": 0.08, "path": "a"}),
        ("read_probe", {"delay": 0.08, "path": "b"}),
    ])

    assert [item.data for item in results] == ["a", "b"]
    assert executor.last_batch_metrics["peak_concurrency"] == 2
    assert not recovery.execution_checkpoints.checkpoints


@pytest.mark.asyncio
async def test_same_workspace_mutations_serialize_across_async_runs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    activity = {"active": 0, "peak": 0}
    tool = MetadataMutatingTool(workspace, activity)
    executor, recovery = recovery_executor(tmp_path, tool)
    agents = [
        AgentLoop(ScriptedClient(call(tool.name, {"path": f"f{i}", "delay": 0.08}),
                                 LLMResponse(content="done")), executor)
        for i in range(2)
    ]

    await asyncio.gather(*(agent.arun("mutate") for agent in agents))

    assert activity["peak"] == 1
    assert len(recovery.ledger.records) == 2


@pytest.mark.asyncio
async def test_different_workspaces_do_not_share_mutation_lock(tmp_path):
    activity = {"active": 0, "peak": 0}
    agents = []
    for index in range(2):
        workspace = tmp_path / f"workspace-{index}"
        workspace.mkdir()
        tool = MetadataMutatingTool(workspace, activity)
        executor, _recovery = recovery_executor(tmp_path / f"runtime-{index}", tool)
        agents.append(AgentLoop(
            ScriptedClient(call(tool.name, {"path": "f", "delay": 0.1}),
                           LLMResponse(content="done")), executor,
        ))

    await asyncio.gather(*(agent.arun("mutate") for agent in agents))
    assert activity["peak"] == 2


@pytest.mark.asyncio
async def test_failure_and_cancellation_leave_durable_physical_checkpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = MetadataMutatingTool(workspace)
    executor, recovery = recovery_executor(tmp_path, tool)
    failed = AgentLoop(
        ScriptedClient(call(tool.name, {"path": "failed", "fail": True}),
                       LLMResponse(content="unused")), executor,
    )
    await failed.arun("fail")
    latest = recovery.execution_checkpoints.latest(failed.last_state.session_id)
    assert latest.reason == "committed_wave"
    assert recovery.snapshots.load(latest.workspace_snapshot_id).manifest_hash
    assert len(recovery.ledger.records) == 1  # reversible retries are disabled

    cancelling = AgentLoop(
        ScriptedClient(call(tool.name, {"path": "cancelled", "delay": 10})), executor,
    )
    task = asyncio.create_task(cancelling.arun("cancel"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = recovery.execution_checkpoints.latest(
        cancelling.last_state.session_id
    )
    assert cancelled.reason == "cancelled_mutation"
    snapshot = recovery.snapshots.load(cancelled.workspace_snapshot_id)
    assert snapshot.manifest_hash == recovery.snapshots.snapshot().manifest_hash


def test_permission_and_validation_failures_do_not_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = MetadataMutatingTool(workspace)
    registry = ToolRegistry()
    registry.register(tool)
    recovery = WorkspaceRecoveryRuntime(
        ShadowGitSnapshotStore(workspace, tmp_path / "snapshots"),
        InMemoryCheckpointStore(), InMemoryExecutionCheckpointStore(),
    )
    denied = ToolExecutor(set(), registry, recovery_runtime=recovery)
    invalid = ToolExecutor({Permission.WRITE}, registry, recovery_runtime=recovery)

    assert denied.execute(tool.name, {"path": "x"}).error_type == "ToolPermissionError"
    assert invalid.execute(tool.name, {}).error_type == "ToolValidationError"
    assert not recovery.execution_checkpoints.checkpoints
