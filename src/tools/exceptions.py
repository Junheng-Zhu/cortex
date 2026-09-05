
class ToolError(Exception):
    pass

class ToolNotFoundError(ToolError):
    pass

class ToolValidationError(ToolError):
    pass

class ToolExecutionError(ToolError):
    pass

