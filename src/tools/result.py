from typing import Any


class ToolResult:
    attempts: int
    duration_ms: int
    success: bool
    error: str
    data: Any

    def __init__(
        self,
        tool_name: str,
        attempts: int,
        duration_ms: int,
        success: bool,
        error_type: str,
        error_message: str,
        data: Any,
        validation_passed: bool | None = None,
    ):
        self.tool_name = tool_name
        self.attempts = attempts
        self.duration_ms = duration_ms
        self.success = success
        self.error_type = error_type
        self.error_message = error_message
        self.data = data
        self.validation_passed = validation_passed

    def __str__(self):
        return f"ToolResult(tool_name={self.tool_name}, attempts={self.attempts}, duration_ms={self.duration_ms}, success={self.success}, error_type={self.error_type}, error_message={self.error_message}, data={self.data})"
