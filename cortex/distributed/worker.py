"""One independent process owns one WorkerRuntime; no Redis imports here."""
import asyncio
from contextlib import suppress
import json
import logging
import os
import socket
from uuid import uuid4
from pathlib import Path

from cortex.runtime.observation import Observation
from cortex.runtime.state import AgentState
from cortex.tools.base import SideEffectPolicy
from cortex.tools.executor import ToolExecutionContext
from .interfaces import ExecutionQueue, ExecutionStore, WorkerRegistry
from .models import FailureKind, InfrastructureUnavailable, OwnershipLost
from .recovery import failure

log = logging.getLogger(__name__)


class ResultSerializationError(TypeError):
    pass


def result_payload(result):
    try:
        payload = {k: getattr(result, k) for k in result.__dataclass_fields__ if k != 'mutation_boundary'}
        return json.loads(json.dumps(payload, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ResultSerializationError(str(error)) from error


def classify(result):
    kind = FailureKind.TOOL_EXECUTION_ERROR
    if result.error_type == 'ToolValidationError': kind = FailureKind.INVALID_ARGUMENTS
    elif result.error_type in {'ToolPermissionError', 'ToolSandboxError'}: kind = FailureKind.PERMISSION_FAILURE
    elif result.error_type in {'ShellUnavailableError', 'ExecutionUnavailableError'}: kind = FailureKind.TOOL_INFRASTRUCTURE
    elif 'Timeout' in (result.error_type or ''): kind = FailureKind.TOOL_TIMEOUT
    return failure(kind, result.error_message or 'Tool failed',
                   infrastructure=kind is FailureKind.TOOL_INFRASTRUCTURE,
                   uncertain=(result.error_message or '').startswith('Excluded metadata changed'))


class WorkerRuntime:
    def __init__(self, queue: ExecutionQueue, store: ExecutionStore, registry: WorkerRegistry, executor, *, worker_name='worker',
                 lease_seconds=10, heartbeat_ttl=10, recovery=None, hook=None):
        if lease_seconds <= 0 or heartbeat_ttl <= 0:
            raise ValueError('worker timers must be positive')
        self.queue, self.store, self.registry, self.executor = queue, store, registry, executor
        self.worker_id = f'{worker_name}-{os.getpid()}-{uuid4().hex}'
        self.lease_seconds, self.heartbeat_ttl = lease_seconds, heartbeat_ttl
        self.recovery, self.hook = recovery, hook
        self.stopping = False
        # Single ToolExecutor attempt per distributed attempt. Validation,
        # permissions, supervision and timeout remain owned by ToolExecutor.
        executor.max_retries = 0

    def request_shutdown(self):
        self.stopping = True

    async def _hook(self, stage, task):
        if self.hook:
            await self.hook(stage, task)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.heartbeat_ttl / 3)
            try:
                if not await asyncio.to_thread(self.registry.heartbeat, self.worker_id, self.heartbeat_ttl):
                    self.stopping = True  # Cannot safely advertise this identity again.
                    return
            except InfrastructureUnavailable:
                log.warning('Worker heartbeat unavailable; execution leases remain authoritative')

    async def _execute(self, task):
        state = AgentState(session_id='distributed-' + task.execution_id,
                           run_id=self.worker_id)
        action_id = f'{task.execution_id}:attempt:{task.attempt}'
        def began(boundary):
            runtime = boundary.runtime
            # Distributed retry must also reject excluded Git/runtime metadata
            # changes in DIRECT mode; the ordinary local mode is unchanged.
            if boundary.excluded_before is None:
                boundary.excluded_before = runtime.snapshots.excluded_state()
            self.store.attach_recovery(task.lease, dict(
                session_id=state.session_id, action_id=action_id,
                workspace_id=runtime.snapshots.workspace_id,
                workspace_root=str(runtime.snapshots.workspace),
                pre_checkpoint_id=boundary.pre_checkpoint.execution_checkpoint_id))
        context = ToolExecutionContext(state, action_id, on_mutation_begin=began,
            before_execution=lambda: self.store.renew(task.lease, self.lease_seconds))
        result = await self.executor.aexecute(task.tool_name, task.arguments, context=context)
        boundary = result.mutation_boundary
        try:
            payload = result_payload(result)
            if boundary:
                state.observations.append(Observation(action_id, result.success, output=payload,
                    error=result.error_message, error_type=result.error_type))
                commit = asyncio.create_task(asyncio.to_thread(
                    boundary.commit, state, reason='distributed_tool_complete'))
                try:
                    await asyncio.shield(commit)
                except asyncio.CancelledError:
                    # A SQLite/Git commit in a thread cannot be cancelled. Wait
                    # for it to release ownership before cancellation cleanup.
                    await commit
                    raise
                result.mutation_boundary = None
            await self._hook('after_tool_before_result', task)
            return result, payload
        finally:
            if boundary and not boundary._closed:
                boundary.cancel(state)

    async def _renew(self, task, operation, lost):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            try:
                await asyncio.to_thread(self.store.renew, task.lease, self.lease_seconds)
            except (OwnershipLost, InfrastructureUnavailable):
                lost.set()
                operation.cancel()
                return

    async def process(self, message):
        task = await asyncio.to_thread(self.store.get, message.execution_id)
        if task is None:
            raise RuntimeError('Message references missing execution')
        if task.terminal:
            await asyncio.to_thread(self.queue.ack, message)
            return
        task = await asyncio.to_thread(self.store.acquire, task.execution_id, self.worker_id, self.lease_seconds)
        if task is None: return  # Duplicate of an active or not-yet-queued task.
        if task.terminal:
            await asyncio.to_thread(self.queue.ack, message)
            return
        # Check the trusted registry rather than trusting producer policy labels.
        try:
            tool = self.executor._get_tool(task.tool_name)
            if tool.side_effect_policy is not task.side_effect_policy:
                raise ValueError('Task SideEffectPolicy does not match the registered tool')
            if tool.side_effect_policy is SideEffectPolicy.WORKSPACE_REVERSIBLE and self.executor.recovery_runtime is None:
                raise ValueError('Workspace-reversible execution requires durable WorkspaceRecoveryRuntime')
            if task.workspace_root is not None:
                runtime = self.executor.recovery_runtime
                if (runtime is None or Path(task.workspace_root) != runtime.snapshots.workspace or
                        list(runtime.snapshots.generation) != task.workspace_generation):
                    raise ValueError('Workspace binding/generation mismatch')
            elif tool.side_effect_policy is SideEffectPolicy.WORKSPACE_REVERSIBLE:
                raise ValueError('Workspace-reversible tasks require workspace_root and generation')
        except Exception as error:
            await asyncio.to_thread(self.store.finish, task.lease, False, None,
                                    failure(FailureKind.POLICY_MISMATCH, str(error)))
            await asyncio.to_thread(self.queue.ack, message)
            return

        async def invoke():
            await self._hook('after_claim_before_start', task)
            running = await asyncio.to_thread(self.store.start, task.lease)
            await self._hook('after_start_before_tool', running)
            return await self._execute(running)

        lost = asyncio.Event()
        operation = asyncio.create_task(invoke())
        renewer = asyncio.create_task(self._renew(task, operation, lost))
        try:
            try:
                result, payload = await operation
            except asyncio.CancelledError:
                if lost.is_set(): return  # Leave PEL for recovery, never ACK uncertainty.
                raise
            if lost.is_set(): return
            if result.success:
                terminal = await asyncio.to_thread(self.store.finish, task.lease, True, payload)
            else:
                error = classify(result)
                if tool.side_effect_policy in {SideEffectPolicy.IRREVERSIBLE, SideEffectPolicy.COMPENSATABLE} and result.attempts:
                    error['uncertain'] = True
                no_retry = error['kind'] in {FailureKind.INVALID_ARGUMENTS, FailureKind.PERMISSION_FAILURE}
                no_retry = no_retry or not tool.retryable or tool.side_effect_policy in {SideEffectPolicy.IRREVERSIBLE, SideEffectPolicy.COMPENSATABLE}
                if no_retry or task.attempt >= task.max_attempts:
                    if task.attempt >= task.max_attempts and not no_retry:
                        error = failure(FailureKind.RETRY_EXHAUSTED, 'max_attempts reached: ' + error['message'])
                    terminal = await asyncio.to_thread(self.store.finish, task.lease, False, payload, error)
                else:
                    await asyncio.to_thread(self.store.end_attempt, task.lease, payload, error)
                    return  # Recovery decides whether rollback/retry is safe.
            renewer.cancel()
            await self._hook('after_result_before_ack', terminal)
            await asyncio.to_thread(self.queue.ack, message)
        except (OwnershipLost, InfrastructureUnavailable):
            log.warning('Execution ownership/durability unavailable; leaving message pending')
        except ResultSerializationError as error:
            # Transport-contract failures are terminal; effects may already exist.
            log.error('Execution result/metadata not serializable: %s', error)
            with suppress(OwnershipLost, InfrastructureUnavailable):
                await asyncio.to_thread(self.store.finish, task.lease, False, None,
                    failure(FailureKind.RESULT_SERIALIZATION, str(error),
                        uncertain=task.side_effect_policy is not SideEffectPolicy.NONE))
                await asyncio.to_thread(self.queue.ack, message)
        except Exception as error:
            # Errors outside ToolExecutor's normalized business result (backend
            # setup, snapshot/SQLite failure, child process start) are infrastructure.
            log.exception('Tool runtime infrastructure failed; recovery must decide safety')
            with suppress(OwnershipLost, InfrastructureUnavailable):
                await asyncio.to_thread(self.store.end_attempt, task.lease,
                    {'success': False, 'error_type': type(error).__name__, 'error_message': str(error)},
                    failure(FailureKind.TOOL_INFRASTRUCTURE, str(error), infrastructure=True,
                        uncertain=task.side_effect_policy is not SideEffectPolicy.NONE))
        finally:
            renewer.cancel()
            with suppress(asyncio.CancelledError): await renewer

    async def run(self):
        await asyncio.to_thread(self.registry.register, self.worker_id,
            {'pid': os.getpid(), 'host': socket.gethostname()}, self.heartbeat_ttl)
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while not self.stopping:
                try:
                    if self.recovery:
                        await asyncio.to_thread(self.recovery.recover_once)
                    message = await asyncio.to_thread(self.queue.receive, self.worker_id, 100)
                    if message and not self.stopping:
                        await self.process(message)
                except InfrastructureUnavailable:
                    log.warning('Distributed infrastructure unavailable; no submission/completion assumed')
                    await asyncio.sleep(.2)
                except OwnershipLost:
                    pass
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError): await heartbeat
            with suppress(InfrastructureUnavailable):
                await asyncio.to_thread(self.registry.unregister, self.worker_id)
            self.executor.close()
