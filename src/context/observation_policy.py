from dataclasses import dataclass

from runtime.observation import Observation

from .artifact_store import ArtifactStore


@dataclass
class ObservationPolicy:
    """Keep small tool results inline and replace large ones with a durable ref."""

    artifact_store: ArtifactStore
    inline_limit: int = 12_000
    preview_chars: int = 2_000

    def __post_init__(self) -> None:
        if self.inline_limit < 1 or self.preview_chars < 1:
            raise ValueError("observation limits must be positive")

    def success(self, action_id: str, tool_name: str, value: object) -> Observation:
        serialized = self.artifact_store.serialize(value)
        if len(serialized) <= self.inline_limit:
            return Observation(action_id=action_id, success=True, output=value)

        artifact = self.artifact_store.put(value)
        return Observation(
            action_id=action_id,
            success=True,
            output=serialized[: self.preview_chars],
            preview=serialized[: self.preview_chars],
            artifact_id=artifact.artifact_id,
            artifact_ref=artifact.ref,
            artifact_path=str(artifact.path),
            size_chars=artifact.size_chars,
            truncated=True,
        )

    @staticmethod
    def model_output(tool_name: str, observation: Observation) -> str:
        if not observation.truncated:
            return str(observation.output)
        return (
            f"Tool {tool_name} succeeded. Content is too large to inline. "
            f"Preview: {observation.preview}\n"
            f"artifact_ref: {observation.artifact_ref} "
            f"size: {observation.size_chars} chars. "
            "Use read_artifact_chunk if more content is needed."
        )
