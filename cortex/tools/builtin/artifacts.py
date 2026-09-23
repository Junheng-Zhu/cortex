from cortex.context.artifacts import ArtifactStore

from ..base import Tool
from ..permission import Permission
from pydantic import BaseModel, Field


class ReadArtifactChunkInput(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=8000)


class ReadArtifactChunkTool(Tool):
    """Page through a result previously externalized by ObservationPolicy."""

    name = "read_artifact_chunk"
    description = (
        "Read a bounded character range from an artifact:// result. Use this when "
        "an observation says its full content was too large to inline."
    )
    input_model = ReadArtifactChunkInput
    permission = Permission.READ
    timeout = 5
    max_retries = 0
    retryable = False

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def execute(self, input: ReadArtifactChunkInput) -> dict:
        chunk = self.store.read_chunk(input.artifact_id, input.offset, input.limit)
        size = self.store.size(input.artifact_id)
        next_offset = input.offset + len(chunk)
        return {
            "artifact_ref": "artifact://" + input.artifact_id.removeprefix("artifact://"),
            "offset": input.offset,
            "next_offset": next_offset,
            "size_chars": size,
            "eof": next_offset >= size,
            "content": chunk,
        }
