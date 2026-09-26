# Execution Runtime v2: integration and lifecycle

## Resource ownership

The `AgentLoop` owns its `ToolExecutor`; the executor owns registered tools; and
`ShellTool` owns its injected `ExecutionBackend`. Calling `AgentLoop.close()` (or
leaving its context manager) closes that chain and force-removes a Docker sandbox.
Application entry points use this context-manager lifecycle.

Tools declare an execution strategy. Ordinary tools remain
`PROCESS_SUPERVISED`: `ToolExecutor` runs them in a temporary multiprocessing
worker and owns the hard timeout. `ShellTool` is `BACKEND_SUPERVISED`: it runs in
the main runtime process after the executor applies the same permission,
validation, timeout-clamping, and retry policies. Its backend owns the command
process/container deadline.

This distinction is essential because putting a session-scoped `DockerBackend`
inside a temporary worker transfers container state to a short-lived process.
The parent cannot reuse or reliably close that worker-owned resource, creating
an orphan-container risk. The backend therefore always stays in the runtime
process that owns it.

## Paths and lifecycle

`ExecutionRequest.cwd` is workspace-relative. `ShellTool` rejects escapes before
creating the request; `LocalBackend` maps it beneath its configured host
workspace, while `DockerBackend` maps it beneath `/workspace` and independently
checks the boundary.

Docker containers are created lazily on the first command and reused, preserving
workspace and in-container session state. A timeout or fatal Docker execution
error force-removes and forgets the container. The next command creates a fresh
one. `close()` is idempotent and force-removes the current container. Cortex
labels its containers to support operational orphan checks.

## Trust boundary

The Docker daemon and host kernel remain trusted infrastructure. The sandbox
receives only the writable workspace mount, not the Docker socket or host
environment. Network isolation, read-only rootfs, restricted tmpfs, non-root
execution, dropped capabilities, `no-new-privileges`, and CPU/memory/PID limits
remain enabled. The workspace is intentionally writable and must therefore be
treated as command-controlled after execution.

The v2 image uses Python 3.11 and includes the offline runtime tools Python,
pytest, Git, grep, and find. Neither image construction nor container creation
copies `.env` files or other host secrets.
