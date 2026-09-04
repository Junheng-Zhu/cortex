# playground/test_registry.py

import sys
import os

# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, PROJECT_ROOT)

from src.tools.base import Tool
from src.tools.registry import ToolRegistry


class ToolA(Tool):
    name="tool_a"
    def execute(filename:str):
        print(filename)

class ToolB(Tool):
    name="tool_b"
    def execute(filename:str, content:str):
        print(filename+content)

a=ToolA()
b=ToolB()
registry=ToolRegistry()
registry.register(a)
#这里()里面要传入一个对象是吗，这样正确吗
registry.register(b)

print(registry._tools)