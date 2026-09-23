import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class Artifact:
    """Metadata for a tool result stored outside the model context."""

    artifact_id: str
    path: Path
    size_chars: int

    @property
    def ref(self) -> str:
        return f"artifact://{self.artifact_id}"


class ArtifactStore:
    """A small, filesystem-backed store for oversized textual observations."""

    def __init__(self, root: str | Path = ".cortex/artifacts") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def serialize(value: object) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            return str(value)

    def put(self, value: object) -> Artifact:
        content = self.serialize(value)
        artifact_id = uuid4().hex
        path = self.root / f"{artifact_id}.txt"
        temporary = self.root / f".{artifact_id}.tmp"
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
        return Artifact(artifact_id, path, len(content))

    def _path(self, artifact_id: str) -> Path:
        normalized = artifact_id.removeprefix("artifact://")
        if not normalized or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("Invalid artifact id")
        path = (self.root / f"{normalized}.txt").resolve()
        if path.parent != self.root:
            raise ValueError("Invalid artifact id")
        return path

    def read_chunk(self, artifact_id: str, offset: int = 0, limit: int = 4000) -> str:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        path = self._path(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")
        # Offsets are Unicode character offsets, not encoding-dependent bytes.
        content = path.read_text(encoding="utf-8")
        return content[offset : offset + limit]

    def size(self, artifact_id: str) -> int:
        path = self._path(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")
        return len(path.read_text(encoding="utf-8"))
