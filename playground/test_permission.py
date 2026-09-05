import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor
from src.tools.file_tools import ReadNoteTool
from src.tools.exceptions import *

allowed_permissions_1 = {"READ"}
allowed_permissions_2 = {"WRITE"}

registry = ToolRegistry()
read_note_tool = ReadNoteTool()
# executor = ToolExecutor(allowed_permissions_1,registry)
executor = ToolExecutor(allowed_permissions_2,registry)
registry.register(read_note_tool)


# ① 正常 Tool
#    "read_note" + {"filename": "python.md"}
if executor.permission_check("read_note"):
    result = executor.execute("read_note", {"filename": "python.md"})
    print(result)
else:
    raise ToolPermissionError("没有访问该工具的权限","read_note")