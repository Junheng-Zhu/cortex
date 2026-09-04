from pydantic import BaseModel
from .base import Tool

class ReadNoteInput(BaseModel):
    path:str



