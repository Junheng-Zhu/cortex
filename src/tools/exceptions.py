
class ToolError(Exception):
    pass

class ToolNotFoundError(ToolError):

    def __init__(self, message, tool_name):
        self.message = message
        self.field_name = tool_name
        super().__init__(message)   
        #这一行一定要有吗，这里不太理解

    def __str__(self):
        return f"字段 '{self.field_name}' 校验失败: {self.message}"
    

class ToolValidationError(ToolError):
    pass

class ToolExecutionError(ToolError):
    pass

