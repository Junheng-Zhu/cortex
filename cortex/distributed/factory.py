"""Process-local construction of existing tools, backend and durable recovery."""
from pathlib import Path
from cortex.tools.registry import ToolRegistry
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.builtin.notes import ListNotesTool, ReadNoteTool, DeleteNoteTool, SlowTool
from cortex.tools.builtin.shell import ShellTool, discover_bash
from cortex.execution.local import LocalBackend
from cortex.execution.docker import DockerBackend
from cortex.execution.config import DockerBackendConfig
from cortex.runtime.workspace_session import WorkspaceManager, WorkspaceMode
from cortex.runtime.workspace import ShadowGitSnapshotStore, WorkspaceWriteLock
from cortex.runtime.checkpoint import SQLiteCheckpointStore
from cortex.runtime.execution_checkpoint import SQLiteExecutionCheckpointStore, MutationLedger, WorkspaceRecoveryRuntime


def make_executor(config):
    workspace = Path(config['workspace']).resolve(strict=True)
    storage = Path(config['storage']).resolve()
    if storage.is_relative_to(workspace):
        raise ValueError('Worker storage must be outside its execution workspace')
    isolated = config.get('isolated', False)
    manager = session = None
    if config.get('workspace_session_id'):
        if not config.get('workspace_manager_root'):
            raise ValueError('workspace_session_id requires workspace_manager_root')
        manager = WorkspaceManager(config['workspace_manager_root'])
        session = manager.load(config['workspace_session_id'])
        manager.validate(session)
        if Path(session.execution_root) != workspace:
            raise ValueError('Worker must bind to session execution_root')
        isolated = session.mode is WorkspaceMode.ISOLATED
        if isolated and storage.is_relative_to(Path(session.main_root)):
            raise ValueError('Isolated storage must be outside main_root')
    elif isolated:
        raise ValueError('Isolated worker requires a durable WorkspaceSession')
    storage.mkdir(parents=True, exist_ok=True)
    with WorkspaceWriteLock.for_workspace(workspace):
        snapshots = ShadowGitSnapshotStore(workspace, storage / 'snapshots')
        recovery = WorkspaceRecoveryRuntime(snapshots,
            SQLiteCheckpointStore(storage / 'agent.db'),
            SQLiteExecutionCheckpointStore(storage / 'execution.db'),
            MutationLedger(storage / 'execution.db'))
    backend = (DockerBackend(DockerBackendConfig(workspace=workspace, isolated=isolated))
        if config.get('backend', 'local') == 'docker'
        else LocalBackend(discover_bash, workspace=workspace, isolated=isolated))
    registry = ToolRegistry()
    for tool in (ListNotesTool(workspace), ReadNoteTool(workspace), DeleteNoteTool(workspace),
                 SlowTool(), ShellTool(backend, workspace)):
        registry.register(tool)
    executor = ToolExecutor(set(Permission), registry, recovery_runtime=recovery)
    if session:
        executor.workspace_guard = lambda: manager.validate(session)
        recovery.workspace_guard = executor.workspace_guard
        recovery.workspace_session, recovery.workspace_manager = session, manager
    executor.timeout = config.get('tool_timeout', 10)
    return executor


def close_recovery(executor):
    runtime = executor.recovery_runtime
    if runtime:
        runtime.snapshots.close()
        runtime.ledger.connection.close()
