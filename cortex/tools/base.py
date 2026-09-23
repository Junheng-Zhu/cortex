from abc import ABC, abstractmethod
from typing import Any
from .permission import Permission


class Tool(ABC):
    name: str
    description: str
    input_model: object
    permission: Permission
    retryable: bool
    max_retries:int
    timeout:int

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError

    # 这里的execute(self,xxx),xxx要怎么换成pydantic进行验证


class ToolError(Exception):
    """Base class for controlled tool failures."""


class ToolNotFoundError(ToolError):
    def __init__(self, message: str, tool_name: str):
        self.message, self.tool_name = message, tool_name
        super().__init__(message)

    def __str__(self) -> str:
        return f"工具名称 {self.tool_name} 失败原因: {self.message}"


class ToolSandboxError(ToolError):
    def __init__(self, message: str, tool_name: str, file_dir: object):
        self.message, self.tool_name, self.file_dir = message, tool_name, file_dir
        super().__init__(message)

    def __str__(self) -> str:
        return f"工具名称 {self.tool_name} 访问地址：'{self.file_dir}'失败原因: {self.message}"


class ToolFileNotFoundError(ToolSandboxError):
    pass


class ToolTimeoutError(ToolError):
    pass
