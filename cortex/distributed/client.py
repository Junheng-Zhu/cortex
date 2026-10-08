"""Explicit distributed submission; the default AgentLoop remains local."""
import asyncio
import time
from .interfaces import ExecutionQueue, ExecutionStore, WorkerRegistry
from .models import ExecutionTask


class DistributedExecutionClient:
    def __init__(self, queue: ExecutionQueue, store: ExecutionStore):
        self.queue, self.store = queue, store

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
        while time.monotonic() < deadline:
            task = await asyncio.to_thread(self.store.get, execution_id)
            if task is None:
                raise KeyError(execution_id)
            if task.terminal:
                return task
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f'Execution {execution_id} is not terminal')
