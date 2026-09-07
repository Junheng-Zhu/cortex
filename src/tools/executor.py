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
    
            

    def _execute_with_retry(self,tool:Tool,validated_input)->ToolResult:
        attempts=0

        for i in range(self.max_retries+1):
            attempt+=1
            try:
                with concurrent.futures.ThreadPoolExecutor() as time_executor:
                    future = time_executor.submit(self._execute_once,tool,validated_input)
                    try:
                        result = future.result(self.timeout)
                    except concurrent.futures.TimeoutError:
                        raise ToolTimeoutError
                

            except ToolError as e:
                if self._should_retry(e,tool,attempts):
                    continue
                else:
                    return ToolResult(tool_name=tool.name,
                                      attempts=attempt,
                                      duration_ms=0,
                                      success=False,
                                      error=str(e),
                                      data=None)
                
            else:
                return ToolResult(tool_name=tool.name,
                                  attempts=attempt,
                                  duration_ms=0,
                                  success=True,
                                  error="",
                                  data=result)
            
            

    def execute(self, tool_name:str, arguments) -> ToolResult:
        start = time.perf_counter()
        tool = self._get_tool(tool_name)
        if not self._check_permission(tool):
            return ToolResult(tool_name=tool.name,
                              attempts=0,
                              duration_ms=0,
                              success=False,
                              error="Permission denied",
                              data=None)
        # 这里直接不要引起异常，返回一个 ToolResult 对象，表示验证失败？
        try:validated_input = self._validate(tool,arguments)
        except :
            return ToolResult(tool_name=tool.name,
                              attempts=0,
                              duration_ms=0,
                              success=False,
                              error="Input validation failed",
                              data=None)
        
        tool_result= self._execute_with_retry(tool,validated_input)
        end = time.perf_counter()
        tool_result.duration_ms = int((end - start) * 1000)
        return tool_result
