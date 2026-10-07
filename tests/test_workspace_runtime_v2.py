"""V2 acceptance tests using real filesystem, Git, SQLite and child processes."""
import asyncio
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from cortex.app.bootstrap import build_agent
from cortex.runtime.checkpoint import SQLiteCheckpointStore
from cortex.runtime.execution_checkpoint import (
    MutationLedger, SQLiteExecutionCheckpointStore, WorkspaceRecoveryRuntime,
)
from cortex.runtime.state import AgentState
from cortex.runtime.workspace import ShadowGitSnapshotStore, WorkspaceWriteLock
from cortex.runtime.workspace_session import WorkspaceManager, WorkspaceMode
from cortex.tools.builtin.notes import ReadNoteInput


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def init_git(root):
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.com')
    git(root, 'add', '--all')
    git(root, 'commit', '-qm', 'base')


def setup_session(tmp_path, kind='plain'):
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'modified').write_text('base')
    (root / 'deleted').write_text('remove')
    if kind != 'plain':
        init_git(root)
    if kind == 'dirty':
        (root / 'modified').write_text('staged')
        git(root, 'add', 'modified')
        (root / 'modified').write_text('dirty working copy')
        (root / 'untracked').write_text('untracked BASE')
    manager = WorkspaceManager(tmp_path / 'runtime')
    session = manager.create(root, mode=WorkspaceMode.ISOLATED, owner_id='owner-a')
    runtime = WorkspaceRecoveryRuntime(manager.snapshots(session.execution_root),
        SQLiteCheckpointStore(tmp_path / 'agents.db'),
        SQLiteExecutionCheckpointStore(tmp_path / 'executions.db'))
    state = AgentState(session_id=session.session_id, run_id=session.run_id)
    return root, manager, session, runtime, state


def changes(root):
    (root / 'modified').write_text('agent')
    (root / 'deleted').unlink()
    (root / 'created').write_text('new')


@pytest.mark.parametrize('kind', ['plain', 'clean', 'dirty'])
def test_isolated_base_final_diff_apply_and_git_preservation(tmp_path, kind):
    root, manager, session, runtime, state = setup_session(tmp_path, kind)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    metadata = (git(root, 'rev-parse', 'HEAD'), git(root, 'symbolic-ref', 'HEAD'),
                (root / '.git/index').read_bytes()) if kind != 'plain' else None
    execution = Path(session.execution_root)
    assert {p.name: p.read_bytes() for p in execution.iterdir() if p.is_file()} == before
    if kind != 'plain':
        assert (execution / '.git').is_dir()
        assert Path(git(execution, 'rev-parse', '--absolute-git-dir').decode().strip()).is_relative_to(execution)
    runtime.mutate(state, lambda: changes(execution), action_ids=('edit',))
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
    assert {c.path: c.operation.value for c in manager.final_diff(session)} == {
        'created': 'CREATED', 'modified': 'MODIFIED', 'deleted': 'DELETED'}
    (root / 'unrelated-user').write_text('keep')
    result = manager.apply(session, runtime=runtime, state=state)
    assert result.success and result.checkpoint.reason == 'final_apply'
    assert (root / 'modified').read_text() == 'agent'
    assert (root / 'created').read_text() == 'new'
    assert not (root / 'deleted').exists()
    assert (root / 'unrelated-user').read_text() == 'keep'
    if metadata:
        assert (git(root, 'rev-parse', 'HEAD'), git(root, 'symbolic-ref', 'HEAD'),
                (root / '.git/index').read_bytes()) == metadata
    assert manager.load(session.session_id).status == 'APPLIED'
    assert not manager.pending_applies()


