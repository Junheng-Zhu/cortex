from .registry import ToolRegistry
from typing import Any
from .exceptions import *
from .base import Tool
from .result import ToolResult
import time
import concurrent.futures



class ToolExecutor:

    def __init__(self, allowed_permissions: set, registry: ToolRegistry):
        self.registry = registry
        self.allowed_permissions = allowed_permissions
        self.timeout=1
        self.max_retries=1

    # def execute(self,tool_name,**kwargs):

    def _get_tool(self, tool_name: str) -> Tool:
        tool = self.registry.get(tool_name)
        return tool

    def _check_permission(self, tool: Tool) -> bool:
        return tool.permission in self.allowed_permissions

    def _validate(self,tool:Tool,arguments)->Any:
        return tool.input_model(**arguments)

    def _execute_once(self,tool:Tool,validated_input)->Any:
        return tool.execute(validated_input)

    def _should_retry(self,error,tool:Tool,attempts:int)->bool:
        if tool.retryable ==False:
            return False
        elif error ==ToolTimeoutError:
            return True
        elif attempts >=tool.max_retries:
            return False
        else:
            return False
    
            

    def _execute_with_retry(self,tool:Tool,validated_input):
        attempt=0

        for i in range(self.max_retries+1):
            attempt+=1
            try:
                with concurrent.futures.ThreadPoolExecutor() as time_executor:
                    future = time_executor.submit(self._execute_once,tool,validated_input)
                    try:
                        result = future.result(self.timeout)
                    except concurrent.futures.TimeoutError:
                        raise ToolTimeoutError
                


            except ToolPermissionError:
                break
            except ToolFileNotFoundError:
                break
            except ToolTimeoutError:
                continue
            else:
                return result
            

    def execute(self, tool_name:str, arguments) -> ToolResult:
        tool = self._get_tool(tool_name)
        if not self._check_permission(tool):
            raise ToolPermissionError("没有访问该工具的权限", tool_name)
        try:validated_input = self._validate(tool,arguments)
        except:
            raise ToolValidationError("输入不符合工具要求",tool_name)
        result_data= self._execute_with_retry(tool,validated_input)
        return ToolResult(tool_name, 0,self.timeout, True, None, result_data)
