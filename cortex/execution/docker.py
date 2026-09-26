"""A hardened, session-scoped Docker execution backend."""

import concurrent.futures
import asyncio
import docker
import threading
import time
from pathlib import Path
from typing import Any

from .backend import ExecutionBackend
from .config import DockerBackendConfig
from .exceptions import ExecutionError, ExecutionTimeoutError, ExecutionUnavailableError
from .models import ExecutionRequest, ExecutionResult


class DockerBackend(ExecutionBackend):
    """Run commands in one lazily-created, disposable sandbox container."""

    def __init__(self, config: DockerBackendConfig, client: Any | None = None):
        self.config = config
        self._client = client
        self._container = None
        self._lock = threading.RLock()

    def _docker_client(self):
        if self._client is None:
            try:
                self._client = docker.from_env()
            except Exception as exc:
                raise ExecutionUnavailableError(f"Docker is unavailable: {exc}") from exc
        return self._client

    def _get_container(self):
        with self._lock:
            if self._container is None:
                workspace = self.config.workspace.resolve()
                try:
                    self._container = self._docker_client().containers.run(
                        self.config.image, command=["sleep", "infinity"], detach=True,
                        working_dir="/workspace", user=self.config.user,
                        volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
                        network_disabled=self.config.network_disabled, read_only=True,
                        tmpfs=self.config.tmpfs, cap_drop=["ALL"],
                        security_opt=["no-new-privileges:true"],
                        mem_limit=self.config.memory_limit, nano_cpus=self.config.nano_cpus,
                        pids_limit=self.config.pids_limit,
                        environment={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"},
                        labels={"com.cortex.execution-runtime": "v2"},
                    )
                except Exception as exc:
                    raise ExecutionUnavailableError(
                        f"Unable to create Docker sandbox: {exc}"
                    ) from exc
            return self._container

    def _container_cwd(self, cwd: Path) -> str:
        if cwd.is_absolute() or ".." in cwd.parts:
            raise ExecutionError("Docker cwd must be workspace-relative")
        host_path = (self.config.workspace.resolve() / cwd).resolve()
        if not host_path.is_relative_to(self.config.workspace.resolve()) or not host_path.is_dir():
            raise ExecutionError("Docker cwd must be an existing workspace directory")
        return str(Path("/workspace") / cwd)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        container_cwd = self._container_cwd(request.cwd)
        container = self._get_container()
        return self._execute_in_container(request, container, container_cwd)

    def _execute_in_container(
        self, request: ExecutionRequest, container, container_cwd: str
    ) -> ExecutionResult:
        started = time.monotonic()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            container.exec_run, ["/bin/bash", "-lc", request.command],
            workdir=container_cwd, demux=True,
        )
        try:
            response = future.result(timeout=request.timeout)
        except concurrent.futures.TimeoutError as exc:
            self._discard_container(container)
            executor.shutdown(wait=False, cancel_futures=True)
            raise ExecutionTimeoutError(
                f"Docker command timed out after {request.timeout:g} seconds"
            ) from exc
        except Exception as exc:
            self._discard_container(container)
            executor.shutdown(wait=False, cancel_futures=True)
            raise ExecutionError(f"Docker execution failed: {exc}") from exc
        executor.shutdown(wait=True)
        output = response.output or (b"", b"")
        stdout_raw, stderr_raw = output if isinstance(output, tuple) else (output, b"")
        stdout = (stdout_raw or b"").decode("utf-8", "replace")
        stderr = (stderr_raw or b"").decode("utf-8", "replace")
        limit = self.config.max_output_chars
        return ExecutionResult(
            response.exit_code, stdout[:limit], stderr[:limit],
            len(stdout) > limit or len(stderr) > limit,
            round((time.monotonic() - started) * 1000),
        )

    def _discard_container(self, expected=None) -> None:
        with self._lock:
            if expected is not None and self._container is not expected:
                return
            container, self._container = self._container, None
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    def close(self) -> None:
        self._discard_container()

    async def aexecute(self, request: ExecutionRequest) -> ExecutionResult:
        container_cwd = self._container_cwd(request.cwd)
        container = self._get_container()
        task = asyncio.create_task(asyncio.to_thread(
            self._execute_in_container, request, container, container_cwd
        ))
        try:
            return await task
        except asyncio.CancelledError:
            # Removing the container terminates the active exec and unblocks the
            # SDK worker; never leave command ownership behind on cancellation.
            self._discard_container(container)
            task.cancel()
            raise
