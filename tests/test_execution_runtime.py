from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.execution import (
    DockerBackend, DockerBackendConfig, ExecutionBackend, ExecutionRequest,
    ExecutionResult, LocalBackend,
)
from cortex.tools.builtin.shell import ShellInput, ShellTool


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


def test_local_backend_captures_and_truncates(tmp_path: Path):
    result = LocalBackend(lambda: Path("/bin/bash"), max_output_chars=3).execute(
        ExecutionRequest("printf abcdef; printf error >&2", tmp_path, 10)
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
    result = backend.execute(ExecutionRequest("pwd", tmp_path, 1))
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
def test_docker_image_integration_if_requested(tmp_path: Path):
    """Opt-in smoke test: CORTEX_RUN_DOCKER_TESTS=1 pytest -m docker."""
    import os
    if os.environ.get("CORTEX_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set CORTEX_RUN_DOCKER_TESTS=1 after building the sandbox image")
    with DockerBackend(DockerBackendConfig(workspace=tmp_path)) as backend:
        result = backend.execute(ExecutionRequest("python --version && git --version", tmp_path, 10))
    assert result.exit_code == 0
