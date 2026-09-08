class ToolError(Exception):
    pass


class ToolNotFoundError(ToolError):

    def __init__(self, message, tool_name):
        self.message = message
        self.tool_name = tool_name
        super().__init__(message)
        # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message

    def __str__(self):
        return f"工具名称 {self.tool_name} 失败原因: {self.message}"


class ToolSandboxError(ToolError):

    def __init__(self, message, tool_name, file_dir):
        self.message = message
        self.tool_name = tool_name
        self.file_dir = file_dir
        super().__init__(message)
        # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message

    def __str__(self):
        return f"工具名称 {self.tool_name} 访问地址：'{self.file_dir}'失败原因: {self.message}"

class ToolPermissionError(ToolError):

    def __init__(self, message, tool_name):
        self.message = message
        self.tool_name = tool_name
        super().__init__(message)
        # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message

    def __str__(self):
        return f"工具名称 {self.tool_name} '失败原因: {self.message}"

# FileNotFoundError
class ToolFileNotFoundError(ToolError):

    def __init__(self, message, tool_name, file_dir):
        self.message = message
        self.tool_name = tool_name
        self.file_dir = file_dir
        super().__init__(message)
        # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message

    def __str__(self):
        return f"工具名称 {self.tool_name} 文件地址：'{self.file_dir}'失败原因: {self.message}"


class ToolValidationError(ToolError):
    def __init__(self, message, tool_name):
            self.message = message
            self.tool_name = tool_name
            super().__init__(message)
            # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message
    
    def __str__(self):
        return f"工具名称 {self.tool_name} '失败原因: {self.message}"


class ToolExecutionError(ToolError):
    pass

class ToolTimeoutError(ToolError):
    message = "工具执行超时"

    def __init__(self, tool_name,message=None):
        self.tool_name = tool_name
        if message is not None:
            self.message = message
        super().__init__(self.message)
        # 如果某个地方只把它当普通 Exception 使用，也不会丢掉 message

    def __str__(self):
        return f"工具名称 {self.tool_name} '失败原因: {self.message}"