import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import cortex.tools.builtin.shell as shell_runtime
from cortex.tools.base import (
    ShellExecutionError,
    ShellUnavailableError,
    ToolSandboxError,
    ToolTimeoutError,
)
from cortex.tools.builtin.shell import ShellInput, ShellTool


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la",
        "pwd && ls -la",
        "python --version",
        "git status",
        "python -m pytest --version",
    ],
)
def test_core_bash_commands_succeed(command):
    result = ShellTool().execute(ShellInput(command=command))

    assert result["exit_code"] == 0
    assert set(result) == {"exit_code", "stdout", "stderr", "truncated"}


def test_nonzero_command_is_a_structured_result():
    result = ShellTool().execute(
        ShellInput(command="grep definitely-not-exist README.md")
    )

    assert result["exit_code"] != 0
    assert isinstance(result["stdout"], str)
    assert isinstance(result["stderr"], str)


def test_cwd_cannot_escape_workspace():
    with pytest.raises(ToolSandboxError):
        ShellTool().execute(ShellInput(command="pwd", cwd="../"))


def test_dangerous_program_is_blocked_inside_a_compound_command():
    with pytest.raises(ToolSandboxError):
        ShellTool().execute(ShellInput(command="pwd && sudo echo forbidden"))


def test_missing_bash_raises_stable_error(monkeypatch):
    def unavailable():
        raise ShellUnavailableError("Bash is unavailable on this system")

    monkeypatch.setattr(shell_runtime, "discover_bash", unavailable)

    with pytest.raises(ShellUnavailableError) as error:
        ShellTool().execute(ShellInput(command="pwd"))
    assert "WinError" not in str(error.value)


def test_process_start_failure_is_normalized(monkeypatch):
    def fail_to_start(*args, **kwargs):
        raise OSError("host process failure")

    monkeypatch.setattr(subprocess, "run", fail_to_start)

    with pytest.raises(ShellExecutionError, match="Unable to start Bash"):
        ShellTool().execute(ShellInput(command="pwd"))


def test_process_disappearing_is_reported_as_unavailable(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("should not leak")

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(ShellUnavailableError, match="Bash is unavailable"):
        ShellTool().execute(ShellInput(command="pwd"))


def test_timeout_uses_tool_timeout_error(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=0.1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ToolTimeoutError, match="timed out"):
        ShellTool().execute(ShellInput(command="sleep 1", timeout=0.1))


def test_windows_prefers_git_bash(monkeypatch, tmp_path: Path):
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.touch()
    monkeypatch.setattr(shell_runtime.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shell_runtime.os, "access", lambda path, mode: True)
    monkeypatch.setattr(shell_runtime.shutil, "which", lambda name: None)

    assert shell_runtime.discover_bash() == git_bash.resolve()


def test_shell_contract_has_only_the_stable_inputs():
    assert set(ShellInput.model_fields) == {"command", "cwd", "timeout"}
    assert "Bash/POSIX" in ShellTool.description
