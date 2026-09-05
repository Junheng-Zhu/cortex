import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor
from src.tools.file_tools import ReadNoteTool
from src.tools.exceptions import *
from src.tools.permission import Permission


allowed_permissions_1 = {Permission.READ}
allowed_permissions_2 = {Permission.WRITE}

registry = ToolRegistry()
read_note_tool = ReadNoteTool()
# executor = ToolExecutor(allowed_permissions_1,registry)
executor = ToolExecutor(allowed_permissions_2,registry)
registry.register(read_note_tool)


# ① 正常 Tool
#    "read_note" + {"filename": "python.md"}
try: result=executor.execute("read_note",{"filename": "python.md"})
except ToolPermissionError as e:
    print(str(e))

