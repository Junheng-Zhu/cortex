"""Atomic logical/physical recovery boundaries for an Agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Protocol, TypeVar
from uuid import uuid4

from cortex.observability.tracer import RunRecorder

from .checkpoint import AgentCheckpoint, CheckpointStore
from .state import AgentState
from .workspace import (
    ShadowGitSnapshotStore, WorkspaceDiff, WorkspaceOperation, WorkspaceSnapshot,
)


@dataclass(frozen=True)
class ExecutionCheckpoint:
    agent_checkpoint_id: str
    workspace_snapshot_id: str
    session_id: str
    run_id: str
    wave_id: str | None = None
    action_ids: tuple[str, ...] = ()
    parent_execution_checkpoint_id: str | None = None
    reason: str = "mutation"
    execution_checkpoint_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class MutationRecord:
    action_id: str | None
    wave_id: str | None
    execution_checkpoint_id: str
    path: str
    operation: WorkspaceOperation
    before_content_hash: str | None
    after_content_hash: str | None


class MutationLedger:
    def __init__(self) -> None:
        self.records: list[MutationRecord] = []

    def record(self, checkpoint: ExecutionCheckpoint, diff: WorkspaceDiff) -> None:
        action_id = checkpoint.action_ids[0] if len(checkpoint.action_ids) == 1 else None
        self.records.extend(
            MutationRecord(action_id, checkpoint.wave_id,
                           checkpoint.execution_checkpoint_id, change.path,
                           change.operation, change.before_content_hash,
                           change.after_content_hash)
            for change in diff.changes
        )


class ExecutionCheckpointStore(Protocol):
    def save(self, checkpoint: ExecutionCheckpoint) -> None: ...
    def load(self, checkpoint_id: str) -> ExecutionCheckpoint | None: ...
    def latest(self, session_id: str) -> ExecutionCheckpoint | None: ...


class InMemoryExecutionCheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: list[ExecutionCheckpoint] = []

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        self.checkpoints.append(checkpoint)

    def load(self, checkpoint_id: str) -> ExecutionCheckpoint | None:
        return next((x for x in self.checkpoints
                     if x.execution_checkpoint_id == checkpoint_id), None)

    def latest(self, session_id: str) -> ExecutionCheckpoint | None:
        return next((x for x in reversed(self.checkpoints)
                     if x.session_id == session_id), None)


class SQLiteExecutionCheckpointStore:
    """Durable execution-boundary index; snapshots remain in shadow Git."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS execution_checkpoints (
                execution_checkpoint_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL, created_at TEXT NOT NULL,
                payload TEXT NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_session "
                "ON execution_checkpoints(session_id, created_at)"
            )

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        payload = json.dumps(asdict(checkpoint), default=str)
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT OR REPLACE INTO execution_checkpoints VALUES (?, ?, ?, ?)",
                               (checkpoint.execution_checkpoint_id,
                                checkpoint.session_id,
                                checkpoint.created_at.isoformat(), payload))

    @staticmethod
    def _decode(payload: str) -> ExecutionCheckpoint:
        values = json.loads(payload)
        values["action_ids"] = tuple(values["action_ids"])
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        return ExecutionCheckpoint(**values)

    def load(self, checkpoint_id: str) -> ExecutionCheckpoint | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM execution_checkpoints WHERE execution_checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._decode(row[0]) if row else None

    def latest(self, session_id: str) -> ExecutionCheckpoint | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM execution_checkpoints WHERE session_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id,),
            ).fetchone()
        return self._decode(row[0]) if row else None


T = TypeVar("T")


