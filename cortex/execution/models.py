"""Value objects shared by execution backends."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    command: str
    cwd: Path
    timeout: float


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
