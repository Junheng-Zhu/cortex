"""Opt-in model tool: submit first, explicitly poll/wait in a later call."""
import asyncio
from dataclasses import asdict
import json
from typing import Literal

from pydantic import BaseModel, Field
from cortex.tools.base import Tool, ExecutionStrategy, SideEffectPolicy
from cortex.tools.permission import Permission
from .client import DistributedExecutionClient


class DelegateTaskInput(BaseModel):
    operation: Literal['submit', 'wait']
    goal: str | None = None
    context_json: str = '{}'
    allowed_tools: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    max_steps: int = 10
    deadline: float | None = None
    task_id: str | None = None
    timeout: float = 1


class DelegateTaskTool(Tool):
    name = 'delegate_task'
    description = ('Submit an independent Child task and return task_id immediately, or explicitly '
                   'wait by task_id. context_json is an explicit JSON object; no Parent history '
                   'is inherited. A wait timeout leaves the task running. Child cannot delegate.')
    input_model = DelegateTaskInput
    permission = Permission.DELEGATE
    retryable = False
    max_retries = 0
    timeout = 10
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    # Submission is a durable external effect, so never eligible as a Child tool.
    side_effect_policy = SideEffectPolicy.IRREVERSIBLE

    def __init__(self, client, parent_executor, parent_run_id):
        self.transport = client
        self.parent_executor = parent_executor
        self.parent_run_id = parent_run_id

    def _client(self):
        permissions = set(self.parent_executor.allowed_permissions)
        names = set(self.parent_executor.registry.list_tools())
        if self.transport.agent_registry is not None:
            permissions.intersection_update(self.transport.agent_allowed_permissions)
            names.intersection_update(self.transport.agent_allowed_tools)
        return DistributedExecutionClient(self.transport.queue, self.transport.store,
            agent_registry=self.parent_executor.registry,
            agent_allowed_permissions=permissions, agent_allowed_tools=names)

    async def aexecute(self, input):
        client = self._client()
        if input.operation == 'submit':
            task_id = await client.asubmit_agent(parent_run_id=self.parent_run_id(),
                goal=input.goal, context=json.loads(input.context_json),
                allowed_tools=input.allowed_tools, idempotency_key=input.idempotency_key,
                max_steps=input.max_steps, deadline=input.deadline)
            return {'task_id': task_id}
        if not input.task_id:
            raise ValueError('wait requires task_id')
        if input.timeout < 0:
            raise ValueError('wait timeout must be non-negative')
        try:
            return asdict(await client.wait_agent(input.task_id, input.timeout))
        except TimeoutError:
            return {'task_id': input.task_id, 'waiting': True}

    def execute(self, input):
        return asyncio.run(self.aexecute(input))
