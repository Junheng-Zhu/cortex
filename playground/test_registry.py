# playground/test_registry.py
from cortex.src.tools.base import Tool
from cortex.src.tools.registry import ToolRegistry
#这两个类怎么导包进来
#还有要在那个终端位置执行python xxx.py;(venv) PS D:\pyproject\cortex> 

class ToolA(Tool):
    name="tool_a"

class ToolB(Tool):
    name="tool_b"

a=ToolA()
b=ToolB()
registry=ToolRegistry()
registry.register(a)
#这里()里面要传入一个对象是吗，这样正确吗
registry.register(b)

print(registry._tools)