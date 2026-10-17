import asyncio
import ast
import json
import os
from pathlib import Path
import sys
import pytest
from pydantic import ValidationError
from cortex.mcp import MCPConfig, MCPManager, load_config
from cortex.mcp.adapter import tool_name
from cortex.mcp.schema import input_model, UnsupportedSchema
from cortex.tools.registry import ToolRegistry
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.observability.tracer import RunRecorder

SERVER = Path(__file__).parent / 'fixtures' / 'mcp_server.py'


def config(tmp_path, ids=('one',), required=True, command=None):
    return MCPConfig.model_validate_json(json.dumps({
        'enabled': True, 'allowed_servers': list(ids), 'servers': [
            {'server_id': sid, 'command': command or sys.executable, 'args': [str(SERVER)],
             'required': required, 'connect_timeout': 3.0, 'call_timeout': 1.0,
             'env': {'PID_FILE': str(tmp_path / (sid + '.pid')), 'MARKER_FILE': str(tmp_path / 'marker')},
             'tools': {name: {'permission': 'READ', 'side_effect_policy': 'none'}
                       for name in ('echo', 'add', 'slow', 'fail', 'crash', 'unsupported')}}
            for sid in ids]}))


def payload(cfg):
    data = json.loads(cfg.model_dump_json())
    for server, raw in zip(cfg.servers, data['servers']):
        raw['env'] = {key: value.get_secret_value() for key, value in server.env.items()}
    return data


async def runtime(cfg, permissions=None):
    registry = ToolRegistry()
    recorder = RunRecorder()
    manager = MCPManager(cfg, recorder)
    allowed = {Permission.READ} if permissions is None else permissions
    await manager.start(registry, allowed)
    executor = ToolExecutor(allowed, registry)
    executor.async_resources.append(manager)
    return manager, executor, recorder


def dead(pid):
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value != 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_discovery_execution_schema_and_cleanup(tmp_path):
    manager, executor, recorder = await runtime(config(tmp_path))
    pid = int((tmp_path / 'one.pid').read_text())
    try:
        assert manager.connections['one'].state == 'ready'
        assert len(manager.connections['one'].tools) == 7
        assert len(executor.list_tool_schemas()) == 5
        name = tool_name('one', 'add')
        assert manager.resolve(name) == ('one', 'add')
        result = await executor.aexecute(name, {'a': 2, 'b': 3})
        assert result.success and result.data['structuredContent'] == {'value': 5}
        assert result.attempts == 1
        for arguments in ({'a': '2', 'b': 3}, {'a': 2}, {'a': True, 'b': 2}, {'a': 1, 'b': 2, 'extra': 0}):
            invalid = await executor.aexecute(name, arguments)
            assert invalid.error_type == 'ToolValidationError' and invalid.attempts == 0
        assert int((tmp_path / 'one.pid').read_text()) == pid
        assert any(e.data.get('discovery_count') == 7 for e in recorder.events)
        assert any(e.data.get('status') == 'unsupported_schema' for e in recorder.events)
    finally:
        await executor.aclose()
    assert dead(pid)
    assert not list(executor.registry.list_tools())
    await executor.aclose()


async def test_errors_namespace_and_isolation(tmp_path):
    manager, executor, _ = await runtime(config(tmp_path, ('one', 'two')))
    try:
        assert tool_name('one', 'echo') != tool_name('two', 'echo')
        failure = await executor.aexecute(tool_name('one', 'fail'), {})
        assert not failure.success and failure.error_type == 'MCPToolError'
        assert failure.data['isError'] and failure.data['content']
        crash = await executor.aexecute(tool_name('one', 'crash'), {})
        assert not crash.success and crash.error_type == 'MCPTransportError'
        result = await executor.aexecute(tool_name('two', 'echo'), {'text': 'alive'})
        assert result.success and result.data['structuredContent']['value'] == 'alive'
    finally:
        await executor.aclose()


async def test_timeout_and_cancel_cleanup(tmp_path):
    for cancel in (False, True):
        manager, executor, recorder = await runtime(config(tmp_path))
        executor.timeout = .05
        pid = int((tmp_path / 'one.pid').read_text())
        try:
            if cancel:
                task = asyncio.create_task(executor.aexecute(tool_name('one', 'slow'), {}))
                await asyncio.sleep(.02)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await executor.aexecute(tool_name('one', 'slow'), {})
                assert not result.success and result.error_type == 'ToolTimeoutError'
                assert result.attempts == 1
            assert dead(pid)
            assert any(e.data.get('status') == ('cancelled' if cancel else 'timeout') for e in recorder.events)
        finally:
            await executor.aclose()