def test_apply_conflict_blocks_every_path_and_discard_preserves_main(tmp_path):
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    (root / 'modified').write_text('user')
    result = manager.apply(session, runtime=runtime, state=state)
    assert result.conflicts == ('modified',)
    assert (root / 'modified').read_text() == 'user'
    assert (root / 'deleted').exists() and not (root / 'created').exists()
    manager.discard(session)
    assert not Path(session.execution_root).exists()
    assert (root / 'modified').read_text() == 'user'
    assert manager.load(session.session_id).status == 'DISCARDED'
    assert runtime.snapshots.load(runtime.active_head(state.session_id).workspace_snapshot_id)


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_apply_failure_and_cancellation_restore_all_paths(tmp_path, failure):
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    def fail(index, path):
        if index == 1:
            raise failure('injected interruption')
    with pytest.raises(failure):
        manager.apply(session, runtime=runtime, state=state, fault=fail)
    assert (root / 'modified').read_text() == 'base'
    assert (root / 'deleted').read_text() == 'remove'
    assert not (root / 'created').exists()
    assert not manager.pending_applies()
    assert manager.load(session.session_id).status == 'ACTIVE'
    assert runtime.active_head(state.session_id).reason != 'final_apply'


def crash_apply(runtime_root, agents, executions, session_id):
    manager = WorkspaceManager(runtime_root)
    session = manager.load(session_id)
    runtime = WorkspaceRecoveryRuntime(manager.snapshots(session.execution_root),
        SQLiteCheckpointStore(agents), SQLiteExecutionCheckpointStore(executions))
    state = AgentState(session_id=session.session_id, run_id=session.run_id)
    manager.apply(session, runtime=runtime, state=state, fault=lambda *_: os._exit(71))


@pytest.mark.parametrize('user_conflict', [False, True])
def test_process_crash_journal_recovery(tmp_path, user_conflict):
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    process = multiprocessing.get_context('spawn').Process(target=crash_apply, args=(
        str(manager.root), str(tmp_path / 'agents.db'), str(tmp_path / 'executions.db'), session.session_id))
    process.start()
    process.join(15)
    assert process.exitcode == 71
    restarted = WorkspaceManager(manager.root)
    pending = restarted.pending_applies()
    assert len(pending) == 1 and pending[0][2] == 'WRITING'
    (root / 'other-user').write_text('keep')
    if user_conflict:
        (root / 'created').write_text('user after crash')
        with pytest.raises(RuntimeError, match='user changes'):
            restarted.recover_apply(pending[0][0])
        assert (root / 'created').read_text() == 'user after crash'
    else:
        restarted.recover_apply(pending[0][0])
        assert not (root / 'created').exists()
        assert (root / 'modified').read_text() == 'base'
        assert not restarted.pending_applies()
    assert (root / 'other-user').read_text() == 'keep'


def test_selective_action_preserves_unrelated_state_and_durable_history(tmp_path):
    root, manager, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    runtime.mutate(state, lambda: changes(execution), action_ids=('edit',))
    runtime.mutate(state, lambda: (execution / 'other').write_text('other'), action_ids=('other',))
    head = runtime.active_head(state.session_id)
    preview = runtime.rollback_action('edit', preview=True)
    assert preview.success and preview.checkpoint is None
    assert (execution / 'modified').read_text() == 'agent'
    result = runtime.rollback_action('edit', state=state)
    assert result.success
    assert (execution / 'modified').read_text() == 'base'
    assert (execution / 'deleted').read_text() == 'remove'
    assert not (execution / 'created').exists()
    assert (execution / 'other').read_text() == 'other'
    assert result.checkpoint.reason == 'selective_rollback'
    assert state.pending_input[-1]['content'].startswith('Workspace rollback completed')
    assert state.observations[-1].success
    assert len(runtime.ledger.records) == 7
    assert runtime.execution_checkpoints.load(head.execution_checkpoint_id) == head
    restarted = WorkspaceRecoveryRuntime(manager.snapshots(session.execution_root),
        SQLiteCheckpointStore(tmp_path / 'agents.db'), SQLiteExecutionCheckpointStore(tmp_path / 'executions.db'))
    assert restarted.active_head(state.session_id) == result.checkpoint
    assert len(restarted.ledger.query(action_id='edit')) == 3
    assert len(restarted.ledger.query(checkpoint_id=result.checkpoint.execution_checkpoint_id)) == 3
    # Undo the compensation by its new action id, without erasing the original history.
    compensation = restarted.ledger.query(checkpoint_id=result.checkpoint.execution_checkpoint_id)[0].action_id
    undone = restarted.rollback_action(compensation)
    assert undone.success and (execution / 'modified').read_text() == 'agent'
    assert restarted.rollback_action(undone.checkpoint.action_ids[0]).success
    assert (execution / 'modified').read_text() == 'base'


