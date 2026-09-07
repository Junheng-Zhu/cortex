from typing import Any

class ToolResult():
    attempts:int
    duration_ms:int
    success:bool
    error:str
    data:Any
    def __init__(self,tool_name:str,attempts:int,duration_ms:int,success:bool,error:str,data:Any):
        self.tool_name=tool_name
        self.attempts=attempts
        self.duration_ms=duration_ms
        self.success=success
        self.error=error
        self.data=data

