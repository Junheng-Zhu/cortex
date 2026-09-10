import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor
from src.tools.file_tools import *
from src.tools.permission import Permission


registry = ToolRegistry()
read_note_tool = ReadNoteTool()
delete_note_tool = DeleteNoteTool()
slow_tool = SlowTool()

allowed_permissions = {Permission.READ, Permission.WRITE, Permission.DELETE}

executor = ToolExecutor(allowed_permissions,registry)

registry.register(read_note_tool)
registry.register(delete_note_tool)
registry.register(slow_tool)

list1=executor.list_tool_schemas()
print(list1)

""" {'name': 'read_note',
 'description': '读取 notes 目录下指定文件的内容。', 
 'properties': {'filename': {'title': 'Filename', 'type': 'string'}}, 
                'required': ['filename'], 
                'title': 'ReadNoteInput', 
                'type': 'object'} """