@pytest.mark.parametrize('path,expected', [('created', None), ('modified', 'base'), ('deleted', 'remove')])
def test_file_rollback_is_selective(tmp_path, path, expected):
    _, _, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    runtime.mutate(state, lambda: changes(execution), action_ids=('edit',))
    checkpoint = runtime.active_head(state.session_id)
    result = runtime.rollback_file(path, checkpoint.execution_checkpoint_id)
    assert result.success and result.paths == (path,)
    assert (execution / path).read_text() == expected if expected else not (execution / path).exists()
    for other in {'created', 'modified', 'deleted'} - {path}:
        assert runtime.ledger.query(path=other)[-1].action_id == 'edit'


@pytest.mark.parametrize('later_action', [False, True])
def test_selective_conflicts_never_overwrite(tmp_path, later_action):
    _, _, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    runtime.mutate(state, lambda: changes(execution), action_ids=('edit',))
    if later_action:
        # Even identical final bytes are a later action's ownership.
        runtime.mutate(state, lambda: (execution / 'modified').write_text('later'), action_ids=('later',))
        runtime.mutate(state, lambda: (execution / 'modified').write_text('agent'), action_ids=('again',))
    else:
        (execution / 'modified').write_text('user')
    before = (execution / 'modified').read_text()
    result = runtime.rollback_action('edit')
    assert result.conflicts == ('modified',)
    assert (execution / 'modified').read_text() == before
    assert (execution / 'created').exists() and not (execution / 'deleted').exists()


@pytest.mark.parametrize('path', ['../x', '/tmp/x', 'C:/main/x', 'C:\\main\\x', 'notes/../../x', '.git/index', 'a/.cortex/state', 'a:b', 'a//b'])
def test_windows_traversal_and_protected_paths_fail_closed(tmp_path, path):
    from cortex.runtime.workspace_paths import safe_path
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


def test_symlink_special_files_case_collisions_and_generation_fail_closed(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    store = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    if os.name != 'nt':
        (root / 'escape').symlink_to(tmp_path / 'outside')
        with pytest.raises(ValueError, match='escaping symlink'):
            store.snapshot()
        (root / 'escape').unlink()
        os.mkfifo(root / 'fifo')
        with pytest.raises(ValueError, match='unsupported'):
            store.snapshot()
        (root / 'fifo').unlink()
    (root / 'A').mkdir()
    (root / 'a').mkdir(exist_ok=True)
    (root / 'A/file').write_text('one')
    (root / 'a/other').write_text('two')
    if os.name != 'nt':
        with pytest.raises(ValueError, match='case collision'):
            store.snapshot()
    shutil.rmtree(root)
    root.mkdir()
    with pytest.raises(RuntimeError, match='stale'):
        store.snapshot()


def require_bwrap():
    if os.name == 'nt' or not shutil.which('bwrap'):
        pytest.skip('isolated Local requires bubblewrap')
    probe = subprocess.run(['bwrap', '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64', '--unshare-all', '/usr/bin/true'], capture_output=True)
    if probe.returncode:
        pytest.skip('bubblewrap namespaces unavailable: ' + probe.stderr.decode())


@pytest.mark.parametrize('use_async', [False, True])
def test_build_agent_isolated_shell_and_notes_never_write_main(tmp_path, use_async):
    require_bwrap()
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'notes').mkdir()
    (root / 'notes/note.txt').write_text('BASE note')
    manager = WorkspaceManager(tmp_path / 'runtime')
    agent = build_agent(object(), execution_workspace=root, workspace_mode='ISOLATED',
                        workspace_manager=manager, skills_enabled=False)
    try:
        execute = lambda name, args: asyncio.run(agent.executor.aexecute(name, args)) if use_async else agent.executor.execute(name, args)
        result = execute('shell', {'command': 'printf isolated > created; printf changed > notes/note.txt'})
        assert result.success and result.data['exit_code'] == 0, result
        assert not (root / 'created').exists()
        assert (root / 'notes/note.txt').read_text() == 'BASE note'
        assert execute('read_note', {'filename': 'note.txt'}).data == 'changed'
        assert not execute('read_note', {'filename': '../created'}).success
        escape = execute('shell', {'command': f"printf forbidden > '{root}/escape'"})
        assert escape.success and escape.data['exit_code'] != 0
        assert not (root / 'escape').exists()
        assert execute('delete_note', {'filename': 'note.txt'}).success
        assert (root / 'notes/note.txt').exists()
        manager.discard(agent.workspace_session, executor=agent.executor)
        with pytest.raises(RuntimeError):
            execute('shell', {'command': 'echo stale'})
    finally:
        agent.close()


