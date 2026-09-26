from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.execution import (
    DockerBackend, DockerBackendConfig, ExecutionBackend, ExecutionRequest,
    ExecutionResult, LocalBackend,
)
from cortex.tools.builtin.shell import ShellInput, ShellTool
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry
from cortex.app.bootstrap import build_agent


class StubBackend(ExecutionBackend):
    def __init__(self):
        self.request = None

    def execute(self, request):
        self.request = request
        return ExecutionResult(7, "out", "err", False, 1)


def test_shell_delegates_and_preserves_visible_contract():
    backend = StubBackend()
    result = ShellTool(backend).execute(ShellInput(command="printf ignored", timeout=2))
    assert result == {"exit_code": 7, "stdout": "out", "stderr": "err", "truncated": False}
    assert backend.request.command == "printf ignored"
    assert backend.request.timeout == 2
    assert backend.request.cwd == Path(".")


def test_executor_keeps_backend_supervised_tool_in_owner_process(tmp_path: Path):
    backend = StubBackend()
    registry = ToolRegistry()
    registry.register(ShellTool(backend, workspace=tmp_path))
    executor = ToolExecutor({Permission.EXECUTE}, registry)
    result = executor.execute("shell", {"command": "echo ok"})
    assert result.success is True
    assert backend.request is not None  # mutation is visible: no multiprocessing copy


def test_shell_constructor_stores_string_workspace_and_sends_relative_cwd(tmp_path: Path):
    child = tmp_path / "child"
    child.mkdir()
    backend = StubBackend()
    tool = ShellTool(backend, str(tmp_path))
    result = tool.execute(ShellInput(command="pwd", cwd="child"))
    assert result["exit_code"] == 7
    assert tool.workspace == tmp_path.resolve()
    assert backend.request.cwd == Path("child")


def test_build_agent_wires_backend_and_workspace_into_shell(tmp_path: Path):
    backend = StubBackend()
    agent = build_agent(
        object(),
        skills_enabled=False,
        execution_backend=backend,
        execution_workspace=tmp_path,
    )
    try:
        result = agent.executor.execute("shell", {"command": "pwd"})
        assert result.success is True
        shell = agent.executor.registry.get("shell")
        assert shell.backend is backend
        assert shell.workspace == tmp_path.resolve()
        assert backend.request.cwd == Path(".")
    finally:
        agent.close()


def test_local_backend_captures_and_truncates(tmp_path: Path):
    result = LocalBackend(
        lambda: Path("/bin/bash"), max_output_chars=3, workspace=tmp_path
    ).execute(
        ExecutionRequest("printf abcdef; printf error >&2", Path("."), 10)
    )
    assert (result.exit_code, result.stdout, result.stderr, result.truncated) == (0, "abc", "err", True)
    assert result.duration_ms >= 0


class FakeContainer:
    def __init__(self):
        self.removed = False

    def exec_run(self, *_args, **_kwargs):
        return SimpleNamespace(exit_code=0, output=(b"hello", b""))

    def remove(self, force=False):
        self.removed = force


class FakeContainers:
    def __init__(self, container):
        self.container = container
        self.options = None

    def run(self, *_args, **kwargs):
        self.options = kwargs
        return self.container


def test_docker_security_defaults_and_close(tmp_path: Path):
    container = FakeContainer()
    containers = FakeContainers(container)
    backend = DockerBackend(
        DockerBackendConfig(workspace=tmp_path),
        client=SimpleNamespace(containers=containers),
    )
    result = backend.execute(ExecutionRequest("pwd", Path("."), 1))
    assert result.stdout == "hello"
    assert containers.options["network_disabled"] is True
    assert containers.options["read_only"] is True
    assert containers.options["cap_drop"] == ["ALL"]
    assert containers.options["security_opt"] == ["no-new-privileges:true"]
    assert containers.options["user"] == "1000:1000"
    assert containers.options["environment"] == {
        "HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"
    }
    backend.close()
    assert container.removed


@pytest.mark.docker
def test_docker_tool_executor_e2e_if_requested(tmp_path: Path, monkeypatch):
    """Full ToolExecutor -> ShellTool -> DockerBackend lifecycle test."""
    import os
    if os.environ.get("CORTEX_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set CORTEX_RUN_DOCKER_TESTS=1 after building the sandbox image")
    import docker

    client = docker.from_env()
    backend = DockerBackend(DockerBackendConfig(workspace=tmp_path), client=client)
    registry = ToolRegistry()
    registry.register(ShellTool(backend, workspace=tmp_path))
    executor = ToolExecutor({Permission.EXECUTE}, registry)

    monkeypatch.setenv("CORTEX_HOST_SECRET", "must-not-leak")
    first = executor.execute("shell", {"command": "echo retained > state && python --version"})
    assert first.success
    first_id = backend._container.id
    second = executor.execute(
        "shell",
        {"command": "test $(cat state) = retained; test -z \"$CORTEX_HOST_SECRET\"; "
                    "touch writable; ! touch /rootfs-write; git --version; grep retained state; find . -name writable"},
    )
    assert second.success and second.data["exit_code"] == 0
    assert backend._container.id == first_id

    inspected = client.api.inspect_container(first_id)
    host = inspected["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Memory"] == 512 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 128
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]

    timed_out = executor.execute("shell", {"command": "sleep 5", "timeout": 0.1})
    assert not timed_out.success and timed_out.error_type == "ToolTimeoutError"
    assert backend._container is None
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(first_id)

    recovered = executor.execute("shell", {"command": "echo recovered"})
    assert recovered.success and backend._container.id != first_id
    recovered_id = backend._container.id
    executor.close()
    executor.close()
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(recovered_id)
