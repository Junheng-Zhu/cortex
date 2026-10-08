"""Pending delivery recovery and conservative side-effect decisions."""
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from cortex.tools.base import SideEffectPolicy
from .interfaces import ExecutionQueue, ExecutionStore, WorkerRegistry
from .models import ExecutionError, ExecutionState, FailureKind, InfrastructureUnavailable, OwnershipLost


def failure(kind, message, *, infrastructure=False, uncertain=False):
    return asdict(ExecutionError(kind, message, infrastructure, uncertain))


class WorkspaceRecoveryEvidence:
    """Only a *completed*, durably checkpointed action can be inverted.

    A pre-snapshot alone cannot prove child quiescence or distinguish user edits.
    Crash during mutation therefore fails closed, without blanket rollback.
    """
    def __init__(self, runtime):
        self.runtime = runtime

    def completed(self, task):
        r = task.recovery
        if (not r or r.get('workspace_id') != self.runtime.snapshots.workspace_id or
                task.workspace_generation != list(self.runtime.snapshots.generation)):
            return None
        if Path(r['workspace_root']) != self.runtime.snapshots.workspace:
            return None
        with self.runtime.snapshots.lock.read():
            self.runtime.snapshots._validate_generation()
            if getattr(self.runtime, "workspace_guard", None):
                self.runtime.workspace_guard()
            head = self.runtime.active_head(r['session_id'])
            if (head is None or head.reason != 'distributed_tool_complete' or
                    tuple(head.action_ids) != (r['action_id'],) or
                    head.parent_execution_checkpoint_id != r['pre_checkpoint_id']):
                return None
            checkpoint = self.runtime.agent_checkpoints.load(head.agent_checkpoint_id)
            if checkpoint is None:
                return None
            for observation in checkpoint.observations:
                if observation['action_id'] == r['action_id']:
                    # Full ToolResult is saved as normalized observation output.
                    return observation['output']
        return None

    def rollback(self, task, before_restore=None):
        records = self.runtime.ledger.query(action_id=task.recovery['action_id'])
        if not records:
            # A completed checkpoint with no mutation is also positive evidence.
            return
        result = self.runtime.rollback_action(task.recovery['action_id'], before_restore=before_restore)
        if result.conflicts:
            raise RuntimeError('workspace recovery conflicts: ' + ', '.join(result.conflicts))


class RecoveryController:
    def __init__(self, queue: ExecutionQueue, store: ExecutionStore, registry: WorkerRegistry, *, workspace_evidence=None,
                 controller_id=None, lease_seconds=10, min_idle_ms=1000,
                 orphan_seconds=30):
        if lease_seconds <= 0 or min_idle_ms < 0 or orphan_seconds <= 0:
            raise ValueError('invalid recovery timing')
        self.queue, self.store, self.registry = queue, store, registry
        self.evidence = workspace_evidence
        self.controller_id = controller_id or 'recovery-' + uuid4().hex
        self.lease_seconds, self.min_idle_ms = lease_seconds, min_idle_ms
        self.orphan_seconds = orphan_seconds

    def _ack_terminal(self, message):
        if message and self.queue.reclaim(message, self.controller_id, self.min_idle_ms):
            self.queue.ack(message)

    def _process(self, task, message=None):
        if task.terminal:
            self._ack_terminal(message)
            return
        now = self.store.now()
        if task.state in {ExecutionState.CLAIMED, ExecutionState.RUNNING, ExecutionState.RECOVERING}:
            if task.lease_expires_at > now:
                return  # Pending idle and heartbeat alone never revoke ownership.
            alive = self.registry.get(task.owner_worker_id) if task.owner_worker_id else None
            task = self.store.lose(task.execution_id, failure(
                FailureKind.LEASE_EXPIRED if alive else FailureKind.WORKER_CRASH,
                'execution lease expired', infrastructure=True))
            if task is None:
                return
        task = self.store.recover(task.execution_id, self.controller_id, self.lease_seconds)
        if task is None:
            return
        lease = task.lease
        try:
            if message:
                self.queue.reclaim(message, self.controller_id, self.min_idle_ms)
            completed = task.outcome
            if task.lost_from == 'RUNNING' and task.side_effect_policy is SideEffectPolicy.WORKSPACE_REVERSIBLE:
                try:
                    # Redis outcome alone does not prove durable workspace commit.
                    completed = self.evidence.completed(task) if self.evidence else None
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    self.store.finish(lease, False, None, failure(FailureKind.RECOVERY_CONFLICT, str(error)))
                    if message: self.queue.ack(message)
                    return
            if completed and completed.get('success'):
                self.store.finish(lease, True, completed)
                if message: self.queue.ack(message)
                return
            error = None
            if task.lost_from == 'RUNNING':
                if task.side_effect_policy is SideEffectPolicy.NONE:
                    pass
                elif task.side_effect_policy is SideEffectPolicy.WORKSPACE_REVERSIBLE and completed:
                    if task.attempt < task.max_attempts:
                        try:
                            self.evidence.rollback(task, before_restore=lambda:
                                self.store.renew(lease, self.lease_seconds))
                        except (InfrastructureUnavailable, OwnershipLost):
                            raise
                        except (OSError, ValueError, RuntimeError, KeyError) as exc:
                            error = failure(FailureKind.RECOVERY_CONFLICT, str(exc))
                else:
                    error = failure(FailureKind.SIDE_EFFECT_UNCERTAIN,
                        'No durable, reversible completion evidence; automatic replay refused',
                        infrastructure=True, uncertain=True)
            if error is None and task.attempt >= task.max_attempts:
                error = failure(FailureKind.RETRY_EXHAUSTED, 'max_attempts reached')
            if error:
                self.store.finish(lease, False, completed, error)
            else:
                # Handoff is at-least-once: replacement delivery must exist before
                # releasing ownership, then ACK the old message. A crash produces
                # duplicates, never an ACKed task with no replacement delivery.
                self.queue.publish(task.execution_id)
                self.store.ready(lease)
            if message:
                self.queue.ack(message)
        except OwnershipLost:
            # Another owner is now responsible. Never ACK without a durable handoff.
            return

    def recover_once(self):
        messages = self.queue.pending(self.min_idle_ms)
        seen = set()
        for message in messages:
            seen.add(message.execution_id)
            task = self.store.get(message.execution_id)
            if task is None:
                # Unknown task is corruption, not a license to execute or drop it.
                raise RuntimeError('Pending message references missing execution')
            self._process(task, message)
        now = self.store.now()
        # Repair create/publish gaps and abandoned recovery handoffs. No trim or
        # eviction is supported; active expired tasks with no PEL are repaired too.
        for task in self.store.tasks():
            if task.execution_id in seen:
                continue
            expired = task.state in {ExecutionState.RUNNING, ExecutionState.CLAIMED, ExecutionState.RECOVERING} and task.lease_expires_at <= now
            orphan = task.state in {ExecutionState.CREATED, ExecutionState.QUEUED} and now - (task.queued_at or task.created_at) >= self.orphan_seconds
            if expired or orphan or task.state is ExecutionState.LOST:
                self._process(task)
