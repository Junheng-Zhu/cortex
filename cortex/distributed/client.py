"""Explicit distributed submission; the default AgentLoop remains local."""
import asyncio
import time
from dataclasses import asdict
from uuid import uuid4
from cortex.tools.base import SideEffectPolicy
from .interfaces import ExecutionQueue, ExecutionStore, WorkerRegistry
from .models import AgentTaskResult, AgentTaskSpec, ExecutionTask, TaskType


class DistributedExecutionClient:
    def __init__(self, queue: ExecutionQueue, store: ExecutionStore, *, agent_registry=None,
                 agent_allowed_permissions=None, agent_allowed_tools=None):
        self.queue, self.store = queue, store
        # Host-owned authorization; never supplied by delegate_task arguments.
        self.agent_registry = agent_registry
        self.agent_allowed_permissions = frozenset(agent_allowed_permissions or ())
        self.agent_allowed_tools = frozenset(agent_allowed_tools if agent_allowed_tools is not None
            else (agent_registry.list_tools() if agent_registry else ()))

    def submit_agent(self, *, parent_run_id, goal, context, allowed_tools,
                     idempotency_key, max_steps=10, deadline=None, max_attempts=3):
        if self.agent_registry is None:
            raise ValueError('submit_agent requires a Host-bound Parent ToolRegistry and permissions')
        if not isinstance(allowed_tools, (list, set, frozenset)) or not all(isinstance(n, str) for n in allowed_tools):
            raise ValueError('allowed_tools must contain tool names')
        parent_tools = sorted(name for name in self.agent_allowed_tools
            if name != 'delegate_task' and
            self.agent_registry.get(name).permission in self.agent_allowed_permissions)
        spec = AgentTaskSpec(parent_run_id, goal, context, sorted(set(allowed_tools)),
                             parent_tools, max_steps, deadline)
        for name in spec.allowed_tools:
            if self.agent_registry.get(name).side_effect_policy is not SideEffectPolicy.NONE:
                raise ValueError('AgentTask V1 only permits effect-free tools')
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError('idempotency_key is required')
        task = ExecutionTask(uuid4().hex, '__agent__', {},
            f'agent:{parent_run_id}:{idempotency_key}', max_attempts=max_attempts,
            task_type=TaskType.AGENT, agent=asdict(spec))
        return self.submit(task).execution_id

    async def asubmit_agent(self, **kwargs):
        return await asyncio.to_thread(self.submit_agent, **kwargs)

    def get_agent_status(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.task_type is not TaskType.AGENT:
            raise ValueError('task_id does not reference an AgentTask')
        return task

    def cancel_agent(self, task_id):
        self.get_agent_status(task_id)
        return self.store.cancel(task_id)

    async def wait_agent(self, task_id, timeout=60):
        await asyncio.to_thread(self.get_agent_status, task_id)
        return AgentTaskResult.from_task(await self.wait(task_id, timeout))

    def submit(self, task: ExecutionTask) -> ExecutionTask:
        canonical = self.store.create(task)
        if canonical.terminal:
            return canonical
        # Publish first: a crash here leaves CREATED and a recoverable outbox gap.
        # Re-submission may create duplicate messages, never a new logical execution.
        self.queue.publish(canonical.execution_id)
        return self.store.queued(canonical.execution_id)

    async def asubmit(self, task):
        return await asyncio.to_thread(self.submit, task)

    async def wait(self, execution_id, timeout=60, poll_interval=.1):
        deadline = time.monotonic() + timeout
        while True:
            task = await asyncio.to_thread(self.store.get, execution_id)
            if task is None:
                raise KeyError(execution_id)
            if task.terminal:
                return task
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Execution {execution_id} is not terminal')
            await asyncio.sleep(poll_interval)
