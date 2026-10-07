from abc import ABC, abstractmethod
from typing import Any
from .permission import Permission
from enum import Enum
import asyncio


class ExecutionStrategy(str, Enum):
    """Who owns timeout and child-resource supervision for a tool call."""

    PROCESS_SUPERVISED = "process_supervised"
    BACKEND_SUPERVISED = "backend_supervised"


class ConcurrencyPolicy(str, Enum):
    """Whether calls to a tool may overlap with other explicitly safe calls."""

    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


class SideEffectPolicy(str, Enum):
    """The kind of durable side effect produced by a tool.

    Workspace writes can be snapshotted and restored by the recovery runtime;
    compensatable and irreversible effects are deliberately only classified in
    V1 (they are not automatically undone).
    """

    NONE = "none"
    WORKSPACE_REVERSIBLE = "workspace_reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


class Tool(ABC):
    name: str
    description: str
    input_model: object
    permission: Permission
    retryable: bool
    max_retries:int
    timeout:int
    execution_strategy = ExecutionStrategy.PROCESS_SUPERVISED
    concurrency_policy = ConcurrencyPolicy.SERIAL
    side_effect_policy = SideEffectPolicy.NONE

    def close(self) -> None:
        """Release runtime-owned resources held by this tool."""

    async def aexecute(self, input: Any) -> Any:
        """Async hook for backend-supervised tools."""
        return await asyncio.to_thread(self.execute, input)

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        raise NotImplementedError

    # 这里的execute(self,xxx),xxx要怎么换成pydantic进行验证


class ToolError(Exception):
    """Base class for controlled tool failures."""


class ToolNotFoundError(ToolError):
    def __reduce__(self):
        return type(self), (self.message, self.tool_name)

    def __init__(self, message: str, tool_name: str):
        self.message, self.tool_name = message, tool_name
        super().__init__(message)

    def __str__(self) -> str:
        return f"工具名称 {self.tool_name} 失败原因: {self.message}"


class ToolSandboxError(ToolError):
    def __reduce__(self):
        return type(self), (self.message, self.tool_name, self.file_dir)

    def __init__(self, message: str, tool_name: str, file_dir: object):
        self.message, self.tool_name, self.file_dir = message, tool_name, file_dir
        super().__init__(message)

    def __str__(self) -> str:
        return f"工具名称 {self.tool_name} 访问地址：'{self.file_dir}'失败原因: {self.message}"


class ToolFileNotFoundError(ToolSandboxError):
    pass


class ToolTimeoutError(ToolError):
    pass


class ShellUnavailableError(ToolError):
    """The host does not provide a Bash executable for the shell runtime."""


class ShellExecutionError(ToolError):
    """Bash was found, but its process could not be started."""
