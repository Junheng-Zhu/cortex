"""Atomic logical/physical recovery boundaries for an Agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
import time
from dataclasses import asdict, replace
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
    branch_id: str = ""
    excluded_state: dict[str, str] | None = None


@dataclass(frozen=True)
class MutationRecord:
    action_id: str | None
    wave_id: str | None
    execution_checkpoint_id: str
    path: str
    operation: WorkspaceOperation
    before_content_hash: str | None
    after_content_hash: str | None
    before_mode: str | None = None
    after_mode: str | None = None
    session_id: str = ""
    sequence: int = 0


class MutationLedger:
    """Append-only SQLite ledger. Blob ids refer to the durable shadow store."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path else ":memory:"
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("CREATE TABLE IF NOT EXISTS mutations (sequence INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT, path TEXT, checkpoint_id TEXT, session_id TEXT, payload TEXT)")
        for column in ("action_id", "path", "checkpoint_id"):
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS mutation_{column} ON mutations({column})")
        self.connection.commit()

    @property
    def records(self) -> list[MutationRecord]:
        return self.query()

    def query(self, *, action_id=None, path=None, checkpoint_id=None, session_id=None):
        clauses, args = [], []
        for key, value in (("action_id", action_id), ("path", path), ("checkpoint_id", checkpoint_id), ("session_id", session_id)):
            if value is not None:
                clauses.append(key + " = ?")
                args.append(value)
        sql = "SELECT sequence, payload FROM mutations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        result = []
        for seq, payload in self.connection.execute(sql + " ORDER BY sequence", args):
            values = json.loads(payload)
            values["operation"] = WorkspaceOperation(values["operation"])
            values["sequence"] = seq
            result.append(MutationRecord(**values))
        return result

    def record(self, checkpoint: ExecutionCheckpoint, diff: WorkspaceDiff,
               before: WorkspaceSnapshot | None = None,
               after: WorkspaceSnapshot | None = None, *, connection=None, table="mutations") -> None:
        action_id = checkpoint.action_ids[0] if len(checkpoint.action_ids) == 1 else None
        old = {x.path: x.mode for x in before.files} if before else {}
        new = {x.path: x.mode for x in after.files} if after else {}
        db = connection or self.connection
        def append():
            for change in diff.changes:
                record = MutationRecord(action_id, checkpoint.wave_id,
                    checkpoint.execution_checkpoint_id, change.path, change.operation,
                    change.before_content_hash, change.after_content_hash,
                    old.get(change.path), new.get(change.path), checkpoint.session_id)
                db.execute(f"INSERT INTO {table}(action_id,path,checkpoint_id,session_id,payload) VALUES (?,?,?,?,?)",
                    (action_id, change.path, checkpoint.execution_checkpoint_id,
                     checkpoint.session_id, json.dumps(asdict(record))))
        if connection is not None:
            append()
        else:
            with db:
                append()



@dataclass(frozen=True)
class RecoveryResult:
    conflicts: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    checkpoint: ExecutionCheckpoint | None = None
    state: AgentState | None = None

    @property
    def success(self):
        return not self.conflicts


class ExecutionCheckpointStore(Protocol):
    def save(self, checkpoint: ExecutionCheckpoint) -> None: ...
    def load(self, checkpoint_id: str) -> ExecutionCheckpoint | None: ...
    def latest(self, session_id: str) -> ExecutionCheckpoint | None: ...


class InMemoryExecutionCheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: list[ExecutionCheckpoint] = []
        self.heads: dict[str, str] = {}

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        self.checkpoints.append(checkpoint)
        self.heads[checkpoint.branch_id or checkpoint.session_id] = checkpoint.execution_checkpoint_id

    def load(self, checkpoint_id: str) -> ExecutionCheckpoint | None:
        return next((x for x in self.checkpoints
                     if x.execution_checkpoint_id == checkpoint_id), None)

    def head(self, session_id: str):
        return self.load(self.heads[session_id]) if session_id in self.heads else None

    def latest(self, session_id: str) -> ExecutionCheckpoint | None:
        return next((x for x in reversed(self.checkpoints)
                     if x.session_id == session_id), None)


