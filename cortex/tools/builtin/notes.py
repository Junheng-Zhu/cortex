import glob
import os
from pathlib import Path
from time import sleep
from typing import List

from ..base import ConcurrencyPolicy, Tool, SideEffectPolicy
from ..base import ToolFileNotFoundError, ToolSandboxError
from ..permission import Permission
from pydantic import BaseModel


class EmptyInput(BaseModel):
    pass


class ReadNoteInput(BaseModel):
    filename: str


class DeleteNoteInput(BaseModel):
    filename: str


class SlowToolInput(BaseModel):
    seconds: int


# 设置笔记目录（项目根目录下的 notes 文件夹）

BASE_DIR = Path(__file__).resolve().parents[3]
NOTES_DIR = BASE_DIR / "notes"


def list_notes(directory=None) -> List[str]:
    """
    列出 notes 目录下的所有文件名称。
    """
    directory = Path(directory or NOTES_DIR)
    if directory.is_symlink():
        raise ToolSandboxError("symlink notes directory", "list_notes", directory)
    if not directory.exists():
        return ["notes 目录为空。"]
    return sorted(p.name for p in directory.iterdir() if p.is_file() and not p.is_symlink())


def read_note(filename: str, directory=None) -> str:
    """
    读取 notes 目录下指定文件的内容。
    参数 filename: 文件名（如 "python.md"）
    """
    directory = Path(directory or NOTES_DIR)
    from cortex.runtime.workspace_paths import safe_path
    try:
        filepath = safe_path(directory.parent, "notes/" + filename)
        if filepath.is_symlink():
            raise ValueError("symlink note")
    except ValueError as exc:
        raise ToolSandboxError(str(exc), "read_note", filename) from exc
    if not filepath.exists():
        raise ToolFileNotFoundError("文件不存在", "read_note", filepath)
    return filepath.read_text(encoding="utf-8")


def delete_note(filename: str) -> bool:

    pass


def slow_tool(a: int):
    sleep(a)


class ReadNoteTool(Tool):
    name = "read_note"
    description = "读取 notes 目录下指定文件的内容。"
    input_model = ReadNoteInput
    permission = Permission.READ
    timeout = 5
    max_retries = 3
    retryable = True
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE

    def __init__(self, workspace=None):
        self.directory = Path(workspace).resolve() / "notes" if workspace else NOTES_DIR

    def execute(self, input) -> str:

        return read_note(input.filename, self.directory)


class ListNotesTool(Tool):
    name = "list_notes"
    description = "列出 notes 目录下可供读取的文件名。读取未知笔记前应先调用此工具。"
    input_model = EmptyInput
    permission = Permission.READ
    timeout = 5
    max_retries = 0
    retryable = False
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE

    def __init__(self, workspace=None):
        self.directory = Path(workspace).resolve() / "notes" if workspace else NOTES_DIR

    def execute(self, input) -> list[str]:
        return list_notes(self.directory)


class DeleteNoteTool(Tool):
    name = "delete_note"
    description = "删除 notes 目录下指定文件。"
    input_model = DeleteNoteInput
    side_effect_policy = SideEffectPolicy.WORKSPACE_REVERSIBLE
    permission = Permission.DELETE
    timeout = 5
    max_retries = 0
    retryable = False

    def __init__(self, workspace=None):
        self.directory = Path(workspace).resolve() / "notes" if workspace else NOTES_DIR

    def execute(self, input) -> bool:

        from cortex.runtime.workspace_paths import safe_path
        path = safe_path(self.directory.parent, "notes/" + input.filename)
        if path.is_symlink():
            raise ToolSandboxError("symlink note", self.name, path)
        path.unlink()
        return True


class SlowTool(Tool):
    name = "slow_tool"
    description = "执行一个耗时操作。"
    input_model = SlowToolInput
    permission = Permission.READ
    timeout = 5
    max_retries = 3
    retryable = True

    def execute(self, input):
        slow_tool(input.seconds)
