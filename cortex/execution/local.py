"""Host Bash execution backend."""

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .backend import ExecutionBackend
from .exceptions import ExecutionError, ExecutionTimeoutError, ExecutionUnavailableError
from .models import ExecutionRequest, ExecutionResult


class LocalBackend(ExecutionBackend):
    def __init__(
        self,
        bash_resolver: Callable[[], Path],
        max_output_chars: int = 10_000,
        workspace: Path | None = None,
    ):
        self._bash_resolver = bash_resolver
        self._max_output_chars = max_output_chars
        self.workspace = (workspace or Path.cwd()).resolve()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        bash = self._bash_resolver()
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(bash), "-lc", request.command], cwd=self._host_cwd(request.cwd),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=request.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeoutError(
                f"Bash command timed out after {request.timeout:g} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise ExecutionUnavailableError("Bash is unavailable on this system") from exc
        except OSError as exc:
            raise ExecutionError(f"Unable to start Bash: {exc}") from exc
        stdout, stderr = completed.stdout, completed.stderr
        limit = self._max_output_chars
        return ExecutionResult(
            completed.returncode, stdout[:limit], stderr[:limit],
            len(stdout) > limit or len(stderr) > limit,
            round((time.monotonic() - started) * 1000),
        )

    def _host_cwd(self, cwd: Path) -> Path:
        mapped = (self.workspace / cwd).resolve()
        if not mapped.is_relative_to(self.workspace) or not mapped.is_dir():
            raise ExecutionError("Local cwd must be an existing workspace directory")
        return mapped

    async def aexecute(self, request: ExecutionRequest) -> ExecutionResult:
        bash = self._bash_resolver()
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                str(bash), "-lc", request.command,
                cwd=self._host_cwd(request.cwd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise ExecutionUnavailableError("Bash is unavailable on this system") from exc
        except OSError as exc:
            raise ExecutionError(f"Unable to start Bash: {exc}") from exc
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(), request.timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ExecutionTimeoutError(
                f"Bash command timed out after {request.timeout:g} seconds"
            ) from exc
        stdout = stdout_raw.decode("utf-8", "replace")
        stderr = stderr_raw.decode("utf-8", "replace")
        limit = self._max_output_chars
        return ExecutionResult(
            process.returncode, stdout[:limit], stderr[:limit],
            len(stdout) > limit or len(stderr) > limit,
            round((time.monotonic() - started) * 1000),
        )
