import os
import sys
import time
import multiprocessing

import pytest
from pydantic import BaseModel

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.tools.base import Tool
from src.tools.executor import ToolExecutor
from src.tools.permission import Permission
from src.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    pass


class EchoInput(BaseModel):
    text: str


class EchoTool(Tool):
    name = "echo"
    description = "normal tool"
    input_model = EchoInput
    permission = Permission.READ
    retryable = False
    max_retries = 0
    timeout = 2

    def execute(self, input):
        return input.text


class SlowTool(Tool):
    name = "slow"
    description = "tool used to test process timeout"
    input_model = EmptyInput
    permission = Permission.READ
    retryable = False
    max_retries = 0
    timeout = 1

    def execute(self, input):
        time.sleep(3)
        return "done"


class FlakyTool(Tool):
    """
    用共享计数器模拟：
    attempt 1 -> ToolTimeoutError
    attempt 2 -> success

    注意：因为 Executor 每次 retry 都会创建新的 Process，
    普通的 self.attempts 不能跨进程保存，所以这里使用 multiprocessing.Value。
    """

    name = "flaky"
    description = "first attempt raises ToolTimeoutError, second succeeds"
    input_model = EmptyInput
    permission = Permission.READ
    retryable = True
    max_retries = 1
    timeout = 2

    def __init__(self, shared_attempts):
        self.shared_attempts = shared_attempts

    def execute(self, input):
        with self.shared_attempts.get_lock():
            self.shared_attempts.value += 1
            current_attempt = self.shared_attempts.value

        if current_attempt == 1:
            # 用 ToolTimeoutError 测试当前 Executor 的 retry policy。
            from src.tools.exceptions import ToolTimeoutError

            raise ToolTimeoutError("simulated transient timeout")

        return "success"


class DeleteLikeTool(Tool):
    name = "delete_like"
    description = "tool requiring DELETE permission"
    input_model = EmptyInput
    permission = Permission.DELETE
    retryable = False
    max_retries = 0
    timeout = 2

    def execute(self, input):
        return "should never execute"


def register(registry, *tools):
    for tool in tools:
        registry.register(tool)


def test_success():
    registry = ToolRegistry()
    register(registry, EchoTool())
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("echo", {"text": "hello"})

    assert result.success is True
    assert result.data == "hello"
    assert result.attempts == 1
    assert result.error_type is None
    assert result.error_message is None
    assert result.validation_passed is True


def test_validation_error():
    registry = ToolRegistry()
    register(registry, EchoTool())
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("echo", {})

    assert result.success is False
    assert result.error_type == "ToolValidationError"
    assert result.attempts == 0
    assert result.validation_passed is False


def test_permission_denied():
    registry = ToolRegistry()
    register(registry, DeleteLikeTool())
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("delete_like", {})

    assert result.success is False
    assert result.error_type == "ToolPermissionError"
    assert result.attempts == 0
    assert result.validation_passed is None


def test_timeout():
    registry = ToolRegistry()
    register(registry, SlowTool())
    executor = ToolExecutor({Permission.READ}, registry)

    start = time.perf_counter()
    result = executor.execute("slow", {})
    elapsed = time.perf_counter() - start

    assert result.success is False
    assert result.error_type == "ToolTimeoutError"
    assert result.attempts == 1

    # Windows 下给进程启动留少量余量，但不能等完整的 3 秒。
    assert elapsed < 2.5


def test_retry_after_transient_timeout():
    registry = ToolRegistry()

    shared_attempts = multiprocessing.Value("i", 0)
    tool = FlakyTool(shared_attempts)
    register(registry, tool)

    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("flaky", {})

    assert result.success is True
    assert result.data == "success"
    assert result.attempts == 2
    assert shared_attempts.value == 2


def test_unknown_tool():
    registry = ToolRegistry()
    executor = ToolExecutor({Permission.READ}, registry)

    with pytest.raises(Exception):
        executor.execute("not_exist", {})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# pytest tests/test_tool_runtime.py -v
