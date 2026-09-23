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
        # Text-mode reads preserve Unicode character semantics while keeping
        # memory bounded; an artifact is never loaded wholesale for paging.
        with path.open(encoding="utf-8") as stream:
            remaining = offset
            while remaining:
                discarded = stream.read(min(remaining, 8192))
                if not discarded:
                    return ""
                remaining -= len(discarded)
            return stream.read(limit)

    def size(self, artifact_id: str) -> int:
        path = self._path(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")
        size = 0
        with path.open(encoding="utf-8") as stream:
            while chunk := stream.read(8192):
                size += len(chunk)
        return size


@dataclass
class ObservationPolicy:
    """Keep small results inline and externalize oversized observations."""
    artifact_store: ArtifactStore
    inline_limit: int = 12_000
    preview_chars: int = 2_000

    def __post_init__(self) -> None:
        if self.inline_limit < 1 or self.preview_chars < 1:
            raise ValueError("observation limits must be positive")

    def success(self, action_id: str, tool_name: str, value: object):
        from cortex.runtime.observation import Observation
        serialized = self.artifact_store.serialize(value)
        if len(serialized) <= self.inline_limit:
            return Observation(action_id=action_id, success=True, output=value)
        artifact = self.artifact_store.put(value)
        preview = serialized[: self.preview_chars]
        return Observation(action_id=action_id, success=True, output=preview, preview=preview, artifact_id=artifact.artifact_id, artifact_ref=artifact.ref, artifact_path=str(artifact.path), size_chars=artifact.size_chars, truncated=True)

    @staticmethod
    def model_output(tool_name: str, observation) -> str:
        if not observation.truncated:
            return str(observation.output)
        return (f"Tool {tool_name} succeeded. Content is too large to inline. Preview: {observation.preview}\n"
                f"artifact_ref: {observation.artifact_ref} size: {observation.size_chars} chars. "
                "Use read_artifact_chunk if more content is needed.")
