from .registry import ToolRegistry
from typing import Any
from .exceptions import *


class ToolExecutor:

    def __init__(self, allowed_permissions: set, registry: ToolRegistry):
        self.registry = registry
        self.allowed_permissions=allowed_permissions

    # def execute(self,tool_name,**kwargs):
    def execute(self, tool_name, arguments) -> Any:
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError as e:
            return str(e)

        validated_input = tool.input_model(**arguments)
        return tool.execute(validated_input)

    def permission_check(self, tool_name)->bool:
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError as e:
            return str(e)

        # 这里和上面执行的代码完全一样，要不要抽离一个方法出来


        if tool.permission.value in self.allowed_permissions:
            return True
        else:
            return False
        # 没有权限要raise吗


        

        