def lock_process(root, ready, release):
    with WorkspaceWriteLock.for_workspace(root):
        ready.set()
        release.wait(10)


def test_cross_process_ownership(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=lock_process, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        lock = WorkspaceWriteLock.for_workspace(tmp_path)
        assert lock._try() is None
        assert lock._try(read=True) is None
        with WorkspaceWriteLock.for_workspace(tmp_path / 'other'):
            pass
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
    with WorkspaceWriteLock.for_workspace(tmp_path):
        pass


@pytest.mark.asyncio
async def test_sync_async_read_write_ownership(tmp_path):
    lock = WorkspaceWriteLock.for_workspace(tmp_path)
    read = lock.read()
    await read.acquire_async()
    writer = asyncio.create_task(lock.acquire_async())
    await asyncio.sleep(0.03)
    assert not writer.done()
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    read.release()
    await lock.acquire_async()
    assert lock._try(read=True) is None
    lock.release()
    await lock.acquire_async()
    lock.release()


def test_distinct_owner_sessions_and_branch_heads(tmp_path):
    root, manager, first, runtime, state = setup_session(tmp_path)
    second = manager.create(root, mode='ISOLATED', owner_id='owner-b')
    assert first.execution_root != second.execution_root
    assert manager.load(second.session_id).owner_id == 'owner-b'
    runtime.checkpoint(state)
    second_runtime = WorkspaceRecoveryRuntime(manager.snapshots(second.execution_root),
        runtime.agent_checkpoints, runtime.execution_checkpoints)
    second_runtime.checkpoint(state)  # Same conversation, separate workspace branch.
    assert runtime.active_head(state.session_id).branch_id != second_runtime.active_head(state.session_id).branch_id
    with pytest.raises(ValueError, match='different workspace'):
        runtime.rollback(second_runtime.active_head(state.session_id).execution_checkpoint_id)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows filesystem / Git Bash integration requires Windows')
def test_native_windows_workspace_paths(tmp_path):
    root, manager, session, runtime, state = setup_session(tmp_path, 'dirty')
    (Path(session.execution_root) / '目录 with spaces.txt').write_text('Windows BASE')
    result = manager.apply(session, runtime=runtime, state=state)
    assert result.success
    assert (root / '目录 with spaces.txt').read_text() == 'Windows BASE'


@pytest.mark.docker
def test_real_docker_isolated_workspace(tmp_path):
    docker = pytest.importorskip('docker')
    from cortex.execution import DockerBackend, DockerBackendConfig, ExecutionRequest
    try:
        client = docker.from_env()
        client.ping()
        client.images.get('cortex-execution-runtime:v1')
    except Exception as exc:
        pytest.skip('Docker daemon/runtime image unavailable: ' + str(exc))
    root, _, session, _, _ = setup_session(tmp_path, 'clean')
    backend = DockerBackend(DockerBackendConfig(workspace=Path(session.execution_root), isolated=True), client=client)
    try:
        result = backend.execute(ExecutionRequest('printf docker > created; git rev-parse --git-common-dir', Path('.'), 10))
        assert result.exit_code == 0, result.stderr
        assert not (root / 'created').exists()
        assert (Path(session.execution_root) / 'created').read_text() == 'docker'
        assert result.stdout.strip() == '.git'
        denied = backend.execute(ExecutionRequest('git add created', Path('.'), 10))
        assert denied.exit_code != 0
    finally:
        backend.close()


def test_isolated_runtime_persistence_stays_outside_main_even_when_cwd_is_main(tmp_path, monkeypatch):
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'file').write_text('BASE')
    monkeypatch.chdir(root)
    manager = WorkspaceManager(tmp_path / 'runtime')
    agent = build_agent(object(), execution_workspace=root, workspace_mode='ISOLATED', workspace_manager=manager)
    try:
        assert sorted(p.name for p in root.iterdir()) == ['file']
        assert agent.executor.registry.get('read_note').directory == Path(agent.workspace_session.execution_root) / 'notes'
        assert not agent.recorder.db_path.is_relative_to(root)
        assert not agent.artifact_store.root.is_relative_to(root)
    finally:
        agent.close()
        manager.discard(agent.workspace_session)


