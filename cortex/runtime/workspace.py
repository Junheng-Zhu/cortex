"""Content-addressed workspace snapshots stored outside the user's repository."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import threading
import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from uuid import uuid4


def git_environment():
    """User Git selectors must never redirect the shadow database/index."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"})
    return env


class WorkspaceOperation(str, Enum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


@dataclass(frozen=True)
class WorkspaceFile:
    path: str
    content_hash: str
    mode: str = "100644"


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_snapshot_id: str
    workspace_id: str
    commit_id: str
    tree_id: str
    manifest_hash: str
    files: tuple[WorkspaceFile, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def snapshot_id(self) -> str:
        return self.workspace_snapshot_id


@dataclass(frozen=True)
class WorkspaceChange:
    path: str
    operation: WorkspaceOperation
    before_content_hash: str | None
    after_content_hash: str | None


@dataclass(frozen=True)
class WorkspaceDiff:
    changes: tuple[WorkspaceChange, ...]

    def __iter__(self):
        return iter(self.changes)


class WorkspaceWriteLock:
    """Workspace-scoped READ/WRITE ownership across sync, async and processes.

    Windows conservatively serializes readers; POSIX readers use shared flock.
    """
    _guard = threading.Lock()
    _locks = {}

    def __init__(self, key=""):
        self._key = key
        self._mutex = threading.Lock()
        self._readers = 0
        self._writer = False
        self._file = None

    def _try(self, read=False):
        with self._mutex:
            if self._writer or ((not read or os.name == "nt") and self._readers):
                return None
            directory = Path(tempfile.gettempdir()) / "cortex-workspace-locks"
            directory.mkdir(exist_ok=True)
            handle = open(directory / hashlib.sha256(self._key.encode()).hexdigest(), "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if read else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
            if read:
                self._readers += 1
            else:
                self._writer = True
            return handle

    def _release(self, handle, read=False):
        with self._mutex:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            if read:
                self._readers -= 1
            else:
                self._writer = False

    def __enter__(self):
        import time
        while True:
            handle = self._try()
            if handle is not None:
                self._file = handle
                return self
            time.sleep(0.01)

    def __exit__(self, *_args):
        self.release()

    async def acquire_async(self):
        while True:
            handle = self._try()
            if handle is not None:
                self._file = handle
                return
            await asyncio.sleep(0.01)

    def release(self):
        handle, self._file = self._file, None
        self._release(handle)

    def read(self):
        return WorkspaceReadOwnership(self)

    @classmethod
    def for_workspace(cls, workspace):
        key = os.path.normcase(str(Path(workspace).resolve()))
        with cls._guard:
            return cls._locks.setdefault(key, cls(key))


class WorkspaceReadOwnership:
    def __init__(self, lock):
        self.lock, self.handle = lock, None

    def __enter__(self):
        import time
        while self.handle is None:
            self.handle = self.lock._try(read=True)
            if self.handle is None:
                time.sleep(0.01)
        return self

    async def acquire_async(self):
        while self.handle is None:
            self.handle = self.lock._try(read=True)
            if self.handle is None:
                await asyncio.sleep(0.01)

    def release(self):
        if self.handle is not None:
            self.lock._release(self.handle, read=True)
            self.handle = None

    def __exit__(self, *_args):
        self.release()


class ShadowGitSnapshotStore:
    """Git object database independent from a workspace's own Git metadata."""

    def __init__(self, workspace: str | Path, store_root: str | Path | None = None):
        self.workspace = Path(workspace).resolve()
        info = self.workspace.stat()
        self.generation = (info.st_dev, info.st_ino)
        identity = hashlib.sha256(os.path.normcase(str(self.workspace)).encode()).hexdigest()
        self.workspace_id = identity
        root = Path(store_root or (Path.home() / ".cortex" / "workspace-snapshots"))
        self.git_dir = root.expanduser().resolve() / identity / "shadow.git"
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        if not self.git_dir.exists():
            self._git("init", "--bare", str(self.git_dir), git_dir=False)
        self._snapshots: dict[str, WorkspaceSnapshot] = {}
        self.lock = WorkspaceWriteLock.for_workspace(self.workspace)

    def _git(self, *args: str, input_data: bytes | None = None,
             git_dir: bool = True) -> str:
        command = ["git"]
        if git_dir:
            command += [f"--git-dir={self.git_dir}"]
        result = subprocess.run(command + list(args), input=input_data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_environment())
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout.decode().strip()

    def _paths(self) -> Iterable[Path]:
        for root, dirs, files in os.walk(self.workspace, topdown=True, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d.casefold() not in {".git", ".cortex"})
            for name in sorted(files):
                path = Path(root) / name
                relative = path.relative_to(self.workspace)
                if not any(p.casefold() in {".git", ".cortex"} for p in relative.parts):
                    yield path
            # os.walk puts symlinked directories in dirs; snapshot the link,
            # rather than following or silently losing it.
            links = [d for d in dirs if (Path(root) / d).is_symlink()]
            dirs[:] = [d for d in dirs if d not in links]
            for name in links:
                yield Path(root) / name

    def excluded_state(self):
        """Fingerprint excluded metadata; never describe it as reversible data."""
        values = {}
        for root, dirs, files in os.walk(self.workspace, followlinks=False):
            relative = Path(root).relative_to(self.workspace)
            excluded = any(p.casefold() in {".git", ".cortex"} for p in relative.parts)
            if excluded:
                values[relative.as_posix() + "/"] = "directory"
            for name in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
                path = Path(root) / name
                rel = path.relative_to(self.workspace)
                if not excluded and name.casefold() not in {".git", ".cortex"}:
                    continue
                info = path.lstat()
                if path.is_symlink():
                    data = os.readlink(path).encode()
                elif stat.S_ISREG(info.st_mode):
                    data = path.read_bytes()
                else:
                    raise ValueError("unsupported excluded metadata file type")
                values[rel.as_posix()] = str(info.st_mode) + ":" + hashlib.sha256(data).hexdigest()
        return values

    def _capture_files(self) -> tuple[WorkspaceFile, ...]:
        from .workspace_paths import safe_path, validate_manifest
        info = self.workspace.stat()
        if (info.st_dev, info.st_ino) != self.generation:
            raise RuntimeError("stale workspace generation")
        result = []
        for path in self._paths():
            relative = path.relative_to(self.workspace).as_posix()
            safe_path(self.workspace, relative)
            if path.is_symlink():
                data = os.readlink(path).encode()
                mode = "120000"
            else:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError(f"unsupported workspace file type: {relative}")
                data = path.read_bytes()
                mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            oid = self._git("hash-object", "-w", "--stdin", input_data=data)
            result.append(WorkspaceFile(relative, oid, mode))
        files = tuple(sorted(result, key=lambda item: item.path))
        validate_manifest(self.workspace, files, self.blob)
        return files

    def _tree(self, files: tuple[WorkspaceFile, ...]) -> str:
        # A temporary index lets Git build nested trees without touching either
        # the user's index or working tree.
        index = self.git_dir.parent / f"index-{uuid4().hex}"
        env = git_environment()
        env.update({"GIT_DIR": str(self.git_dir), "GIT_INDEX_FILE": str(index)})
        try:
            for item in files:
                subprocess.run(
                    ["git", "update-index", "--add", "--cacheinfo",
                     item.mode, item.content_hash, item.path], env=env, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            proc = subprocess.run(["git", "write-tree"], env=env, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc.stdout.decode().strip()
        finally:
            index.unlink(missing_ok=True)

    @staticmethod
    def _manifest_hash(files: tuple[WorkspaceFile, ...]) -> str:
        body = "".join(f"{x.mode} {x.content_hash} {x.path}\n" for x in files)
        return hashlib.sha256(body.encode()).hexdigest()

    def snapshot(self) -> WorkspaceSnapshot:
        with self.lock:
            return self.snapshot_unlocked()

    def snapshot_unlocked(self) -> WorkspaceSnapshot:
        """Capture while the caller already owns ``lock``."""
        files = self._capture_files()
        tree = self._tree(files)
        parent = self._git("rev-parse", "--verify", "refs/heads/snapshots") if self._has_head() else None
        args = ["commit-tree", tree, "-m", "Cortex workspace snapshot"]
        if parent:
            args[2:2] = ["-p", parent]
        env = git_environment()
        env.update({"GIT_AUTHOR_NAME": "Cortex", "GIT_AUTHOR_EMAIL": "cortex@localhost",
                    "GIT_COMMITTER_NAME": "Cortex", "GIT_COMMITTER_EMAIL": "cortex@localhost"})
        proc = subprocess.run(["git", f"--git-dir={self.git_dir}", *args], env=env,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        commit = proc.stdout.decode().strip()
        self._git("update-ref", "refs/heads/snapshots", commit)
        snap = WorkspaceSnapshot(commit, self.workspace_id, commit, tree,
                                 self._manifest_hash(files), files)
        self._snapshots[snap.workspace_snapshot_id] = snap
        return snap

    def load(self, snapshot_id: str) -> WorkspaceSnapshot | None:
        """Rehydrate a snapshot from the durable shadow object database."""
        cached = self._snapshots.get(snapshot_id)
        if cached is not None:
            return cached
        proc = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", "ls-tree", "-rz", "-r", snapshot_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_environment(),
        )
        if proc.returncode:
            return None
        files = []
        for record in proc.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, _kind, oid = metadata.decode().split()
            files.append(WorkspaceFile(raw_path.decode(), oid, mode))
        values = tuple(sorted(files, key=lambda item: item.path))
        tree = self._git("rev-parse", f"{snapshot_id}^{{tree}}")
        snapshot = WorkspaceSnapshot(snapshot_id, self.workspace_id, snapshot_id,
                                     tree, self._manifest_hash(values), values)
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    def _has_head(self) -> bool:
        proc = subprocess.run(["git", f"--git-dir={self.git_dir}", "rev-parse", "--verify",
                               "refs/heads/snapshots"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=git_environment())
        return proc.returncode == 0

    def diff(self, before: WorkspaceSnapshot,
             after: WorkspaceSnapshot | None = None) -> WorkspaceDiff:
        if after is None:
            files = self._capture_files()
            after_map = {x.path: x for x in files}
        else:
            after_map = {x.path: x for x in after.files}
        before_map = {x.path: x for x in before.files}
        changes = []
        for path in sorted(before_map.keys() | after_map.keys()):
            old, new = before_map.get(path), after_map.get(path)
            if old is None:
                op = WorkspaceOperation.CREATED
            elif new is None:
                op = WorkspaceOperation.DELETED
            elif old.content_hash != new.content_hash or old.mode != new.mode:
                op = WorkspaceOperation.MODIFIED
            else:
                continue
            changes.append(WorkspaceChange(path, op, old.content_hash if old else None,
                                           new.content_hash if new else None))
        return WorkspaceDiff(tuple(changes))

    def blob(self, oid: str) -> bytes:
        return subprocess.run(["git", f"--git-dir={self.git_dir}", "cat-file", "blob", oid],
                              check=True, stdout=subprocess.PIPE, env=git_environment()).stdout

    def restore(self, snapshot: WorkspaceSnapshot) -> None:
        if snapshot.workspace_id != self.workspace_id:
            raise ValueError("snapshot belongs to a different workspace")
        with self.lock:
            self.restore_unlocked(snapshot)

    def restore_unlocked(self, snapshot: WorkspaceSnapshot) -> None:
        """Restore while the caller already owns ``lock``."""
        if snapshot.workspace_id != self.workspace_id:
            raise ValueError("snapshot belongs to a different workspace")
        from .workspace_paths import validate_manifest
        self._capture_files()  # Generation, path and current file-type preflight.
        validate_manifest(self.workspace, snapshot.files, self.blob)
        for item in snapshot.files:
            leaf = self.workspace / item.path
            if leaf.is_dir() and not leaf.is_symlink():
                raise ValueError("directory collision during full restore: " + item.path)
        wanted = {item.path: item for item in snapshot.files}
        for path in sorted(self._paths(), key=lambda p: len(p.parts), reverse=True):
            relative = path.relative_to(self.workspace).as_posix()
            if relative not in wanted:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        for relative, item in wanted.items():
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            data = subprocess.run(["git", f"--git-dir={self.git_dir}", "cat-file", "blob",
                                   item.content_hash], check=True, stdout=subprocess.PIPE, env=git_environment()).stdout
            if item.mode == "120000":
                os.symlink(data.decode(), path)
            else:
                path.write_bytes(data)
                path.chmod(0o755 if item.mode == "100755" else 0o644)
        actual = self._manifest_hash(self._capture_files())
        if actual != snapshot.manifest_hash:
            raise RuntimeError("restored workspace failed manifest verification")
