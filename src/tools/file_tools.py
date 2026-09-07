import os
import glob
from typing import List, Dict, Any
from .base import Tool
from .schemas import *
from pathlib import Path
from .exceptions import *
from .permission import Permission
from time import sleep



# 设置笔记目录（项目根目录下的 notes 文件夹）

BASE_DIR = Path(__file__).resolve().parent.parent.parent
NOTES_DIR = BASE_DIR / "notes"




def list_notes() -> List[str]:
    """
    列出 notes 目录下的所有文件名称。
    """
    if not os.path.exists(NOTES_DIR):
        os.makedirs(NOTES_DIR, exist_ok=True)
        return ["notes 目录为空，已自动创建该文件夹。"]

    files = glob.glob(os.path.join(NOTES_DIR, "*"))
    # 只返回文件名，不返回完整路径
    return [os.path.basename(f) for f in files if os.path.isfile(f)]


def read_note(filename: str) -> str:
    """
    读取 notes 目录下指定文件的内容。
    参数 filename: 文件名（如 "python.md"）
    """
    filepath = NOTES_DIR / filename
    
    filep = Path(filepath).resolve()

    # 安全检查：防止通过 ../ 读取其他目录
    if not filep.is_relative_to(NOTES_DIR):
        raise ToolSandboxError("读取其他路径", "read_note", filep.resolve())

    if not filep.exists():
        raise ToolFileNotFoundError("文件不存在","read_note",filepath)


    content = filep.read_text(encoding="utf-8")
    return content


def delete_note(filename: str) -> bool:

    pass

def slow_tool(a:int):
    sleep(10)



class ReadNoteTool(Tool):
    name = "read_note"
    description = "读取 notes 目录下指定文件的内容。"
    input_model = ReadNoteInput
    permission = Permission.READ
    timeout = 5
    max_retries = 3
    retryable = True

    def execute(self, input) -> str:

        return read_note(input.filename)

class DeleteNoteTool(Tool):
    name = "delete_note"
    description = "删除 notes 目录下指定文件。"
    input_model = DeleteNoteInput
    permission = Permission.DELETE
    timeout = 5
    max_retries = 0
    retryable = False

    def execute(self,input)->bool:

        pass


class SlowTool(Tool):
    name = "slow_tool"
    description = "删除 notes 目录下指定文件。"
    input_model = ""
    permission = Permission.READ
    timeout = 5
    max_retries = 3
    retryable = True

    def execute(self,input):
        slow_tool(input)

        