def test_restart_build_agent_and_compensation_context(tmp_path):
    require_bwrap()
    from cortex.llm.protocol import LLMResponse, ToolCall
    class Client:
        def __init__(self):
            self.responses = [LLMResponse(tool_calls=[ToolCall('edit', 'shell', {'command': 'printf agent > modified'})]), LLMResponse(content='done')]
        def respond(self, **kwargs):
            return self.responses.pop(0)
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'modified').write_text('BASE')
    manager = WorkspaceManager(tmp_path / 'runtime')
    agent = build_agent(Client(), execution_workspace=root, workspace_mode='ISOLATED', workspace_manager=manager, skills_enabled=False)
    try:
        assert agent.run('edit') == 'done'
        assert (root / 'modified').read_text() == 'BASE'
        state = agent.last_state
        action_id = agent.workspace_recovery_runtime.ledger.records[-1].action_id
        session = manager.load(agent.workspace_session.session_id)
    finally:
        agent.close()
    restarted = build_agent(object(), workspace_manager=manager, workspace_session=session, skills_enabled=False)
    try:
        runtime = restarted.workspace_recovery_runtime
        assert runtime.active_head(state.session_id)
        result = runtime.undo_action(action_id)
        assert result.success
        assert result.state.pending_input[-1]['content'].startswith('Workspace rollback completed')
        assert (Path(session.execution_root) / 'modified').read_text() == 'BASE'
        assert runtime.redo_action(result.checkpoint.action_ids[0]).success
        assert (Path(session.execution_root) / 'modified').read_text() == 'agent'
    finally:
        restarted.close()


def test_excluded_metadata_mutation_fails_closed_and_can_discard(tmp_path):
    require_bwrap()
    root = tmp_path / 'main'
    root.mkdir()
    agent = build_agent(object(), execution_workspace=root, workspace_mode='ISOLATED',
                        workspace_manager=WorkspaceManager(tmp_path / 'runtime'), skills_enabled=False)
    try:
        result = agent.executor.execute('shell', {'command': 'mkdir nested; mkdir nested/.git; printf bad > nested/.git/state'})
        assert not result.success and result.error_type == 'ToolSandboxError'
        assert agent.workspace_manager.load(agent.workspace_session.session_id).status == 'INVALID'
        head = agent.workspace_recovery_runtime.execution_checkpoints.latest('tool-executor-direct')
        assert head.reason == 'unrecoverable_mutation'
        with pytest.raises(RuntimeError):
            agent.executor.execute('shell', {'command': 'echo next'})
        agent.workspace_manager.discard(agent.workspace_session, executor=agent.executor)
        assert not list(root.iterdir())
    finally:
        agent.close()


