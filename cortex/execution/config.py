"""Configuration for execution backends."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DockerBackendConfig:
    workspace: Path
    image: str = "cortex-execution-runtime:v1"
    memory_limit: str = "512m"
    nano_cpus: int = 1_000_000_000
    pids_limit: int = 128
    user: str = "1000:1000"
    network_disabled: bool = True
    tmpfs: dict[str, str] = field(
        default_factory=lambda: {"/tmp": "rw,noexec,nosuid,size=64m"}
    )
    max_output_chars: int = 10_000
