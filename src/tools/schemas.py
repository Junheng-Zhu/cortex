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
