"""Workspace cleanup tests run on every platform, including Windows."""
import os
from pathlib import Path
import stat
import subprocess

import pytest

from cortex.runtime import workspace_cleanup as cleanup
from cortex.runtime import workspace_session as sessions
from cortex.runtime.workspace_session import WorkspaceManager


def test_remove_tree_deletes_normal_and_readonly_files(tmp_path):
    root = tmp_path / 'tree'
    (root / 'nested').mkdir(parents=True)
    (root / 'nested/normal').write_text('normal')
    readonly = root / 'nested/readonly'
    readonly.write_text('Git object')
    readonly.chmod(stat.S_IREAD)
    cleanup._remove_tree(root)
    assert not root.exists()


def test_remove_tree_missing_directory_is_safe(tmp_path):
    cleanup._remove_tree(tmp_path / 'absent')


def test_permission_callback_makes_file_writable_and_retries(tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    root.mkdir()
    readonly = root / 'object'
    readonly.write_text('object')
    readonly.chmod(stat.S_IREAD)
    original_rmtree = cleanup.shutil.rmtree
    original_unlink = os.unlink
    attempts = []
    def unlink(path, *args, **kwargs):
        attempts.append(Path(path))
        assert Path(path).stat().st_mode & stat.S_IWUSR
        original_unlink(path, *args, **kwargs)
    def simulate_windows_error(path, *, onerror):
        error = PermissionError('WinError 5: access denied')
        onerror(os.unlink, str(readonly), (PermissionError, error, None))
        original_rmtree(path)
    monkeypatch.setattr(cleanup.os, 'unlink', unlink)
    monkeypatch.setattr(cleanup.shutil, 'rmtree', simulate_windows_error)
    cleanup._remove_tree(root)
    assert attempts == [readonly]
    assert not root.exists()


@pytest.mark.parametrize('error_type', [OSError, PermissionError])
def test_cleanup_propagates_unrecoverable_errors(tmp_path, monkeypatch, error_type):
    root = tmp_path / 'tree'
    root.mkdir()
    target = root / 'locked'
    target.write_text('locked')
    error = error_type('persistent failure')
    def fail_unlink(*args, **kwargs):
        raise error
    def failing_rmtree(path, *, onerror):
        onerror(os.unlink, str(target), (error_type, error, None))
    monkeypatch.setattr(cleanup.os, 'unlink', fail_unlink)
    monkeypatch.setattr(cleanup.shutil, 'rmtree', failing_rmtree)
    with pytest.raises(error_type, match='persistent failure'):
        cleanup._remove_tree(root)
    assert target.read_text() == 'locked'


def test_cleanup_chmod_failure_is_not_suppressed(tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    root.mkdir()
    target = root / 'locked'
    target.write_text('locked')
    def denied_chmod(*args, **kwargs):
        raise PermissionError('chmod denied')
    def failing_rmtree(path, *, onerror):
        error = PermissionError('delete denied')
        onerror(os.unlink, str(target), (PermissionError, error, None))
    monkeypatch.setattr(cleanup.os, 'chmod', denied_chmod)
    monkeypatch.setattr(cleanup.shutil, 'rmtree', failing_rmtree)
    with pytest.raises(PermissionError, match='chmod denied'):
        cleanup._remove_tree(root)


def make_clean_git(root):
    root.mkdir()
    (root / 'file').write_text('BASE')
    for arguments in [
        ['init', '-q'], ['config', 'user.name', 'Test'],
        ['config', 'user.email', 'test@example.com'],
        ['add', 'file'], ['commit', '-qm', 'BASE'],
    ]:
        subprocess.run(['git', '-C', str(root), *arguments], check=True)


def test_git_temporary_repositories_and_discard_are_cleaned(tmp_path):
    main = tmp_path / 'main'
    make_clean_git(main)
    manager = WorkspaceManager(tmp_path / 'runtime')
    session = manager.create(main, mode='ISOLATED')
    assert session.provider == 'git-worktree'
    execution = Path(session.execution_root)
    assert not (execution.parent / (execution.name + '-git')).exists()
    assert not (execution.parent / (execution.name + '-metadata')).exists()
    readonly = execution / 'readonly'
    readonly.write_text('agent')
    readonly.chmod(stat.S_IREAD)
    manager.discard(session)
    assert not execution.exists()
    assert manager.load(session.session_id).status == 'DISCARDED'
    assert (main / 'file').read_text() == 'BASE'


def test_failed_git_creation_cleans_all_temporary_repositories(tmp_path, monkeypatch):
    main = tmp_path / 'main'
    make_clean_git(main)
    manager = WorkspaceManager(tmp_path / 'runtime')
    def fail_move(*args, **kwargs):
        raise RuntimeError('injected metadata move failure')
    monkeypatch.setattr(sessions.shutil, 'move', fail_move)
    with pytest.raises(RuntimeError, match='metadata move failure'):
        manager.create(main, mode='ISOLATED')
    assert not list(manager.root.glob('execution-*'))
    assert (main / 'file').read_text() == 'BASE'


def test_creation_cleanup_attempts_all_resources_and_reports_failure(tmp_path, monkeypatch):
    main = tmp_path / 'main'
    main.mkdir()
    manager = WorkspaceManager(tmp_path / 'runtime')
    creation_error = RuntimeError('create failed')
    attempts = []
    def fail_create(self, main, destination, snapshot):
        destination.mkdir()
        raise creation_error
    def fail_cleanup(resource):
        attempts.append(resource)
        if len(attempts) == 1:
            raise PermissionError('cleanup failed')
    monkeypatch.setattr(sessions.SnapshotWorkspaceProvider, 'create', fail_create)
    monkeypatch.setattr(sessions, '_remove_tree', fail_cleanup)
    with pytest.raises(PermissionError, match='cleanup failed') as error:
        manager.create(main, mode='ISOLATED')
    assert error.value.__cause__ is creation_error
    assert len(attempts) == 3


@pytest.mark.parametrize('persistent', [False, True])
def test_traversal_permission_retry_is_complete_and_bounded(tmp_path, monkeypatch, persistent):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'file').write_text('keep until deleted')
    original_rmtree = cleanup.shutil.rmtree
    calls = []
    def failing_walk(path, *, onerror):
        calls.append(Path(path))
        if len(calls) == 1 or persistent:
            error = PermissionError('scandir denied')
            onerror(os.scandir, str(path), (PermissionError, error, None))
        else:
            original_rmtree(path, onerror=onerror)
    monkeypatch.setattr(cleanup.shutil, 'rmtree', failing_walk)
    if persistent:
        with pytest.raises(PermissionError, match='scandir denied'):
            cleanup._remove_tree(root)
        assert (root / 'file').exists()
    else:
        cleanup._remove_tree(root)
        assert not root.exists()
    assert calls == [root, root]
