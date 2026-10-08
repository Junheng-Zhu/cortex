"""Importable spawn targets: workers never inherit a Python execution registry."""
import asyncio
import json
import os
from pathlib import Path
import time
from pydantic import BaseModel

from cortex.distributed.factory import make_executor, close_recovery
from cortex.distributed.worker import WorkerRuntime
from cortex.distributed.redis_adapter import connect, RedisExecutionQueue, RedisExecutionStore, RedisWorkerRegistry
from cortex.distributed.recovery import RecoveryController, WorkspaceRecoveryEvidence
from cortex.tools.base import ToolTimeoutError, Tool, SideEffectPolicy, ExecutionStrategy
from cortex.tools.executor import ToolExecutor
from cortex.tools.registry import ToolRegistry
from cortex.tools.permission import Permission

LOCAL_COUNT = 0


class ProbeInput(BaseModel):
    delay: float = .05
    log: str
    fail: bool = False
    fail_first: bool = False
    invalid_result: bool = False
    metadata_change: bool = False


class ProbeTool(Tool):
    name = 'probe'
    description = 'Integration probe'
    input_model = ProbeInput
    permission = Permission.READ
    retryable = True
    max_retries = 5
    timeout = 20
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED

    def execute(self, input):
        raise AssertionError('native async path required')

    async def aexecute(self, input):
        global LOCAL_COUNT
        LOCAL_COUNT += 1
        count = LOCAL_COUNT
        # Observability only: coordination/ownership is exclusively Redis.
        fd = os.open(input.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try: os.write(fd, (json.dumps({'pid': os.getpid(), 'local_count': count, 'at': time.time()}) + '\n').encode())
        finally: os.close(fd)
        await asyncio.sleep(input.delay)
        if input.fail: raise RuntimeError('business failure')
        if input.invalid_result: return {object()}
        return {'pid': os.getpid(), 'local_count': count, 'empty': [], 'nested': {'large': 12345678901234567890}}


class IrreversibleTool(ProbeTool):
    name = 'irreversible'
    side_effect_policy = SideEffectPolicy.IRREVERSIBLE


class CompensatableTool(ProbeTool):
    name = 'compensatable'
    side_effect_policy = SideEffectPolicy.COMPENSATABLE


class TimeoutTool(ProbeTool):
    name = 'timeout_probe'
    async def aexecute(self, input):
        await super().aexecute(input.model_copy(update={'delay': .01}))
        raise ToolTimeoutError('backend timed out')


class DeniedTool(ProbeTool):
    name = 'denied'
    permission = Permission.EXECUTE


class WorkspaceTool(ProbeTool):
    name = 'workspace_probe'
    permission = Permission.WRITE
    side_effect_policy = SideEffectPolicy.WORKSPACE_REVERSIBLE

    def __init__(self, root): self.root = Path(root)

    async def aexecute(self, input):
        before = (self.root / 'file').read_text()
        result = await super().aexecute(input.model_copy(update={'fail': False}))
        (self.root / 'file').write_text('changed')
        if input.metadata_change:
            (self.root / '.git').mkdir(exist_ok=True)
            (self.root / '.git' / 'distributed-marker').write_text('not reversible')
        if input.fail or (input.fail_first and len(Path(input.log).read_text().splitlines()) == 1):
            raise RuntimeError('failed after mutation')
        return dict(result, before=before)


def make_probe_executor(config):
    if config.get('workspace'):
        executor = make_executor(config)
        executor.registry.register(WorkspaceTool(config['workspace']))
    else:
        executor = ToolExecutor(set(Permission), ToolRegistry())
    for tool in (ProbeTool(), IrreversibleTool(), CompensatableTool(), TimeoutTool(), DeniedTool()):
        executor.registry.register(tool)
    if config.get('deny_execution'):
        executor.allowed_permissions.discard(Permission.EXECUTE)
    executor.timeout = 20
    return executor


def worker_process(url, namespace, config, stop, hook_stage=None, marker=None, recover=True):
    client = connect(url)
    queue, store, registry = RedisExecutionQueue(client, namespace), RedisExecutionStore(client, namespace), RedisWorkerRegistry(client, namespace)
    executor = make_probe_executor(config)
    evidence = WorkspaceRecoveryEvidence(executor.recovery_runtime) if executor.recovery_runtime else None
    controller = RecoveryController(queue, store, registry, workspace_evidence=evidence,
                                   min_idle_ms=100, lease_seconds=2, orphan_seconds=.5)
    async def hook(stage, task):
        if stage == hook_stage:
            Path(marker).write_text(task.execution_id)
            while True: await asyncio.sleep(.02)
    runtime = WorkerRuntime(queue, store, registry, executor,
        lease_seconds=3 if config.get('workspace') else .6,
        heartbeat_ttl=3 if config.get('workspace') else .6, recovery=controller if recover else None, hook=hook)
    async def run():
        async def monitor():
            while not stop.is_set(): await asyncio.sleep(.02)
            runtime.request_shutdown()
        watcher = asyncio.create_task(monitor())
        try: await runtime.run()
        finally:
            watcher.cancel()
            close_recovery(executor)
    try: asyncio.run(run())
    finally: client.close()


def recovery_process(url, namespace, barrier):
    client = connect(url)
    controller = RecoveryController(RedisExecutionQueue(client, namespace), RedisExecutionStore(client, namespace),
        RedisWorkerRegistry(client, namespace), min_idle_ms=0, lease_seconds=2)
    barrier.wait(timeout=10)
    controller.recover_once()
    client.close()


def acquire_process(url, namespace, execution_id, barrier, outcomes):
    client = connect(url)
    store = RedisExecutionStore(client, namespace)
    barrier.wait(timeout=10)
    task = store.acquire(execution_id, str(os.getpid()), 30)
    outcomes.put(task.owner_worker_id if task else None)
    client.close()
