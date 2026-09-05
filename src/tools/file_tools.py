import os
import glob
from typing import List, Dict, Any
from .base import Tool
from .schemas import ReadNoteInput
from pathlib import Path
from .exceptions import *
from .permission import permission


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
        raise ToolAccessPermissionError("读取其他路径", "read_note", filep.resolve())

    if not filep.exists():
        raise ToolFileNotFoundError("文件不存在","read_note",filepath)


    content = filep.read_text(encoding="utf-8")
    return content




# # 工具注册表（供 Agent 调用）
# TOOL_REGISTRY = {
#     "list_notes": {
#         "func": list_notes,
#         "description": "列出笔记文件夹中的所有笔记文件名称。无需任何参数。",
#         "parameters": {"type": "object", "properties": {}},
#     },
#     "read_note": {
#         "func": read_note,
#         "description": "读取指定笔记文件的完整内容。",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "filename": {
#                     "type": "string",
#                     "description": "笔记文件的名称，例如 'python.md'",
#                 }
#             },
#             "required": ["filename"],
#         },
#     },
# }


class ReadNoteTool(Tool):
    name = "read_note"
    description = "读取 notes 目录下指定文件的内容。"
    input_model = ReadNoteInput
    permission = permission.READ

    def execute(self, input) -> str:

        return read_note(input.filename)
