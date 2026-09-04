from pydantic import BaseModel
from .base import Tool
from .file_tools import read_notes

class ReadNoteInput(BaseModel):
    path:str
    filename:str



class ReadNoteTool(Tool):
    name="read_note"
    description="读取 notes 目录下指定文件的内容。"
    input_model=ReadNoteInput()

    def execute(self,path:str,filename:str):
        self.input_model(path,filename)
        read_notes(filename)


