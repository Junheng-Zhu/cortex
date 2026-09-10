from pydantic import BaseModel


class ReadNoteInput(BaseModel):

    filename: str


class DeleteNoteInput(BaseModel):

    filename: str


class SlowToolInput(BaseModel):
    seconds: int
