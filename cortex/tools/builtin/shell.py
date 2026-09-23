"""A workspace-confined Bash runtime with one cross-platform contract."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from ..base import (
    ShellExecutionError,
    ShellUnavailableError,
    Tool,
    ToolSandboxError,
    ToolTimeoutError,
)
from ..permission import Permission


class ShellInput(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    cwd: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=30)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
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


def discover_bash() -> Path:
    """Find the Bash implementation used by the stable shell contract."""
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        candidates = []
        for root in filter(None, roots):
            base = Path(root)
            candidates.extend(
                (
                    base / "Git" / "bin" / "bash.exe",
                    base / "Programs" / "Git" / "bin" / "bash.exe",
                )
            )
        candidates.extend(
            Path(path)
            for path in filter(
                None, [shutil.which("bash.exe"), shutil.which("bash")]
            )
        )
    else:
        candidates = [Path("/bin/bash")]

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise ShellUnavailableError("Bash is unavailable on this system")


def _validate_command(command: str, cwd: Path) -> None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise ShellExecutionError(f"Invalid Bash command: {exc}") from exc
    if not tokens:
        raise ShellExecutionError("Bash command must not be empty")
    blocked = {Path(token).name.casefold() for token in tokens} & BLOCKED_PROGRAMS
    if blocked:
        raise ToolSandboxError(
            f"program is blocked by the shell safety policy: {sorted(blocked)[0]}",
            "shell",
            cwd,
        )


class ShellTool(Tool):
    name = "shell"
    description = (
        "Execute Bash commands inside the Cortex workspace. "
        "The command language is Bash/POSIX even when Cortex runs on Windows. "
        "Common commands: pwd, ls, cat, grep, find, git, python, pytest."
    )
    input_model = ShellInput
    permission = Permission.EXECUTE
    timeout = DEFAULT_TIMEOUT_SECONDS
    max_retries = 0
    retryable = False

    def execute(self, input: ShellInput) -> dict[str, int | str | bool]:
        cwd = (PROJECT_ROOT / (input.cwd or ".")).resolve()
        if not cwd.is_relative_to(PROJECT_ROOT) or not cwd.is_dir():
            raise ToolSandboxError(
                "cwd must be an existing directory inside the Cortex workspace",
                "shell",
                cwd,
            )
        _validate_command(input.command, cwd)
        bash = discover_bash()

        try:
            completed = subprocess.run(
                [str(bash), "-lc", input.command],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=input.timeout or DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolTimeoutError(
                f"Bash command timed out after {input.timeout or DEFAULT_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise ShellUnavailableError("Bash is unavailable on this system") from exc
        except OSError as exc:
            raise ShellExecutionError(f"Unable to start Bash: {exc}") from exc

        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:MAX_OUTPUT_CHARS],
            "stderr": completed.stderr[:MAX_OUTPUT_CHARS],
            "truncated": len(completed.stdout) > MAX_OUTPUT_CHARS
            or len(completed.stderr) > MAX_OUTPUT_CHARS,
        }
