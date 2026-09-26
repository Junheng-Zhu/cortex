"""Value objects shared by execution backends."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    command: str
    # Backends own the mapping of this workspace-relative path.
    cwd: Path
    timeout: float

    def __post_init__(self) -> None:
        cwd = Path(self.cwd)
        if cwd.is_absolute() or ".." in cwd.parts:
            raise ValueError("execution cwd must be workspace-relative and cannot escape")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
