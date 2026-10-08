"""CLI entry points create genuinely independent Python worker processes."""
import argparse
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import signal

from cortex.tools.base import SideEffectPolicy
from .client import DistributedExecutionClient
from .models import ExecutionTask
from .factory import make_executor, close_recovery
from .redis_adapter import connect, RedisExecutionQueue, RedisExecutionStore, RedisWorkerRegistry
from .recovery import RecoveryController, WorkspaceRecoveryEvidence
from .worker import WorkerRuntime


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('worker', 'recovery', 'submit', 'status'))
    p.add_argument('--redis-url', default=os.environ.get('CORTEX_REDIS_URL', 'redis://localhost:6379/0'))
    p.add_argument('--namespace', default='cortex-distributed-v1')
    p.add_argument('--worker-name', default='worker')
    p.add_argument('--workspace', type=Path)
    p.add_argument('--storage', type=Path)
    p.add_argument('--workspace-session-id')
    p.add_argument('--workspace-manager-root', type=Path)
    p.add_argument('--backend', choices=('local', 'docker'), default='local')
    p.add_argument('--isolated', action='store_true', help='Execution root is an existing isolated session; enable existing sandbox')
    p.add_argument('--factory', help='Trusted importable module:function(config) -> ToolExecutor')
    p.add_argument('--lease-seconds', type=float, default=10)
    p.add_argument('--heartbeat-ttl', type=float, default=10)
    p.add_argument('--min-idle-ms', type=int, default=1000)
    p.add_argument('--tool-timeout', type=float, default=10)
    p.add_argument('--tool-name')
    p.add_argument('--tool-call-id')
    arguments = p.add_mutually_exclusive_group()
    arguments.add_argument('--arguments', default='{}')
    arguments.add_argument('--arguments-file', type=Path)
    p.add_argument('--side-effect-policy', choices=[p.value for p in SideEffectPolicy], default='none')
    p.add_argument('--max-attempts', type=int, default=3)
    p.add_argument('--idempotency-key')
    p.add_argument('--execution-id')
    return p


async def serve(args, queue, store, registry):
    if not args.factory and (args.workspace is None or args.storage is None):
        raise ValueError('worker/recovery requires --workspace and --storage')
    factory = make_executor
    if args.factory:
        module, symbol = args.factory.split(':', 1)
        factory = getattr(importlib.import_module(module), symbol)
    executor = factory(vars(args))
    evidence = WorkspaceRecoveryEvidence(executor.recovery_runtime) if executor.recovery_runtime else None
    controller = RecoveryController(queue, store, registry, workspace_evidence=evidence,
                                   lease_seconds=args.lease_seconds, min_idle_ms=args.min_idle_ms)
    runtime = WorkerRuntime(queue, store, registry, executor, worker_name=args.worker_name,
        lease_seconds=args.lease_seconds, heartbeat_ttl=args.heartbeat_ttl, recovery=controller)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: runtime.request_shutdown())
    print(json.dumps({'worker_id': runtime.worker_id, 'pid': os.getpid()}), flush=True)
    try:
        if args.command == 'worker':
            await runtime.run()
        else:
            while not runtime.stopping:
                await asyncio.to_thread(controller.recover_once)
                await asyncio.sleep(.2)
    finally:
        executor.close()
        close_recovery(executor)


def main():
    logging.basicConfig(level=logging.INFO)
    args = parser().parse_args()
    client = connect(args.redis_url)
    queue, store, registry = RedisExecutionQueue(client, args.namespace), RedisExecutionStore(client, args.namespace), RedisWorkerRegistry(client, args.namespace)
    try:
        if args.command in {'worker', 'recovery'}:
            asyncio.run(serve(args, queue, store, registry))
        elif args.command == 'submit':
            if not args.tool_name or not args.tool_call_id:
                raise ValueError('submit requires --tool-name and --tool-call-id')
            root = args.workspace.resolve(strict=True) if args.workspace else None
            info = root.stat() if root else None
            arguments = json.loads(args.arguments_file.read_text(encoding='utf-8-sig')
                if args.arguments_file else args.arguments)
            task = ExecutionTask(args.tool_call_id, args.tool_name, arguments,
                args.idempotency_key or args.tool_call_id, SideEffectPolicy(args.side_effect_policy),
                max_attempts=args.max_attempts, workspace_root=str(root) if root else None,
                workspace_generation=[info.st_dev, info.st_ino] if info else None)
            print(json.dumps(DistributedExecutionClient(queue, store).submit(task).to_dict()))
        else:
            if not args.execution_id: raise ValueError('status requires --execution-id')
            task = store.get(args.execution_id)
            if task is None: raise KeyError(args.execution_id)
            print(json.dumps(task.to_dict(), indent=2))
    finally:
        client.close()


if __name__ == '__main__':
    main()
