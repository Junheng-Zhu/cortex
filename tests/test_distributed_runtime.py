"""Offline model and wiring checks (no fake Redis correctness claims)."""
import asyncio
from pathlib import Path
import pytest
from cortex.distributed.models import ExecutionTask, ExecutionState, InfrastructureUnavailable
from cortex.distributed.client import DistributedExecutionClient
from cortex.distributed.factory import make_executor, close_recovery
from cortex.tools.base import SideEffectPolicy


def test_state_machine_and_task_validation():
    task = ExecutionTask('call', 'tool', {}, 'stable')
    with pytest.raises(ValueError, match='illegal'): task.transition('SUCCEEDED')
    for state in ('QUEUED', 'CLAIMED', 'RUNNING', 'LOST', 'RECOVERING', 'QUEUED', 'CLAIMED', 'RUNNING', 'SUCCEEDED'):
        task.transition(state)
    with pytest.raises(ValueError, match='illegal'): task.transition('RUNNING')
    for state in ('FAILED', 'CANCELLED'):
        assert ExecutionTask('call', 'tool', {}, 'stable', state=state).terminal
    with pytest.raises(ValueError): ExecutionTask('call', 'tool', {}, 'id', max_attempts=0)
    with pytest.raises(ValueError): ExecutionTask('call', 'tool', {'a': float('nan')}, 'id')


def test_fingerprint_rejects_different_execution_or_workspace():
    task = ExecutionTask('a', 'tool', {}, 'key')
    duplicate = ExecutionTask('b', 'tool', {}, 'key')
    assert task.fingerprint() == duplicate.fingerprint()
    duplicate.side_effect_policy = SideEffectPolicy.IRREVERSIBLE
    assert task.fingerprint() != duplicate.fingerprint()
    duplicate.side_effect_policy = SideEffectPolicy.NONE
    duplicate.workspace_root = '/another'
    assert task.fingerprint() != duplicate.fingerprint()


def test_submission_failure_does_not_report_queued():
    task = ExecutionTask('a', 'tool', {}, 'key')
    class Store:
        def create(self, value): return value
        def queued(self, _): raise AssertionError('must not mark queued')
    class Queue:
        def publish(self, _): raise InfrastructureUnavailable('offline')
    with pytest.raises(InfrastructureUnavailable): DistributedExecutionClient(Queue(), Store()).submit(task)
    assert task.state is ExecutionState.CREATED


def test_factory_binds_existing_backend_and_recovery(tmp_path):
    root = tmp_path / 'workspace'; root.mkdir()
    executor = make_executor({'workspace': root, 'storage': tmp_path / 'state'})
    try:
        shell = executor.registry.get('shell')
        assert shell.workspace == root
        assert executor.recovery_runtime.snapshots.workspace == root
        assert executor.registry.get('read_note').directory == root / 'notes'
    finally:
        executor.close(); close_recovery(executor)
    with pytest.raises(ValueError, match='outside'):
        make_executor({'workspace': root, 'storage': root / 'state'})


def test_factory_reuses_isolated_session_guard(tmp_path):
    from cortex.runtime.workspace_session import WorkspaceManager
    main = tmp_path / 'main'; main.mkdir(); (main / 'file').write_text('base')
    manager = WorkspaceManager(tmp_path / 'manager')
    session = manager.create(main, mode='ISOLATED')
    executor = make_executor({'workspace': Path(session.execution_root), 'storage': tmp_path / 'state',
        'workspace_session_id': session.session_id, 'workspace_manager_root': manager.root})
    try:
        assert executor.registry.get('shell').backend.isolated
        executor.workspace_guard()
        session.status = 'INVALID'; manager.save(session)
        with pytest.raises(RuntimeError, match='inactive'):
            executor.workspace_guard()
    finally:
        executor.close(); close_recovery(executor)
        manager.discard(session)


def test_isolated_factory_requires_session_and_rejects_main_binding(tmp_path):
    from cortex.runtime.workspace_session import WorkspaceManager
    main = tmp_path / 'main'; main.mkdir()
    with pytest.raises(ValueError, match='durable WorkspaceSession'):
        make_executor({'workspace': main, 'storage': tmp_path / 's', 'isolated': True})
    manager = WorkspaceManager(tmp_path / 'manager'); session = manager.create(main, mode='ISOLATED')
    try:
        with pytest.raises(ValueError, match='execution_root'):
            make_executor({'workspace': main, 'storage': tmp_path / 's',
                'workspace_session_id': session.session_id, 'workspace_manager_root': manager.root})
    finally: manager.discard(session)


def test_agent_dispatch_is_explicit_and_keeps_mutation_boundaries_local():
    from cortex.distributed.executor import DistributedToolExecutor
    from cortex.tools.registry import ToolRegistry
    from cortex.tools.permission import Permission
    from cortex.tools.builtin.notes import DeleteNoteTool
    registry = ToolRegistry(); registry.register(DeleteNoteTool())
    with pytest.raises(ValueError, match='requires NONE'):
        DistributedToolExecutor(set(Permission), registry, distributed_client=object(), distributed_tools={'delete_note'})


def test_core_runtime_has_no_redis_imports():
    import ast
    import cortex.distributed as package
    directory = Path(package.__file__).parent
    for name in ('models', 'interfaces', 'client', 'worker', 'recovery', 'executor'):
        module = ast.parse((directory / (name + '.py')).read_text())
        for node in ast.walk(module):
            if isinstance(node, ast.Import): assert all(alias.name != 'redis' for alias in node.names)
            if isinstance(node, ast.ImportFrom): assert not (node.module or '').startswith('redis')


def test_build_agent_selects_distributed_executor_without_changing_loop(tmp_path):
    from cortex.app.bootstrap import build_agent
    from cortex.distributed.executor import DistributedToolExecutor
    from cortex.runtime.session import SessionConfig
    root = tmp_path / 'main'; root.mkdir()
    agent = build_agent(object(), execution_workspace=root,
        workspace_snapshot_root=tmp_path / 'snapshots', skills_enabled=False,
        session_config=SessionConfig(persist_trace=False),
        distributed_execution_client=object(), distributed_tool_names={'slow_tool'})
    try:
        assert isinstance(agent.executor, DistributedToolExecutor)
        assert agent.executor.distributed_tools == {'slow_tool'}
    finally: agent.close()
