from .registry import ToolRegistry
from typing import Any
from .exceptions import *
from .base import Tool
from .result import ToolResult
import time
from pydantic import ValidationError
import multiprocessing



class ToolExecutor:

    def __init__(self, allowed_permissions: set, registry: ToolRegistry):
        self.registry = registry
        self.allowed_permissions = allowed_permissions
        self.timeout=10
        self.max_retries=5
        # 全局的 timeout 和 max_retries

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
        elif isinstance(error, ToolTimeoutError):
            return True
        elif attempts >=tool.max_retries:
            return False
        else:
            return False

    def _worker_with_queur(self,tool:Tool,validated_input,queue):
        try:
            result = tool.execute(validated_input)
            tool_result=ToolResult(tool_name=tool.name,
                                  attempts=0,
                                  duration_ms=0,
                                  success=True,
                                  error="",
                                  data=result)



            queue.put(tool_result)
        except ToolError as e:
            tool_result=ToolResult(tool_name=tool.name,
                                   attempts=0,
                                   duration_ms=0,
                                    success=False,
                                    error=str(e),
                                    data=None)
            queue.put(tool_result)

    

    def _execute_with_retry(self,tool:Tool,validated_input)->ToolResult:
        attempts=0
        tool_retries=min(tool.max_retries,self.max_retries)
        tool_timeout=min(tool.timeout,self.timeout)
        tool_queue = multiprocessing.Queue()

        for i in range(tool_retries):
            attempts+=1
            try:

                p=multiprocessing.Process(target=self._worker_with_queur,args=(tool,validated_input,tool_queue))
                p.start()
                p.join(tool_timeout)

                if p.is_alive():
                    p.terminate()
                    p.join()
                    raise ToolTimeoutError
               

            except ToolError as e:
                if self._should_retry(e,tool,attempts):
                    continue
                else:
                    return ToolResult(tool_name=tool.name,
                                      attempts=attempts,
                                      duration_ms=0,
                                      success=False,
                                      error=str(e),
                                      data=None)
                
            else:
                if not tool_queue.empty():
                    tool_result=tool_queue.get()
                    tool_result.attempts=attempts
                    return tool_result
                elif attempts >= tool_retries:
                    return ToolResult(tool_name=tool.name,
                                      attempts=attempts,
                                      duration_ms=0,
                                      success=False,
                                      error="Tool execution failed after maximum retries",
                                      data=None)

            
            
            

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
        except ValidationError as e:
            return ToolResult(tool_name=tool.name,
                              attempts=0,
                              duration_ms=0,
                              success=False,
                              error=str(e),
                              data=None)
        
        tool_result= self._execute_with_retry(tool,validated_input)
        end = time.perf_counter()
        tool_result.duration_ms = int((end - start) * 1000)
        return tool_result