class WorkspaceRecoveryRuntime:
    """Coordinates snapshots with logical checkpoints under one write lock."""

    def __init__(self, snapshots: ShadowGitSnapshotStore,
                 agent_checkpoints: CheckpointStore,
                 execution_checkpoints: ExecutionCheckpointStore | None = None,
                 ledger: MutationLedger | None = None,
                 recorder: RunRecorder | None = None):
        self.snapshots = snapshots
        self.agent_checkpoints = agent_checkpoints
        self.execution_checkpoints = (execution_checkpoints or
                                      InMemoryExecutionCheckpointStore())
        self.ledger = ledger or MutationLedger()
        self.recorder = recorder
        self._workspace_snapshots: dict[str, WorkspaceSnapshot] = {}

    def checkpoint(self, state: AgentState, *, reason: str = "checkpoint",
                   wave_id: str | None = None,
                   action_ids: tuple[str, ...] = ()) -> ExecutionCheckpoint:
        with self.snapshots.lock:
            return self._checkpoint_unlocked(
                state, reason=reason, wave_id=wave_id, action_ids=action_ids
            )

    def _checkpoint_unlocked(
        self, state: AgentState, *, reason: str, wave_id: str | None,
        action_ids: tuple[str, ...], snapshot: WorkspaceSnapshot | None = None,
    ) -> ExecutionCheckpoint:
        agent = AgentCheckpoint.capture(state)
        workspace = snapshot or self.snapshots.snapshot_unlocked()
        self.agent_checkpoints.save(agent)
        self._workspace_snapshots[workspace.workspace_snapshot_id] = workspace
        parent = self.execution_checkpoints.latest(state.session_id)
        execution = ExecutionCheckpoint(
            agent.checkpoint_id, workspace.workspace_snapshot_id,
            state.session_id, state.run_id, wave_id, tuple(action_ids),
            parent.execution_checkpoint_id if parent else None, reason,
        )
        self.execution_checkpoints.save(execution)
        return execution

    def begin_mutation(
        self, state: AgentState, *, action_id: str, tool_name: str,
        wave_id: str | None = None, side_effect_policy: str = "workspace_reversible",
    ) -> "MutationBoundary":
        self.snapshots.lock.__enter__()
        try:
            return self._begin_mutation_owned(
                state, action_id=action_id, tool_name=tool_name,
                wave_id=wave_id, side_effect_policy=side_effect_policy,
            )
        except BaseException:
            self.snapshots.lock.release()
            raise

    async def abegin_mutation(
        self, state: AgentState, *, action_id: str, tool_name: str,
        wave_id: str | None = None, side_effect_policy: str = "workspace_reversible",
    ) -> "MutationBoundary":
        await self.snapshots.lock.acquire_async()
        try:
            return self._begin_mutation_owned(
                state, action_id=action_id, tool_name=tool_name,
                wave_id=wave_id, side_effect_policy=side_effect_policy,
            )
        except BaseException:
            self.snapshots.lock.release()
            raise

    def _begin_mutation_owned(
        self, state: AgentState, *, action_id: str, tool_name: str,
        wave_id: str | None, side_effect_policy: str,
    ) -> "MutationBoundary":
        before = self.snapshots.snapshot_unlocked()
        pre = self._checkpoint_unlocked(
            state, reason="before_mutation", wave_id=wave_id,
            action_ids=(action_id,), snapshot=before,
        )
        return MutationBoundary(
            runtime=self, state=state, action_id=action_id, tool_name=tool_name,
            wave_id=wave_id, side_effect_policy=side_effect_policy,
            pre_checkpoint=pre, before_snapshot=before,
        )

    def mutate(self, state: AgentState, operation: Callable[[], T], *,
               action_ids: tuple[str, ...] = (), wave_id: str | None = None) -> T:
        """Run one reversible mutation and durably bind its resulting states."""
        with self.snapshots.lock:
            before_snapshot = self.snapshots.snapshot_unlocked()
            self._checkpoint_unlocked(
                state, reason="before_mutation", wave_id=wave_id,
                action_ids=action_ids, snapshot=before_snapshot,
            )
            error = None
            try:
                result = operation()
            except BaseException as exc:
                error = exc
                result = None
            after_snapshot = self.snapshots.snapshot_unlocked()
            after = self._checkpoint_unlocked(
                state, reason=("failed_mutation" if error else "committed_mutation"),
                wave_id=wave_id,
                action_ids=action_ids, snapshot=after_snapshot,
            )
            self.ledger.record(after, self.snapshots.diff(before_snapshot, after_snapshot))
            if error is not None:
                raise error
            return result

    def rollback(self, target_id: str, *, max_steps: int = 10) -> AgentState:
        """Restore both halves, retaining rollback's source as an undo target."""
        target = self.execution_checkpoints.load(target_id)
        if target is None:
            raise KeyError(f"execution checkpoint not found: {target_id}")
        workspace = (self._workspace_snapshots.get(target.workspace_snapshot_id) or
                     self.snapshots.load(target.workspace_snapshot_id))
        agent = self.agent_checkpoints.load(target.agent_checkpoint_id)
        if workspace is None or agent is None:
            raise RuntimeError("execution checkpoint is incomplete")
        with self.snapshots.lock:
            latest = self.execution_checkpoints.latest(target.session_id)
            # Capture the current state as an execution boundary so rollback is
            # itself reversible. Its logical half is the currently active one.
            current_agent = (self.agent_checkpoints.load(latest.agent_checkpoint_id)
                             if latest else None)
            if current_agent is not None:
                pre_workspace = self.snapshots.snapshot_unlocked()
                self._workspace_snapshots[pre_workspace.workspace_snapshot_id] = pre_workspace
                pre = ExecutionCheckpoint(
                    current_agent.checkpoint_id, pre_workspace.workspace_snapshot_id,
                    current_agent.session_id, current_agent.run_id,
                    parent_execution_checkpoint_id=(
                        latest.execution_checkpoint_id if latest else None),
                    reason="pre_rollback",
                )
                self.execution_checkpoints.save(pre)
            self.snapshots.restore_unlocked(workspace)
            restored = agent.restore(new_run_id=agent.run_id, max_steps=max_steps)
            verified = self.snapshots.snapshot_unlocked()
            if verified.manifest_hash != workspace.manifest_hash:
                raise RuntimeError("rollback workspace verification failed")
            self._workspace_snapshots[verified.workspace_snapshot_id] = verified
            post_agent = AgentCheckpoint.capture(restored)
            self.agent_checkpoints.save(post_agent)
            post = ExecutionCheckpoint(
                post_agent.checkpoint_id, verified.workspace_snapshot_id,
                restored.session_id, restored.run_id,
                parent_execution_checkpoint_id=target.execution_checkpoint_id,
                reason="post_rollback",
            )
            self.execution_checkpoints.save(post)
            if self.recorder:
                self.recorder.record(
                    "workspace_rollback", target_execution_checkpoint_id=target_id,
                    pre_rollback_execution_checkpoint_id=(
                        pre.execution_checkpoint_id if current_agent is not None else None),
                    post_rollback_execution_checkpoint_id=post.execution_checkpoint_id,
                    workspace_manifest_hash=verified.manifest_hash,
                )
            return restored