async def test_permissions_and_default_deny(tmp_path):
    manager, executor, recorder = await runtime(config(tmp_path), set())
    try:
        assert not executor.list_tool_schemas()
        assert not manager.adapters
        assert any(e.data.get('status') == 'permission_denied' for e in recorder.events)
    finally:
        await executor.aclose()
    manager, executor, _ = await runtime(config(tmp_path))
    try:
        assert tool_name('one', 'write_marker') not in manager.adapters
        executor.allowed_permissions.clear()
        assert not executor.list_tool_schemas()
        with pytest.raises(PermissionError):
            executor.to_schema(tool_name('one', 'echo'))
        denied = await executor.aexecute(tool_name('one', 'echo'), {'text': 'no'})
        assert denied.error_type == 'ToolPermissionError'
        assert not (tmp_path / 'marker').exists()
    finally:
        await executor.aclose()


async def test_start_failure_optional_required_and_cleanup(tmp_path):
    cfg = config(tmp_path, required=False, command='nonexistent-cortex-mcp-command')
    manager, executor, _ = await runtime(cfg)
    assert not manager.adapters
    await executor.aclose()
    cfg = config(tmp_path, ('one', 'two'))
    raw = payload(cfg)
    raw['servers'][1]['command'] = 'nonexistent-cortex-mcp-command'
    manager = MCPManager(MCPConfig.model_validate_json(json.dumps(raw)))
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match='initialization failed'):
        await manager.start(registry, {Permission.READ})
    assert not list(registry.list_tools())
    assert dead(int((tmp_path / 'one.pid').read_text()))


def test_schema_subset_and_no_coercion():
    schema = {'type': 'object', '$defs': {'N': {'type': 'integer', 'minimum': 1}},
              'properties': {'n': {'$ref': '#/$defs/N'}, 'tags': {'type': 'array', 'items': {'enum': ['a', 'b']}}},
              'required': ['n'], 'additionalProperties': False}
    model = input_model(schema)
    assert model(n=2).model_dump(exclude_unset=True) == {'n': 2}
    assert model.model_json_schema() == schema
    for args in ({}, {'n': 0}, {'n': '2'}, {'n': 1, 'tags': ['c']}, {'n': 1, 'extra': 1}):
        with pytest.raises(ValidationError):
            model(**args)
    assert input_model({'type': 'object', 'properties': {'$ref': {'type': 'string'}}})(**{'$ref': 'ok'}).model_dump() == {'$ref': 'ok'}
    for extra in ({'format': 'email'}, {'$ref': 'https://example.com/a'}, {'unevaluatedProperties': False}, {'$defs': {'X': {'$ref': '#/$defs/X'}}}):
        with pytest.raises(UnsupportedSchema):
            input_model({'type': 'object', **extra})


def test_invalid_configuration(tmp_path):
    path = tmp_path / 'invalid.json'
    for value in ({'enabled': True, 'unknown': 'secret-value'},
                  {'servers': [{'server_id': 'bad id', 'command': 'secret-value'}]},
                  {'servers': [{'server_id': 's', 'command': 'secret-value', 'tools': {'x': {'permission': 'WRITE', 'side_effect_policy': 'workspace_reversible'}}}]},
                  {'servers': [{'server_id': 's', 'command': 'secret-value', 'tools': {'x': {'permission': 'WRITE', 'side_effect_policy': 'irreversible'}}}]}):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError) as exc:
            load_config(path)
        assert 'secret-value' not in str(exc.value)
    path.write_text('{"enabled": false, "enabled": true}')
    with pytest.raises(ValueError):
        load_config(path)
    cfg = config(tmp_path)
    raw = payload(cfg)
    raw['servers'] *= 2
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_config(path)


async def test_duplicate_registration_and_safe_unregister(tmp_path):
    manager, executor, _ = await runtime(config(tmp_path))
    try:
        adapter = next(iter(manager.adapters.values()))
        with pytest.raises(ValueError, match='Duplicate'):
            executor.registry.register(adapter)
        assert not executor.registry.unregister(adapter.name, expected=object())
        assert executor.registry.get(adapter.name) is adapter
    finally:
        await executor.aclose()