def test_symlink_and_executable_mode_selective_recovery(tmp_path):
    if os.name == 'nt':
        pytest.skip('POSIX symlink and executable bit test')
    _, _, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    (execution / 'link').symlink_to('modified')
    (execution / 'modified').chmod(0o755)
    def modify():
        (execution / 'link').unlink()
        (execution / 'link').symlink_to('deleted')
        (execution / 'modified').chmod(0o644)
    runtime.mutate(state, modify, action_ids=('mode-and-link',))
    assert runtime.rollback_action('mode-and-link').success
    assert os.readlink(execution / 'link') == 'modified'
    assert (execution / 'modified').stat().st_mode & 0o100


@pytest.mark.parametrize('path', ['CON', 'nul.txt', 'a/LPT1', '.git ', 'name.', 'name '])
def test_windows_alias_paths_fail_closed(tmp_path, path):
    from cortex.runtime.workspace_paths import safe_path
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


def test_ambient_git_selectors_cannot_redirect_shadow_or_main_index(tmp_path, monkeypatch):
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'file').write_text('BASE')
    init_git(root)
    index = (root / '.git/index').read_bytes()
    monkeypatch.setenv('GIT_DIR', str(root / '.git'))
    monkeypatch.setenv('GIT_INDEX_FILE', str(root / '.git/index'))
    monkeypatch.setenv('GIT_WORK_TREE', str(root))
    snapshots = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    base = snapshots.snapshot()
    assert snapshots.load(base.snapshot_id) == base
    manager = WorkspaceManager(tmp_path / 'runtime')
    session = manager.create(root, mode='ISOLATED')
    assert (root / '.git/index').read_bytes() == index
    assert (Path(session.execution_root) / 'file').read_text() == 'BASE'


def test_apply_journal_commit_failure_rolls_back_history_and_files(tmp_path):
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    original_head = runtime.active_head(state.session_id)
    with manager._db() as db:
        db.execute("CREATE TRIGGER reject_success BEFORE UPDATE ON apply_journal WHEN NEW.status='COMMITTED' BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END")
    with pytest.raises(Exception, match='injected commit failure'):
        manager.apply(session, runtime=runtime, state=state)
    assert runtime.active_head(state.session_id) == original_head
    assert (root / 'modified').read_text() == 'base'
    assert (root / 'deleted').exists() and not (root / 'created').exists()
    assert not manager.pending_applies()


def test_committed_apply_reconciles_session_metadata_failure(tmp_path, monkeypatch):
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    save = manager.save
    def fail(session):
        if session.status == 'APPLIED':
            raise OSError('injected status failure')
        save(session)
    monkeypatch.setattr(manager, 'save', fail)
    with pytest.raises(OSError, match='status failure'):
        manager.apply(session, runtime=runtime, state=state)
    restarted = WorkspaceManager(manager.root)
    loaded = restarted.load(session.session_id)
    assert loaded.status == 'APPLIED'
    assert loaded.active_checkpoint_id == runtime.active_head(state.session_id).execution_checkpoint_id
    assert not restarted.pending_applies()
    assert (root / 'modified').read_text() == 'agent'
    assert not (root / 'deleted').exists()


