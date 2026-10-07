"""Workspace V2 lifecycle; no model tool can apply to the main workspace."""
from dataclasses import dataclass, asdict
from enum import Enum
import json
import asyncio
import threading
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from .workspace import ShadowGitSnapshotStore, WorkspaceWriteLock, git_environment
from .workspace_paths import validate_manifest, safe_path, write_file
from .execution_checkpoint import RecoveryResult


class WorkspaceMode(str, Enum):
    DIRECT = 'DIRECT'
    ISOLATED = 'ISOLATED'


@dataclass
class WorkspaceSession:
    session_id: str
    run_id: str
    owner_id: str
    mode: WorkspaceMode
    main_root: str
    execution_root: str
    base_snapshot_id: str
    provider: str
    generation: tuple[int, int]
    main_generation: tuple[int, int]
    status: str = 'ACTIVE'
    active_checkpoint_id: str | None = None
    audit_note: str | None = None


class SnapshotWorkspaceProvider:
    name = 'snapshot'

    def create(self, main, destination, snapshot):
        destination.mkdir()
        validate_manifest(destination, snapshot.files, main.blob)
        for item in snapshot.files:
            write_file(destination, item.path, item, main.blob)
        # Independent metadata, never a copy of the user's index or config.
        # Git commands operate on a synthetic BASE commit; staged state is not copied.
        # Synthetic BASE repository, including non-Git input.
        subprocess.run(['git', 'init', '-q', str(destination)], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(destination), 'add', '--force', '--all'], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(destination), '-c', 'user.name=Cortex',
            '-c', 'user.email=cortex@localhost', 'commit', '--allow-empty', '-qm', 'Workspace BASE'], check=True, env=git_environment())

    def remove(self, main, destination):
        shutil.rmtree(destination)


class GitWorktreeProvider(SnapshotWorkspaceProvider):
    name = 'git-worktree'

    def create(self, main, destination, snapshot):
        # Linked metadata would expose main/common-dir to Local and be unusable
        # in Docker. Create an independent clone and a detached worktree there.
        repository = destination.parent / (destination.name + '-git')
        subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-checkout',
                        str(main.workspace), str(repository)], check=True, env=git_environment())
        head = subprocess.check_output(['git', '-C', str(main.workspace), 'rev-parse', 'HEAD'], env=git_environment()).decode().strip()
        subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '--quiet', '--detach',
                        str(destination), head], check=True, env=git_environment())
        # Move private metadata inside execution_root: Docker mounts one root.
        metadata = destination.parent / (destination.name + '-metadata')
        subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-checkout',
                        str(repository), str(metadata)], check=True, env=git_environment())
        (destination / '.git').unlink()
        shutil.move(str(metadata / '.git'), destination / '.git')
        shutil.rmtree(metadata)
        shutil.rmtree(repository)
        subprocess.run(['git', '-C', str(destination), 'update-ref', '--no-deref', 'HEAD', head], check=True, env=git_environment())
        subprocess.run(['git', '-C', str(destination), 'reset', '--mixed', '--quiet', head], check=True, env=git_environment())
        # Includes ignored files present in BASE, even for a clean Git status.
        actual = {x.relative_to(destination).as_posix() for x in destination.rglob('*')
                  if x.is_file() and '.git' not in x.relative_to(destination).parts}
        wanted = {x.path for x in snapshot.files}
        for relative in actual - wanted:
            safe_path(destination, relative).unlink()
        validate_manifest(destination, snapshot.files, main.blob)
        for item in snapshot.files:
            write_file(destination, item.path, item, main.blob)