async def test_external_write_no_retry(tmp_path):
    raw = payload(config(tmp_path))
    raw['servers'][0]['tools']['write_marker'] = {'permission': 'WRITE', 'side_effect_policy': 'irreversible', 'authorize_external_effects': True}
    cfg = MCPConfig.model_validate_json(json.dumps(raw))
    manager, executor, _ = await runtime(cfg, {Permission.READ, Permission.WRITE})
    executor.timeout = .1
    try:
        name = tool_name('one', 'write_marker')
        adapter = manager.adapters[name]
        assert not adapter.retryable
        assert adapter.side_effect_policy.value == 'irreversible'
        result = await executor.aexecute(name, {})
        assert result.error_type == 'ToolTimeoutError' and result.attempts == 1
        assert (tmp_path / 'marker').read_text() == 'called\n'
        assert result.mutation_boundary is None
    finally:
        await executor.aclose()


async def test_agent_real_stdio(tmp_path):
    from cortex.app.bootstrap import async_build_agent
    from cortex.llm.protocol import LLMResponse, ToolCall
    class Client:
        def __init__(self):
            self.count = 0
        def respond(self, **kwargs):
            self.count += 1
            if self.count == 1:
                return LLMResponse(tool_calls=[ToolCall('call-1', tool_name('one', 'add'), {'a': 4, 'b': 5})])
            outputs = [item for item in kwargs['input'] if item.get('type') == 'function_call_output']
            assert ast.literal_eval(outputs[-1]['output'])['structuredContent'] == {'value': 9}
            return LLMResponse(content='done')
    agent = await async_build_agent(Client(), mcp_config=config(tmp_path), execution_workspace=tmp_path,
                                    skills_enabled=False, workspace_recovery_enabled=False)
    try:
        assert await agent.arun('add') == 'done'
        with pytest.raises(RuntimeError, match='aclose'):
            agent.close()
    finally:
        await agent.aclose()
    assert dead(int((tmp_path / 'one.pid').read_text()))


async def test_initialize_timeout_and_cancel(tmp_path):
    for cancel in (False, True):
        raw = payload(config(tmp_path))
        raw['servers'][0]['env']['START_DELAY'] = '1'
        raw['servers'][0]['connect_timeout'] = 1.0
        cfg = MCPConfig.model_validate_json(json.dumps(raw))
        manager = MCPManager(cfg)
        task = asyncio.create_task(manager.start(ToolRegistry(), {Permission.READ}))
        if cancel:
            # Wait for the child PID, then cancel while initialize is pending.
            for _ in range(200):
                if (tmp_path / 'one.pid').exists():
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match='initialization failed'):
                await task
        assert dead(int((tmp_path / 'one.pid').read_text()))
        (tmp_path / 'one.pid').unlink()


async def test_shutdown_with_inflight_call(tmp_path):
    manager, executor, _ = await runtime(config(tmp_path))
    task = asyncio.create_task(executor.aexecute(tool_name('one', 'slow'), {}))
    await asyncio.sleep(.03)
    await executor.aclose()
    result = await task
    assert not result.success
    assert dead(int((tmp_path / 'one.pid').read_text()))


async def test_secrets_not_in_errors_results_or_sdk_logs(tmp_path, caplog):
    raw = payload(config(tmp_path))
    raw['servers'][0]['env']['TOKEN'] = 'test-private-token-123'
    cfg = MCPConfig.model_validate_json(json.dumps(raw))
    manager, executor, recorder = await runtime(cfg)
    try:
        result = await executor.aexecute(tool_name('one', 'echo'), {'text': 'test-private-token-123'})
        assert result.data['structuredContent']['value'] == '<redacted>'
        invalid = await executor.aexecute(tool_name('one', 'add'), {'a': 'test-private-token-123', 'b': 2})
        assert 'test-private-token-123' not in invalid.error_message
        assert 'test-private-token-123' not in repr(recorder.events)
    finally:
        await executor.aclose()
    raw['servers'][0]['env']['BAD_WIRE'] = 'test-private-token-123'
    manager = MCPManager(MCPConfig.model_validate_json(json.dumps(raw)))
    try:
        await manager.start(ToolRegistry(), {Permission.READ})
    except RuntimeError as exc:
        assert 'test-private-token-123' not in str(exc)
    finally:
        await manager.aclose()
    assert 'test-private-token-123' not in caplog.text


async def test_disabled_and_server_allowlist_never_launch(tmp_path):
    for enabled, allowed in ((False, ['one']), (True, [])):
        raw = payload(config(tmp_path))
        raw['enabled'] = enabled
        raw['allowed_servers'] = allowed
        manager, executor, _ = await runtime(MCPConfig.model_validate_json(json.dumps(raw)))
        assert not manager.connections and not manager.adapters
        assert not (tmp_path / 'one.pid').exists()
        await executor.aclose()
