from pydantic import BaseModel, Field


class EmptyInput(BaseModel):
    pass


class ReadNoteInput(BaseModel):
    filename: str


class DeleteNoteInput(BaseModel):
    filename: str


class SlowToolInput(BaseModel):
    seconds: int


class ShellInput(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    cwd: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=30)


class ReadArtifactChunkInput(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    # Leave room for paging metadata so this tool's own result stays inline.
    limit: int = Field(default=4000, ge=1, le=8000)
