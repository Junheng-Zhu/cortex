class ToolError(Exception):
    pass


class ToolNotFoundError(ToolError):

    def __init__(self, message, tool_name):
        self.message = message
        self.tool_name = tool_name
        # super().__init__(message)
        # 我执行时，发现有没有这一行，都不影响执行str(e)，是否要保留

    def __str__(self):
        return f"工具名称 '{self.tool_name}' 失败原因: {self.message}"


class ToolValidationError(ToolError):
    pass


class ToolExecutionError(ToolError):
    pass
