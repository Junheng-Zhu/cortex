from src.context.artifact_store import ArtifactStore

from .base import Tool
from .permission import Permission
from .schemas import ReadArtifactChunkInput


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
