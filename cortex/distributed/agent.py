"""Child construction and authorization at the existing ToolExecutor boundary."""
from dataclasses import asdict
import json
from pathlib import Path

from cortex.app.bootstrap import build_agent
from cortex.context.artifacts import ArtifactStore
from cortex.memory.manager import MemoryManager
from cortex.memory.store import InMemoryStore
from cortex.memory.session_store import InMemorySessionStore
from cortex.observability.tracer import RunRecorder
from cortex.runtime.checkpoint import InMemoryCheckpointStore
from cortex.runtime.session import Session, SessionConfig
from cortex.runtime.workspace_session import WorkspaceManager
from cortex.tools.base import SideEffectPolicy
from cortex.tools.executor import ToolExecutor, ToolResult
from cortex.tools.registry import ToolRegistry
from .models import AgentTaskSpec, AgentTaskResult, ExecutionState


class AgentDeadlineExceeded(RuntimeError):
    pass


def validate_agent(task, executor):
    spec = AgentTaskSpec(**task.agent)
    if task.side_effect_policy is not SideEffectPolicy.NONE or task.workspace_root is not None:
        raise ValueError('AgentTask V1 requires NONE and cannot request workspace mutations')
    registry = ToolRegistry()
    for name in spec.allowed_tools:
        # These tools bind runtime-owned artifacts/skill context. A Worker
        # registry borrowed from another agent is not a Child-scoped binding.
        if name == 'read_artifact_chunk' or name.startswith('skill_'):
            raise ValueError('V1 does not delegate context-bound artifact/skill tools')
        tool = executor._get_tool(name)
        if not executor._check_permission(tool) or tool.side_effect_policy is not SideEffectPolicy.NONE:
            raise ValueError('Child tool denied by Worker permissions or side-effect policy: ' + name)
        registry.register(tool)
    return spec, registry


class ChildToolExecutor(ToolExecutor):
    """Borrow Worker tools, retaining its supervision and concurrency limits."""
    def __init__(self, source, registry, guard):
        super().__init__(source.allowed_permissions, registry,
                         max_tool_concurrency=source.max_tool_concurrency)
        self.source, self.guard = source, guard
        self._concurrency_semaphore = source._concurrency_semaphore
        self._serial_lock = source._serial_lock

    async def aexecute(self, tool_name, arguments, context=None):
        import asyncio
        await asyncio.to_thread(self.guard)
        if tool_name not in self.registry.list_tools():
            return ToolResult(tool_name, 0, 0, False, 'ToolPermissionError',
                              'Tool is outside Child allowed_tools', None)
        tool = self.source._get_tool(tool_name)
        if tool.side_effect_policy is not SideEffectPolicy.NONE or not self.source._check_permission(tool):
            return ToolResult(tool_name, 0, 0, False, 'ToolPermissionError',
                              'Child tool no longer satisfies Worker policy', None)
        if self._closed:
            raise RuntimeError('Child ToolExecutor is closed')
        # The underlying executor calls this again *after* any workspace lock
        # wait, immediately before launching the supervised tool.
        if context is None:
            raise RuntimeError('Child tool calls require an AgentState context')
        context.before_execution = self.guard
        return await self.source.aexecute(tool_name, arguments, context)

    def execute(self, tool_name, arguments, context=None):
        raise RuntimeError('Child uses the existing async AgentLoop path')

    def close(self):
        # Tools belong to WorkerRuntime, not an individual Child attempt.
        self._closed = True


def build_child(client, task, source_executor, registry, guard, workspace, storage):
    spec = AgentTaskSpec(**task.agent)
    child = build_agent(client,
        max_steps=spec.max_steps, tool_registry=registry,
        allowed_permissions=set(source_executor.allowed_permissions),
        session=Session(session_id=spec.child_session_id, temporary_chat=True),
        session_config=SessionConfig(temporary_chat=True, persist_trace=False),
        recorder=RunRecorder(persist=False), memory_manager=MemoryManager(InMemoryStore()),
        session_store=InMemorySessionStore(), checkpoint_store=InMemoryCheckpointStore(),
        skills_enabled=False, workspace_recovery_enabled=False,
        execution_workspace=workspace, workspace_manager=WorkspaceManager(Path(storage) / 'workspace'),
        artifact_store=ArtifactStore(Path(storage) / 'artifacts'), decision_in_thread=True)
    # Replace only the executor boundary; AgentLoop/Observation/budget are reused.
    child.executor = ChildToolExecutor(source_executor, registry, guard)
    return child


def agent_result(task, child=None, *, answer=None, error=None, elapsed_ms=0):
    state = child.last_state if child else None
    run = child.recorder.current_run if child else None
    statistics = dict(attempt=task.attempt, child_session_id=task.agent['child_session_id'],
        steps=state.step_count if state else 0, duration_ms=elapsed_ms,
        termination_reason=getattr(state, '_termination_reason', None),
        total_tokens=run.total_tokens if run else 0, token_limit_enforced=False)
    payload = asdict(AgentTaskResult(task.execution_id,
        ExecutionState.FAILED if error else ExecutionState.SUCCEEDED, answer, error, statistics))
    payload['success'] = error is None
    return json.loads(json.dumps(payload, allow_nan=False))


def child_query(spec):
    return spec.goal + '\n\nExplicit task context (JSON):\n' + json.dumps(spec.context, ensure_ascii=False, allow_nan=False)