class SQLiteExecutionCheckpointStore:
    """Durable execution-boundary index; snapshots remain in shadow Git."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
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
            connection.execute("CREATE TABLE IF NOT EXISTS execution_heads (session_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO execution_heads SELECT session_id, execution_checkpoint_id FROM execution_checkpoints e WHERE rowid=(SELECT rowid FROM execution_checkpoints WHERE session_id=e.session_id ORDER BY created_at DESC,rowid DESC LIMIT 1)")

    def head(self, session_id: str):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT checkpoint_id FROM execution_heads WHERE session_id=?", (session_id,)).fetchone()
        return self.load(row[0]) if row else None

    def save(self, checkpoint: ExecutionCheckpoint) -> None:
        payload = json.dumps(asdict(checkpoint), default=str)
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO execution_checkpoints VALUES (?, ?, ?, ?)",
                               (checkpoint.execution_checkpoint_id,
                                checkpoint.session_id,
                                checkpoint.created_at.isoformat(), payload))
            connection.execute("INSERT INTO execution_heads VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id", (checkpoint.branch_id or checkpoint.session_id, checkpoint.execution_checkpoint_id))

    def save_boundary(self, checkpoint, ledger, diff, before, after):
        if ledger.path == ":memory:":
            ledger.record(checkpoint, diff, before, after)
            self.save(checkpoint)
            return
        # SQLite rollback-journal transactions atomically commit both databases.
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise RuntimeError("WAL is not supported for atomic history transactions")
            table = "mutations"
            if Path(ledger.path).resolve() != self.path.resolve():
                db.execute("ATTACH DATABASE ? AS ledger", (ledger.path,))
                db.execute("PRAGMA ledger.synchronous=FULL")
                if db.execute("PRAGMA ledger.journal_mode").fetchone()[0] != "delete":
                    raise RuntimeError("WAL ledger is not supported")
                table = "ledger.mutations"
            ledger.record(checkpoint, diff, before, after, connection=db, table=table)
            db.execute("INSERT INTO execution_checkpoints VALUES (?,?,?,?)", (
                checkpoint.execution_checkpoint_id, checkpoint.session_id,
                checkpoint.created_at.isoformat(), json.dumps(asdict(checkpoint), default=str)))
            db.execute("INSERT INTO execution_heads VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id",
                       (checkpoint.branch_id or checkpoint.session_id, checkpoint.execution_checkpoint_id))

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
        self.ledger = ledger or MutationLedger(snapshots.git_dir.parent / "mutations.db")
        self.recorder = recorder
        self._workspace_snapshots: dict[str, WorkspaceSnapshot] = {}

    def active_head(self, session_id):
        head = getattr(self.execution_checkpoints, "head", self.execution_checkpoints.latest)
        scoped = head(self.snapshots.workspace_id + ":" + session_id)
        if scoped is not None:
            return scoped
        legacy = head(session_id)
        return legacy if legacy and self.snapshots.load(legacy.workspace_snapshot_id) else None

    def rollback_action(self, action_id: str, *, state=None, preview=False):
        records = self.ledger.query(action_id=action_id)
        if not records:
            raise KeyError(action_id)
        return self._selective(records, state=state, preview=preview)

    def undo_action(self, action_id: str, *, state=None, preview=False):
        """Undo by appending a protected compensation; returns its redo action id."""
        return self.rollback_action(action_id, state=state, preview=preview)

    def redo_action(self, compensation_action_id: str, *, state=None, preview=False):
        return self.rollback_action(compensation_action_id, state=state, preview=preview)

    def rollback_file(self, path: str, target: str, *, state=None, preview=False):
        from .workspace_paths import safe_path
        safe_path(self.snapshots.workspace, path)
        records = self.ledger.query(path=path)
        selected = [r for r in records if r.execution_checkpoint_id == target or r.action_id == target]
        if not selected:
            raise KeyError(target)
        return self._selective(selected, state=state, preview=preview)

    def _selective(self, records, *, state, preview):
        from .workspace import WorkspaceFile
        from .workspace_paths import validate_manifest, write_file
        from .observation import Observation
        with self.snapshots.lock:
            if getattr(self, "workspace_guard", None):
                self.workspace_guard()
            for record in records:
                source = self.execution_checkpoints.load(record.execution_checkpoint_id)
                if source is None or source.reason == "unrecoverable_mutation":
                    raise RuntimeError("mutation has no safe reversible checkpoint")
            current = self.snapshots.snapshot_unlocked()
            current_map = {x.path: x for x in current.files}
            desired, expected = {}, {}
            for record in records:
                if record.path not in desired:
                    desired[record.path] = (WorkspaceFile(record.path, record.before_content_hash, record.before_mode)
                        if record.before_content_hash and record.before_mode else None)
                expected[record.path] = (WorkspaceFile(record.path, record.after_content_hash, record.after_mode)
                    if record.after_content_hash and record.after_mode else None)
            selected_sequences = {r.sequence for r in records}
            conflicts = set()
            for path in desired:
                first = min(r.sequence for r in records if r.path == path)
                if any(r.sequence >= first and r.sequence not in selected_sequences for r in self.ledger.query(path=path)):
                    conflicts.add(path)
                if current_map.get(path) != expected[path]:
                    conflicts.add(path)
            # Legacy ledger entries without mode cannot safely be inverted.
            conflicts.update(r.path for r in records if
                (r.before_content_hash and not r.before_mode) or (r.after_content_hash and not r.after_mode))
            if conflicts or preview:
                return RecoveryResult(tuple(sorted(conflicts)), tuple(sorted(desired)))
            sessions = {r.session_id for r in records}
            if len(sessions) != 1:
                raise ValueError("rollback spans sessions")
            if state is None:
                head = self.active_head(next(iter(sessions)))
                agent = self.agent_checkpoints.load(head.agent_checkpoint_id) if head else None
                if agent is None:
                    raise RuntimeError("missing active logical checkpoint")
                state = agent.restore(new_run_id=agent.run_id, max_steps=10)
            if state.session_id not in sessions:
                raise ValueError("rollback logical session mismatch")
            files = dict(current_map)
            for path, item in desired.items():
                if item is None:
                    files.pop(path, None)
                else:
                    files[path] = item
            validate_manifest(self.snapshots.workspace, files.values(), self.snapshots.blob)
            # A full pre-boundary remains a recovery target if a write fails.
            self._checkpoint_unlocked(state, reason="before_selective_rollback", wave_id=None,
                                      action_ids=(), snapshot=current)
            try:
                for path, item in desired.items():
                    write_file(self.snapshots.workspace, path, item, self.snapshots.blob)
            except BaseException:
                for path in desired:
                    write_file(self.snapshots.workspace, path, current_map.get(path), self.snapshots.blob)
                raise
            after = self.snapshots.snapshot_unlocked()
            if {x.path: x for x in after.files} != files:
                raise RuntimeError("selective rollback verification failed")
            action = "compensation-" + uuid4().hex
            message = "Workspace rollback completed: " + ", ".join(sorted(desired))
            state.observations.append(Observation(action, True, output=message))
            state.pending_input.append({"role": "user", "content": message})
            post = self._checkpoint_unlocked(state, reason="selective_rollback", wave_id=None,
                                             action_ids=(action,), snapshot=after, before_snapshot=current)
            return RecoveryResult(paths=tuple(sorted(desired)), checkpoint=post, state=state)

    def checkpoint(self, state: AgentState, *, reason: str = "checkpoint",
                   wave_id: str | None = None,
                   action_ids: tuple[str, ...] = ()) -> ExecutionCheckpoint:
        with self.snapshots.lock:
            if getattr(self, "workspace_guard", None):
                self.workspace_guard()
            return self._checkpoint_unlocked(
                state, reason=reason, wave_id=wave_id, action_ids=action_ids
            )

    def _checkpoint_unlocked(
        self, state: AgentState, *, reason: str, wave_id: str | None,
        action_ids: tuple[str, ...], snapshot: WorkspaceSnapshot | None = None,
        before_snapshot: WorkspaceSnapshot | None = None,
    ) -> ExecutionCheckpoint:
        agent = AgentCheckpoint.capture(state)
        workspace = snapshot or self.snapshots.snapshot_unlocked()
        self.agent_checkpoints.save(agent)
        self._workspace_snapshots[workspace.workspace_snapshot_id] = workspace
        parent = self.active_head(state.session_id)
        execution = ExecutionCheckpoint(
            agent.checkpoint_id, workspace.workspace_snapshot_id,
            state.session_id, state.run_id, wave_id, tuple(action_ids),
            parent.execution_checkpoint_id if parent else None, reason,
        )
        execution = replace(execution, branch_id=self.snapshots.workspace_id + ":" + state.session_id,
            excluded_state=self.snapshots.excluded_state() if
            getattr(getattr(self, "workspace_session", None), "mode", None) == "ISOLATED" else None)
        if before_snapshot is not None:
            self._save_mutation(execution, before_snapshot, workspace)
        else:
            self.execution_checkpoints.save(execution)
        if getattr(self, "workspace_manager", None):
            self.workspace_session.active_checkpoint_id = execution.execution_checkpoint_id
            self.workspace_session.run_id = state.run_id
            self.workspace_manager.save(self.workspace_session)
        return execution

    def _save_mutation(self, checkpoint, before, after):
        diff = self.snapshots.diff(before, after)
        save = getattr(self.execution_checkpoints, "save_boundary", None)
        if save:
            save(checkpoint, self.ledger, diff, before, after)
        else:
            self.ledger.record(checkpoint, diff, before, after)
            self.execution_checkpoints.save(checkpoint)

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
        if getattr(self, "workspace_guard", None):
            self.workspace_guard()
        excluded = (self.snapshots.excluded_state() if
                    getattr(getattr(self, "workspace_session", None), "mode", None) == "ISOLATED" else None)
        before = self.snapshots.snapshot_unlocked()
        pre = self._checkpoint_unlocked(
            state, reason="before_mutation", wave_id=wave_id,
            action_ids=(action_id,), snapshot=before,
        )
        return MutationBoundary(
            runtime=self, state=state, action_id=action_id, tool_name=tool_name,
            wave_id=wave_id, side_effect_policy=side_effect_policy,
            pre_checkpoint=pre, before_snapshot=before, excluded_before=excluded,
        )

    def mutate(self, state: AgentState, operation: Callable[[], T], *,
               action_ids: tuple[str, ...] = (), wave_id: str | None = None) -> T:
        """Run one reversible mutation and durably bind its resulting states."""
        with self.snapshots.lock:
            if getattr(self, "workspace_guard", None):
                self.workspace_guard()
            excluded_before = (self.snapshots.excluded_state() if
                getattr(getattr(self, "workspace_session", None), "mode", None) == "ISOLATED" else None)
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
            unsupported = excluded_before is not None and self.snapshots.excluded_state() != excluded_before
            if unsupported and error is None:
                error = RuntimeError("excluded metadata mutation is not reversible")
            after_snapshot = self.snapshots.snapshot_unlocked()
            after = self._checkpoint_unlocked(
                state, reason=("unrecoverable_mutation" if unsupported else "failed_mutation" if error else "committed_mutation"),
                wave_id=wave_id,
                action_ids=action_ids, snapshot=after_snapshot, before_snapshot=before_snapshot,
            )
            if unsupported and getattr(self, "workspace_manager", None):
                self.workspace_session.status = "INVALID"
                self.workspace_manager.save(self.workspace_session)
            if error is not None:
                raise error
            return result

    def rollback(self, target_id: str, *, max_steps: int = 10) -> AgentState:
        """Restore both halves, retaining rollback's source as an undo target."""
        target = self.execution_checkpoints.load(target_id)
        if target is None:
            raise KeyError(f"execution checkpoint not found: {target_id}")
        if target.branch_id and target.branch_id != self.snapshots.workspace_id + ":" + target.session_id:
            raise ValueError("checkpoint belongs to a different workspace branch")
        workspace = (self._workspace_snapshots.get(target.workspace_snapshot_id) or
                     self.snapshots.load(target.workspace_snapshot_id))
        agent = self.agent_checkpoints.load(target.agent_checkpoint_id)
        if workspace is None or agent is None:
            raise RuntimeError("execution checkpoint is incomplete")
        with self.snapshots.lock:
            if getattr(self, "workspace_guard", None):
                self.workspace_guard()
            if target.excluded_state is not None and self.snapshots.excluded_state() != target.excluded_state:
                raise RuntimeError("excluded metadata changed; full rollback cannot safely restore it")
            latest = self.active_head(target.session_id)
            # Capture the current state as an execution boundary so rollback is
            # itself reversible. Its logical half is the currently active one.
            current_agent = (self.agent_checkpoints.load(latest.agent_checkpoint_id)
                             if latest else None)
            pre_workspace = self.snapshots.snapshot_unlocked()
            if current_agent is not None:
                self._workspace_snapshots[pre_workspace.workspace_snapshot_id] = pre_workspace
                pre = ExecutionCheckpoint(
                    current_agent.checkpoint_id, pre_workspace.workspace_snapshot_id,
                    current_agent.session_id, current_agent.run_id,
                    parent_execution_checkpoint_id=(
                        latest.execution_checkpoint_id if latest else None),
                    reason="pre_rollback",
                )
                pre = replace(pre, branch_id=self.snapshots.workspace_id + ":" + target.session_id, excluded_state=target.excluded_state)
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
                parent_execution_checkpoint_id=(pre.execution_checkpoint_id if current_agent else target.execution_checkpoint_id),
                reason="post_rollback",
            )
            post = replace(post, branch_id=self.snapshots.workspace_id + ":" + target.session_id, excluded_state=target.excluded_state)
            self._save_mutation(post, pre_workspace, verified)
            if getattr(self, "workspace_manager", None):
                self.workspace_session.active_checkpoint_id = post.execution_checkpoint_id
                self.workspace_manager.save(self.workspace_session)
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
    excluded_before: dict[str, str] | None = None
    unsupported_paths: tuple[str, ...] = ()
    started_at: float = field(default_factory=time.perf_counter)
    _closed: bool = False

    def capture_after_execution(self) -> WorkspaceSnapshot:
        if self.after_snapshot is None:
            if self.excluded_before is not None:
                current = self.runtime.snapshots.excluded_state()
                self.unsupported_paths = tuple(sorted(p for p in current.keys() | self.excluded_before.keys()
                    if current.get(p) != self.excluded_before.get(p)))
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
            if self.unsupported_paths:
                reason = "unrecoverable_mutation"
            committed = self.runtime._checkpoint_unlocked(
                state, reason=reason, wave_id=self.wave_id,
                action_ids=(self.action_id,), snapshot=after, before_snapshot=self.before_snapshot,
            )
            diff = self.runtime.snapshots.diff(self.before_snapshot, after)
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
            if self.unsupported_paths and getattr(self.runtime, "workspace_manager", None):
                self.runtime.workspace_session.status = "INVALID"
                self.runtime.workspace_manager.save(self.runtime.workspace_session)
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
