# Cortex Execution Runtime v1

## Architecture

`ShellTool` remains responsible for its stable Pydantic input, command policy, and
workspace path validation. It converts accepted input into an `ExecutionRequest`
and delegates execution to an injected `ExecutionBackend`. Backends return an
`ExecutionResult`; the tool deliberately exposes only the pre-v1 keys
`exit_code`, `stdout`, `stderr`, and `truncated`. Backend duration is available to
runtime callers without changing the model-visible contract.

`LocalBackend` runs the discovered Bash directly. `DockerBackend` lazily creates
one container per backend instance and reuses it for the session. `close()` (and
the context-manager exit) force-removes that container. A timed-out or failed
container is force-removed and never reused.

## Trust boundary and Docker defaults

The Docker daemon remains privileged infrastructure and is outside the sandbox
trust boundary. The workspace is the only host path mounted, at `/workspace`.
The Docker socket is never mounted. Container creation uses these defaults:

* networking disabled;
* read-only root filesystem and a size-limited, `noexec`, `nosuid` `/tmp` tmpfs;
* all Linux capabilities dropped and `no-new-privileges` enabled;
* non-root UID/GID 1000, an explicit minimal environment, and no inherited host
  environment;
* 512 MiB memory, one CPU, and 128 PID limits.

Build the offline-capable image with:

```bash
docker build -t cortex-execution-runtime:v1 docker/execution-runtime
```

It contains Bash, Python, pytest, Git, grep, and find. Image construction itself
requires package-network access; command execution does not.

## Backend differences and limitations

Local execution has host user privileges and host network/environment visibility;
its safety policy is therefore a guardrail, not strong isolation. Docker execution
provides the isolation and resource controls above, but requires a trusted Docker
daemon and prebuilt image. The workspace is writable in v1, so a sandboxed command
can modify it. Docker's daemon and kernel are shared attack surfaces. v1 does not
provide async execution, snapshots, SSH, Kubernetes, sub-agents, or per-command
containers. Output is UTF-8-decoded with replacement and bounded to 10,000
characters per stream.
