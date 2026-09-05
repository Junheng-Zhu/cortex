from .registry import ToolRegistry
from typing import Any
from .exceptions import *


class ToolExecutor():

    def __init__(self,registry:ToolRegistry):
        self.registry=registry

    # def execute(self,tool_name,**kwargs):
    def execute(self,tool_name, arguments)->Any:
        try: tool=self.registry.get(tool_name)
        except ToolNotFoundError as e:
            return e.__str__
        #异常里面的函数怎么使用呢

        validated_input=tool.input_model(**arguments)
        return tool.execute(validated_input)

    