from .registry import ToolRegistry
from typing import Any
from .exceptions import *
from .base import Tool


class ToolExecutor:

    def __init__(self, allowed_permissions: set, registry: ToolRegistry):
        self.registry = registry
        self.allowed_permissions=allowed_permissions

    # def execute(self,tool_name,**kwargs):

    def _get_tool(self,tool_name:str)->Tool:
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError :
            raise ToolNotFoundError
        # 这里怎么继续抛出异常呢，还要传参数吗，还是可以返回内部的异常
        return tool


    def _check_permission(self,tool:Tool)->bool:
        return tool.permission in self.allowed_permissions

    def execute(self, tool_name, arguments) -> Any:
        try:tool=self._get_tool(tool_name)
        except ToolNotFoundError as e:
            raise ToolNotFoundError

        if not self._check_permission(tool):
            raise ToolPermissionError("没有访问该工具的权限",tool_name)

        validated_input = tool.input_model(**arguments)
        return tool.execute(validated_input)




        

        
