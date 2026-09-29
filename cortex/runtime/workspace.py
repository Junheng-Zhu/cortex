"""Content-addressed workspace snapshots stored outside the user's repository."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from uuid import uuid4


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
    """Process-wide exclusive ownership for mutation, snapshot and restore."""

    _guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> threading.RLock:
        key = str(Path(workspace).resolve())
        with cls._guard:
            return cls._locks.setdefault(key, threading.RLock())


class ShadowGitSnapshotStore:
    """Git object database independent from a workspace's own Git metadata."""

    def __init__(self, workspace: str | Path, store_root: str | Path | None = None):
        self.workspace = Path(workspace).resolve()
        identity = hashlib.sha256(str(self.workspace).encode()).hexdigest()
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
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout.decode().strip()

    def _paths(self) -> Iterable[Path]:
        for root, dirs, files in os.walk(self.workspace, topdown=True, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", ".cortex"})
            for name in sorted(files):
                path = Path(root) / name
                relative = path.relative_to(self.workspace)
                if relative.parts[0] not in {".git", ".cortex"}:
                    yield path
            # os.walk puts symlinked directories in dirs; snapshot the link,
            # rather than following or silently losing it.
            links = [d for d in dirs if (Path(root) / d).is_symlink()]
            dirs[:] = [d for d in dirs if d not in links]
            for name in links:
                yield Path(root) / name

    def _capture_files(self) -> tuple[WorkspaceFile, ...]:
        result = []
        for path in self._paths():
            relative = path.relative_to(self.workspace).as_posix()
            if path.is_symlink():
                data = os.readlink(path).encode()
                mode = "120000"
            else:
                data = path.read_bytes()
                mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            oid = self._git("hash-object", "-w", "--stdin", input_data=data)
            result.append(WorkspaceFile(relative, oid, mode))
        return tuple(sorted(result, key=lambda item: item.path))

    def _tree(self, files: tuple[WorkspaceFile, ...]) -> str:
        # A temporary index lets Git build nested trees without touching either
        # the user's index or working tree.
        index = self.git_dir.parent / f"index-{uuid4().hex}"
        env = os.environ.copy()
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
            files = self._capture_files()
            tree = self._tree(files)
            parent = self._git("rev-parse", "--verify", "refs/heads/snapshots") if self._has_head() else None
            args = ["commit-tree", tree, "-m", "Cortex workspace snapshot"]
            if parent:
                args[2:2] = ["-p", parent]
            env = os.environ.copy()
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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                              stderr=subprocess.DEVNULL)
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

    def restore(self, snapshot: WorkspaceSnapshot) -> None:
        if snapshot.workspace_id != self.workspace_id:
            raise ValueError("snapshot belongs to a different workspace")
        with self.lock:
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
                                       item.content_hash], check=True, stdout=subprocess.PIPE).stdout
                if item.mode == "120000":
                    os.symlink(data.decode(), path)
                else:
                    path.write_bytes(data)
                    path.chmod(0o755 if item.mode == "100755" else 0o644)
            actual = self._manifest_hash(self._capture_files())
            if actual != snapshot.manifest_hash:
                raise RuntimeError("restored workspace failed manifest verification")