def test_ledger_and_checkpoint_head_commit_atomically(tmp_path):
    _, _, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    head = runtime.checkpoint(state)
    with runtime.ledger.connection:
        runtime.ledger.connection.execute("CREATE TRIGGER reject_record BEFORE INSERT ON mutations BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
    with pytest.raises(Exception, match='ledger unavailable'):
        runtime.mutate(state, lambda: (execution / 'modified').write_text('interrupted'), action_ids=('edit',))
    assert not runtime.ledger.records
    current = runtime.active_head(state.session_id)
    assert current.reason == 'before_mutation'
    assert current.parent_execution_checkpoint_id == head.execution_checkpoint_id
    with runtime.ledger.connection:
        runtime.ledger.connection.execute("DROP TRIGGER reject_record")
    assert runtime.rollback(current.execution_checkpoint_id)
    assert (execution / 'modified').read_text() == 'base'


def test_create_session_never_refreshes_user_git_index(tmp_path):
    root = tmp_path / 'main'
    root.mkdir()
    (root / 'file').write_text('BASE')
    init_git(root)
    index = (root / '.git/index').read_bytes()
    # Change stat data without changing content; ordinary git status may refresh index.
    (root / 'file').write_text('BASE')
    manager = WorkspaceManager(tmp_path / 'runtime')
    manager.create(root, mode='ISOLATED')
    assert (root / '.git/index').read_bytes() == index


@pytest.mark.asyncio
async def test_async_apply_cancellation_waits_for_restore(tmp_path):
    import threading
    root, manager, session, runtime, state = setup_session(tmp_path)
    runtime.mutate(state, lambda: changes(Path(session.execution_root)), action_ids=('edit',))
    started, release = threading.Event(), threading.Event()
    def pause(index, path):
        if index == 0:
            started.set()
            release.wait(10)
    task = asyncio.create_task(manager.aapply(session, runtime=runtime, state=state, fault=pause))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (root / 'modified').read_text() == 'base'
    assert (root / 'deleted').exists() and not (root / 'created').exists()
    assert not manager.pending_applies()
    assert runtime.active_head(state.session_id).reason != 'final_apply'


def test_file_shape_conflict_does_not_overwrite_user_directory(tmp_path):
    _, _, session, runtime, state = setup_session(tmp_path)
    execution = Path(session.execution_root)
    runtime.mutate(state, lambda: (execution / 'modified').unlink(), action_ids=('delete',))
    (execution / 'modified').mkdir()
    (execution / 'modified/user').write_text('user directory')
    with pytest.raises(ValueError):
        runtime.rollback_action('delete')
    assert (execution / 'modified/user').read_text() == 'user directory'


def test_direct_session_is_lazy_and_recovery_disabled_still_has_ownership(tmp_path):
    root = tmp_path / 'main'
    root.mkdir()
    agent = build_agent(object(), execution_workspace=root, workspace_recovery_enabled=False, skills_enabled=False)
    try:
        session = agent.workspace_session
        assert session.mode is WorkspaceMode.DIRECT
        assert session.main_root == session.execution_root == str(root)
        assert session.base_snapshot_id == ''
        assert agent.executor.workspace_lock is WorkspaceWriteLock.for_workspace(root)
        result = agent.executor.execute('shell', {'command': 'printf direct > file'})
        assert result.success and (root / 'file').read_text() == 'direct'
    finally:
        agent.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory handle generation check')
def test_generation_rejects_replacement_even_if_path_identity_matches(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    store = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    try:
        root.rmdir()
        root.mkdir()
        # Simulate an inode-only detector accepting the replacement. The pinned
        # original directory must still reject it, independent of allocation order.
        current = root.stat()
        store.generation = current.st_dev, current.st_ino
        with pytest.raises(RuntimeError, match='stale'):
            store.snapshot()
        with pytest.raises(RuntimeError, match='stale'):
            store.excluded_state()
    finally:
        store.close()


def test_directory_writes_preserve_snapshot_store_generation(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    store = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    try:
        (root / 'created').write_text('new')
        first = store.snapshot()
        (root / 'created').unlink()
        assert store.snapshot().files == ()
        store.restore(first)
        assert (root / 'created').read_text() == 'new'
    finally:
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory handle lifecycle')
def test_snapshot_store_releases_generation_handle(tmp_path):
    import gc
    root = tmp_path / 'workspace'
    root.mkdir()
    store = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    descriptor = store._directory_fd
    store.close()
    store.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with pytest.raises(RuntimeError, match='closed'):
        store.snapshot()
    del store
    gc.collect()
    store = ShadowGitSnapshotStore(root, tmp_path / 'shadow')
    descriptor = store._directory_fd
    del store
    gc.collect()
    with pytest.raises(OSError):
        os.fstat(descriptor)