class WorkspaceManager:
    def __init__(self, storage_root: str | Path):
        self.root = Path(storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'sessions.db'
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS apply_journal (id TEXT PRIMARY KEY, session_id TEXT, status TEXT, payload TEXT)')

    def _db(self):
        db = sqlite3.connect(self.database)
        db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            db.close()
            raise RuntimeError('WAL is not supported for durable Apply journal')
        return db

    def save(self, session):
        with self._db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (session.session_id, json.dumps(asdict(session))))

    def load(self, session_id):
        with self._db() as db:
            row = db.execute('SELECT payload FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        values = json.loads(row[0])
        values['mode'] = WorkspaceMode(values['mode'])
        values['generation'] = tuple(values['generation'])
        values['main_generation'] = tuple(values['main_generation'])
        session = WorkspaceSession(**values)
        if session.status == 'APPLYING':
            with self._db() as db:
                journal = db.execute('SELECT status,payload FROM apply_journal WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
            if journal and journal[0] in {'COMMITTED', 'RESTORED'}:
                session.status = 'APPLIED' if journal[0] == 'COMMITTED' else 'ACTIVE'
                session.active_checkpoint_id = json.loads(journal[1]).get('checkpoint', session.active_checkpoint_id)
                self.save(session)
        return session

    @staticmethod
    def generation(root):
        st = Path(root).stat()
        return st.st_dev, st.st_ino

    def validate(self, session, *, active=True):
        stored = self.load(session.session_id)
        if (stored.execution_root != session.execution_root or stored.generation != session.generation or
                (active and stored.status != 'ACTIVE')):
            raise RuntimeError('stale or inactive workspace session')
        if session.mode is WorkspaceMode.ISOLATED:
            marker = Path(session.execution_root) / '.cortex/workspace-session'
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != session.session_id:
                raise RuntimeError('stale workspace session generation marker')
        if (self.generation(session.execution_root) != session.generation or
                self.generation(session.main_root) != session.main_generation):
            raise RuntimeError('stale workspace generation')

    def snapshots(self, root):
        return ShadowGitSnapshotStore(root, self.root / 'snapshots')

    def create(self, main_root, *, mode=WorkspaceMode.DIRECT, owner_id='user', session_id=None, run_id=None):
        main_root = Path(main_root).resolve()
        if not main_root.is_dir():
            raise ValueError('workspace must exist')
        if self.root.is_relative_to(main_root):
            raise ValueError('runtime storage must be outside workspace')
        mode = WorkspaceMode(mode)
        identity = session_id or uuid4().hex
        with self._db() as db:
            if db.execute('SELECT 1 FROM sessions WHERE id=?', (identity,)).fetchone():
                raise ValueError('workspace session already exists; use load')
        if mode is WorkspaceMode.DIRECT:
            # DIRECT remains lazy: constructing an Agent must not eagerly scan/snapshot.
            with WorkspaceWriteLock.for_workspace(main_root):
                session = WorkspaceSession(identity, run_id or uuid4().hex, owner_id, mode,
                    str(main_root), str(main_root), "", "direct", self.generation(main_root),
                    self.generation(main_root))
                self.save(session)
            return session
        main = self.snapshots(main_root)
        destination = self.root / ('execution-' + uuid4().hex)
        provider = SnapshotWorkspaceProvider()
        with main.lock:
            base = main.snapshot_unlocked()
            if mode is WorkspaceMode.ISOLATED:
                validate_manifest(destination, base.files, main.blob)
                git = subprocess.run(['git', '-C', str(main_root), 'status', '--porcelain', '--untracked-files=all'],
                                     capture_output=True, env=git_environment())
                head = subprocess.run(['git', '-C', str(main_root), 'rev-parse', '--verify', 'HEAD'], capture_output=True, env=git_environment())
                if git.returncode == 0 and not git.stdout and head.returncode == 0:
                    provider = GitWorktreeProvider()
                try:
                    provider.create(main, destination, base)
                    marker = destination / '.cortex'
                    marker.mkdir(exist_ok=True)
                    (marker / 'workspace-session').write_text(identity)
                    execution = self.snapshots(destination)
                    if execution.snapshot().manifest_hash != base.manifest_hash:
                        raise RuntimeError('isolated BASE verification failed')
                except BaseException:
                    for resource in (destination, destination.parent / (destination.name + '-git'),
                                     destination.parent / (destination.name + '-metadata')):
                        shutil.rmtree(resource, ignore_errors=True)
                    raise
            else:
                destination = main_root
        session = WorkspaceSession(identity, run_id or uuid4().hex, owner_id, mode,
            str(main_root), str(destination), base.snapshot_id, provider.name,
            self.generation(destination), self.generation(main_root))
        self.save(session)
        return session

    def final_diff(self, session):
        self.validate(session)
        main = self.snapshots(session.main_root)
        execution = self.snapshots(session.execution_root)
        with execution.lock:
            final = execution.snapshot_unlocked()
        return main.diff(main.load(session.base_snapshot_id), final)

    def _journal(self, identity, session, status, payload):
        with self._db() as db:
            db.execute('INSERT INTO apply_journal VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                       (identity, session.session_id, status, json.dumps(payload)))

    def pending_applies(self):
        with self._db() as db:
            return [(x[0], x[1], x[2], json.loads(x[3])) for x in db.execute(
                "SELECT * FROM apply_journal WHERE status NOT IN ('COMMITTED','RESTORED')")]

    def apply(self, session, *, runtime, state, executor=None, fault=None, cancel_event=None):
        if session.mode is not WorkspaceMode.ISOLATED:
            raise ValueError('Apply requires isolated workspace')
        execution = self.snapshots(session.execution_root)
        main = self.snapshots(session.main_root)
        if runtime.snapshots.workspace != execution.workspace:
            raise ValueError('runtime workspace mismatch')
        from .execution_checkpoint import SQLiteExecutionCheckpointStore
        from .checkpoint import SQLiteCheckpointStore
        if not isinstance(runtime.agent_checkpoints, SQLiteCheckpointStore):
            raise ValueError('durable Apply requires SQLiteCheckpointStore')
        if not isinstance(runtime.execution_checkpoints, SQLiteExecutionCheckpointStore):
            raise ValueError('durable Apply requires SQLiteExecutionCheckpointStore')
        with sqlite3.connect(runtime.execution_checkpoints.path) as history:
            if history.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise RuntimeError('WAL is not supported for durable Apply history')
        # Order is always execution then main. Drains active Tool ownership.
        with execution.lock:
            self.validate(session)
            if executor is not None:
                executor.close()
            with main.lock:
                self.validate(session)
                if any(p[3]['main_root'] == session.main_root for p in self.pending_applies()):
                    raise RuntimeError('unfinished apply journal requires recovery')
                base = main.load(session.base_snapshot_id)
                if base is None:
                    raise RuntimeError('missing BASE snapshot')
                final = execution.snapshot_unlocked()
                current = main.snapshot_unlocked()
                old = {x.path: x for x in base.files}
                new = {x.path: x for x in final.files}
                now = {x.path: x for x in current.files}
                paths = tuple(c.path for c in main.diff(base, final))
                conflicts = tuple(p for p in paths if now.get(p) != old.get(p))
                if conflicts:
                    return RecoveryResult(conflicts, paths)
                merged = dict(now)
                for path in paths:
                    if path in new:
                        merged[path] = new[path]
                    else:
                        merged.pop(path, None)
                validate_manifest(main.workspace, merged.values(),
                                  lambda oid: execution.blob(oid) if oid in {x.content_hash for x in final.files} else main.blob(oid))
                # Preflight every affected path before journaling or writing.
                for path in paths:
                    target = safe_path(main.workspace, path)
                    if target.exists() and target.is_dir() and not target.is_symlink():
                        raise ValueError('directory collision: ' + path)
                identity = uuid4().hex
                payload = dict(main_root=session.main_root, backup=current.snapshot_id,
                               final=final.snapshot_id, paths=paths, main_generation=session.main_generation)
                self._journal(identity, session, 'PREPARED', payload)
                session.status = 'APPLYING'
                self.save(session)
                try:
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError()
                    self._journal(identity, session, 'WRITING', payload)
                    for index, path in enumerate(paths):
                        if cancel_event and cancel_event.is_set():
                            raise asyncio.CancelledError()
                        observed = {x.path: x for x in main._capture_files()}
                        if observed.get(path) != now.get(path):
                            raise RuntimeError('main changed during Apply: ' + path)
                        write_file(main.workspace, path, new.get(path), execution.blob)
                        if fault:
                            fault(index, path)
                    verified = main.snapshot_unlocked()
                    if {x.path: x for x in verified.files} != merged:
                        raise RuntimeError('Apply verification failed')
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError()
                    from .checkpoint import AgentCheckpoint
                    from .execution_checkpoint import ExecutionCheckpoint, SQLiteExecutionCheckpointStore
                    agent = AgentCheckpoint.capture(state)
                    runtime.agent_checkpoints.save(agent)
                    parent = runtime.active_head(state.session_id)
                    post = ExecutionCheckpoint(agent.checkpoint_id, final.snapshot_id,
                        state.session_id, state.run_id, reason='final_apply',
                        parent_execution_checkpoint_id=parent.execution_checkpoint_id if parent else None,
                        branch_id=execution.workspace_id + ':' + state.session_id,
                        excluded_state=execution.excluded_state())
                    payload['checkpoint'] = post.execution_checkpoint_id
                    payload['applied_snapshot'] = verified.snapshot_id
                    if not isinstance(runtime.execution_checkpoints, SQLiteExecutionCheckpointStore):
                        raise ValueError('durable Apply requires SQLiteExecutionCheckpointStore')
                    # Journal success, final ExecutionCheckpoint and branch head commit together.
                    with self._db() as db:
                        history = 'main'
                        if runtime.execution_checkpoints.path.resolve() != self.database.resolve():
                            db.execute('ATTACH DATABASE ? AS history', (str(runtime.execution_checkpoints.path),))
                            db.execute('PRAGMA history.synchronous=FULL')
                            history = 'history'
                        db.execute(f'INSERT INTO {history}.execution_checkpoints VALUES (?,?,?,?)',
                            (post.execution_checkpoint_id, post.session_id, post.created_at.isoformat(), json.dumps(asdict(post), default=str)))
                        db.execute(f'INSERT INTO {history}.execution_heads VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id',
                            (post.branch_id, post.execution_checkpoint_id))
                        db.execute('UPDATE apply_journal SET status=?,payload=? WHERE id=?',
                            ('COMMITTED', json.dumps(payload), identity))
                    session.status = 'APPLIED'
                    session.active_checkpoint_id = post.execution_checkpoint_id
                    self.save(session)
                    return RecoveryResult(paths=paths, checkpoint=post, state=state)
                except BaseException:
                    with self._db() as db:
                        committed = db.execute('SELECT status FROM apply_journal WHERE id=?', (identity,)).fetchone()
                    if committed and committed[0] == 'COMMITTED':
                        # The data and final checkpoint are committed. load() reconciles metadata.
                        raise
                    try:
                        self._restore_apply(session, payload, main, execution)
                        self._journal(identity, session, 'RESTORED', payload)
                        session.status = 'ACTIVE'
                        self.save(session)
                    except BaseException:
                        self._journal(identity, session, 'RECOVERY_REQUIRED', payload)
                    raise

    async def aapply(self, session, *, runtime, state, executor=None, fault=None):
        """Drain async Tools without blocking their event loop; await cleanup on cancel."""
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self.apply, session,
            runtime=runtime, state=state, executor=executor, fault=fault, cancel_event=cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(task)
            except BaseException:
                pass
            raise

    def _restore_apply(self, session, payload, main, execution):
        if self.generation(main.workspace) != tuple(payload['main_generation']):
            raise RuntimeError('stale main workspace generation')
        backup = main.load(payload['backup'])
        final = execution.load(payload['final'])
        if backup is None or final is None:
            raise RuntimeError('missing apply recovery snapshot')
        old = {x.path: x for x in backup.files}
        new = {x.path: x for x in final.files}
        now = {x.path: x for x in main.snapshot_unlocked().files}
        # Fail closed after a crash if the user changed even one involved file.
        if any(now.get(p) not in (old.get(p), new.get(p)) for p in payload['paths']):
            raise RuntimeError('user changes conflict with apply recovery')
        for path in payload['paths']:
            write_file(main.workspace, path, old.get(path), main.blob)
        verified = {x.path: x for x in main.snapshot_unlocked().files}
        if any(verified.get(p) != old.get(p) for p in payload['paths']):
            raise RuntimeError('apply recovery verification failed')

    def recover_apply(self, journal_id):
        with self._db() as db:
            row = db.execute('SELECT session_id,status,payload FROM apply_journal WHERE id=?', (journal_id,)).fetchone()
        if row is None:
            raise KeyError(journal_id)
        session = self.load(row[0])
        if row[1] in {'COMMITTED', 'RESTORED'}:
            return
        main, execution = self.snapshots(session.main_root), self.snapshots(session.execution_root)
        with execution.lock, main.lock:
            with self._db() as db:
                latest = db.execute('SELECT status,payload FROM apply_journal WHERE id=?', (journal_id,)).fetchone()
            if latest[0] in {'COMMITTED', 'RESTORED'}:
                return
            row = (row[0], latest[0], latest[1])
            self._restore_apply(session, json.loads(row[2]), main, execution)
            self._journal(journal_id, session, 'RESTORED', json.loads(row[2]))
            session.status = 'ACTIVE'
            self.save(session)

    def discard(self, session, *, executor=None):
        if session.mode is not WorkspaceMode.ISOLATED:
            raise ValueError('Discard requires isolated workspace')
        execution = self.snapshots(session.execution_root)
        with execution.lock:
            self.validate(session, active=False)
            if self.load(session.session_id).status not in {'ACTIVE', 'INVALID'}:
                raise RuntimeError('cannot discard applying or completed session')
            if executor:
                executor.close()
            try:
                execution.snapshot_unlocked()
            except ValueError as error:
                session.audit_note = "Discard retained prior snapshots; final snapshot unsupported: " + str(error)
            shutil.rmtree(session.execution_root)
            session.status = 'DISCARDED'
            self.save(session)
