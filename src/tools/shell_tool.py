"""A deliberately small, constrained command execution tool."""

import shlex
import subprocess
from pathlib import Path

from .base import Tool
from .exceptions import ToolSandboxError
from .permission import Permission
from .schemas import ShellInput

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_OUTPUT_CHARS = 10_000
DEFAULT_TIMEOUT_SECONDS = 10
BLOCKED_PROGRAMS = {
    "chmod",
    "chown",
    "dd",
    "kill",
    "mkfs",
    "mount",
    "poweroff",
    "reboot",
    "rm",
    "shutdown",
    "sudo",
}
SHELL_OPERATORS = {"|", "||", "&&", ";", ">", ">>", "<", "`"}


class ShellTool(Tool):
    name = "shell"
    description = "在项目目录内执行单个安全命令，返回 exit_code、stdout 和 stderr。"
    input_model = ShellInput
    permission = Permission.EXECUTE
    timeout = DEFAULT_TIMEOUT_SECONDS
    max_retries = 0
    retryable = False

    def execute(self, input: ShellInput) -> dict[str, int | str | bool]:
        cwd = (PROJECT_ROOT / (input.cwd or ".")).resolve()
        if not cwd.is_relative_to(PROJECT_ROOT) or not cwd.is_dir():
            raise ToolSandboxError(
                "cwd must be an existing directory inside the project", "shell", cwd
            )

        argv = shlex.split(input.command)
        if not argv:
            raise ValueError("command must not be empty")
        program = Path(argv[0]).name.lower()
        if program in BLOCKED_PROGRAMS or any(
            token in SHELL_OPERATORS for token in argv
        ):
            raise ToolSandboxError(
                "command is not allowed by the shell safety policy", "shell", cwd
            )

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=input.timeout or DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
            return {
                "exit_code": completed.returncode,
                "stdout": completed.stdout[:MAX_OUTPUT_CHARS],
                "stderr": completed.stderr[:MAX_OUTPUT_CHARS],
                "truncated": len(completed.stdout) > MAX_OUTPUT_CHARS
                or len(completed.stderr) > MAX_OUTPUT_CHARS,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
            )
            stderr = (
                exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            )
            return {
                "exit_code": 124,
                "stdout": (stdout or "")[:MAX_OUTPUT_CHARS],
                "stderr": (stderr or "command timed out")[:MAX_OUTPUT_CHARS],
                "truncated": False,
            }
