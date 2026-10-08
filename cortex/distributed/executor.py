"""Optional Agent-facing dispatch for an explicit subset of effect-free tools.

Workspace mutations remain on the Agent's existing logical/physical commit path.
Explicit ExecutionTask submission supports workspace mutations in workers without
pretending a remote boundary holds the Agent's local workspace lock.
"""
import asyncio
from uuid import uuid4
from pydantic import ValidationError

from cortex.tools.base import SideEffectPolicy
from cortex.tools.executor import ToolExecutor, ToolResult
from .models import ExecutionTask, ExecutionState


class DistributedToolExecutor(ToolExecutor):
    def __init__(self, *args, distributed_client, distributed_tools,
                 distributed_timeout=60, distributed_max_attempts=3, **kwargs):
        super().__init__(*args, **kwargs)
        self.distributed_client = distributed_client
        self.distributed_tools = frozenset(distributed_tools)
        self.distributed_timeout = distributed_timeout
        self.distributed_max_attempts = distributed_max_attempts
        if distributed_timeout <= 0 or distributed_max_attempts < 1:
            raise ValueError('invalid distributed timeout/attempt limit')
        for name in self.distributed_tools:
            if self.registry.get(name).side_effect_policy is not SideEffectPolicy.NONE:
                raise ValueError('Agent distributed dispatch V1 requires NONE tools; use explicit ExecutionTask for workspace writes')

    async def aexecute(self, tool_name, arguments, context=None):
        if tool_name not in self.distributed_tools:
            return await super().aexecute(tool_name, arguments, context)
        if self._closed: raise RuntimeError('ToolExecutor is closed')
        tool = self.registry.get(tool_name)
        if not self._check_permission(tool):
            return ToolResult(tool.name, 0, 0, False, 'ToolPermissionError', 'Permission denied', None)
        try:
            validated = self._validate(tool, arguments)
        except ValidationError as error:
            return ToolResult(tool.name, 0, 0, False, 'ToolValidationError', str(error), None, False)
        if self.workspace_guard: self.workspace_guard()
        root = generation = None
        if self.recovery_runtime:
            snapshots = self.recovery_runtime.snapshots
            snapshots._validate_generation()
            root, generation = str(snapshots.workspace), list(snapshots.generation)
        call_id = context.action_id if context else uuid4().hex
        key = f'{context.state.session_id}:{call_id}' if context else call_id
        task = ExecutionTask(call_id, tool_name, validated.model_dump(mode='json'), key,
            max_attempts=self.distributed_max_attempts, workspace_root=root, workspace_generation=generation)
        submitted = await self.distributed_client.asubmit(task)
        finished = await self.distributed_client.wait(submitted.execution_id, self.distributed_timeout)
        payload = finished.result or {}
        error = finished.error or {}
        return ToolResult(tool_name, finished.attempt, payload.get('duration_ms', 0),
            finished.state is ExecutionState.SUCCEEDED,
            error.get('kind') or payload.get('error_type'),
            error.get('message') or payload.get('error_message'), payload.get('data'),
            payload.get('validation_passed'))

    def execute(self, tool_name, arguments, context=None):
        if tool_name not in self.distributed_tools:
            return super().execute(tool_name, arguments, context)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute(tool_name, arguments, context))
        raise RuntimeError('Use aexecute inside an active event loop')