@dataclass
class MutationBoundary:
    """An owned mutation awaiting logical commit by ``AgentLoop``."""

    runtime: WorkspaceRecoveryRuntime
    state: AgentState
    action_id: str
    tool_name: str
    wave_id: str | None
    side_effect_policy: str
    pre_checkpoint: ExecutionCheckpoint
    before_snapshot: WorkspaceSnapshot
    after_snapshot: WorkspaceSnapshot | None = None
    started_at: float = field(default_factory=time.perf_counter)
    _closed: bool = False

    def capture_after_execution(self) -> WorkspaceSnapshot:
        if self.after_snapshot is None:
            self.after_snapshot = self.runtime.snapshots.snapshot_unlocked()
            self.runtime._workspace_snapshots[
                self.after_snapshot.workspace_snapshot_id
            ] = self.after_snapshot
        return self.after_snapshot

    def commit(self, state: AgentState, *, reason: str = "committed_mutation") -> ExecutionCheckpoint:
        if self._closed:
            raise RuntimeError("mutation boundary is already closed")
        try:
            after = self.capture_after_execution()
            committed = self.runtime._checkpoint_unlocked(
                state, reason=reason, wave_id=self.wave_id,
                action_ids=(self.action_id,), snapshot=after,
            )
            diff = self.runtime.snapshots.diff(self.before_snapshot, after)
            self.runtime.ledger.record(committed, diff)
            if self.runtime.recorder:
                counts = {operation.value: 0 for operation in WorkspaceOperation}
                for change in diff.changes:
                    counts[change.operation.value] += 1
                self.runtime.recorder.record(
                    "workspace_mutation", action_id=self.action_id,
                    wave_id=self.wave_id, tool_name=self.tool_name,
                    side_effect_policy=self.side_effect_policy,
                    pre_execution_checkpoint_id=(
                        self.pre_checkpoint.execution_checkpoint_id),
                    committed_execution_checkpoint_id=(
                        committed.execution_checkpoint_id),
                    workspace_snapshot_id=after.workspace_snapshot_id,
                    changed_file_count=len(diff.changes),
                    created_count=counts[WorkspaceOperation.CREATED.value],
                    modified_count=counts[WorkspaceOperation.MODIFIED.value],
                    deleted_count=counts[WorkspaceOperation.DELETED.value],
                    mutation_duration_ms=(time.perf_counter() - self.started_at) * 1000,
                    recovery_outcome=reason,
                )
            return committed
        finally:
            self._closed = True
            self.runtime.snapshots.lock.release()

    def cancel(self, state: AgentState) -> ExecutionCheckpoint:
        """Durably bind cancellation state before propagating cancellation."""
        return self.commit(state, reason="cancelled_mutation")

    def release_on_setup_error(self) -> None:
        if not self._closed:
            self._closed = True
            self.runtime.snapshots.lock.release()
