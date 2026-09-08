import os
import sys
import time
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
    description = "测试输入验证和正常执行"
    input_model = EchoInput
    permission = Permission.READ
    retryable = False
    max_retries = 0
    timeout = 2

    def execute(self, input):
        return input.text


class SlowTool(Tool):
    name = "slow"
    description = "测试 timeout"
    input_model = EmptyInput
    permission = Permission.READ
    retryable = False
    max_retries = 0
    timeout = 1

    def execute(self, input):
        time.sleep(3)
        return "done"


class FlakyTool(Tool):
    name = "flaky"
    description = "第一次失败，第二次成功"
    input_model = EmptyInput
    permission = Permission.READ
    retryable = True
    max_retries = 1
    timeout = 2

    def __init__(self):
        self.attempts = 0

    def execute(self, input):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary failure")
        return "success"


def register(registry, *tools):
    for tool in tools:
        registry.register(tool)


def test_tool_success():
    registry = ToolRegistry()
    register(registry, EchoTool())
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("echo", {"text": "hello"})

    assert result.success is True
    assert result.data == "hello"
    assert result.attempts == 1


def test_validation_error():
    registry = ToolRegistry()
    register(registry, EchoTool())
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("echo", {})

    assert result.success is False
    assert result.attempts == 0
    assert "validation" in result.error.lower() or "field" in result.error.lower()


def test_permission_denied():
    registry = ToolRegistry()
    register(registry, EchoTool())
    executor = ToolExecutor(set(), registry)

    result = executor.execute("echo", {"text": "hello"})

    assert result.success is False
    assert "permission" in result.error.lower()


def test_timeout():
    registry = ToolRegistry()
    register(registry, SlowTool())
    executor = ToolExecutor({Permission.READ}, registry)

    start = time.perf_counter()
    result = executor.execute("slow", {})
    elapsed = time.perf_counter() - start

    assert result.success is False
    assert "timeout" in result.error.lower()
    assert elapsed < 3.0


def test_retry_after_transient_error():
    registry = ToolRegistry()
    tool = FlakyTool()
    register(registry, tool)
    executor = ToolExecutor({Permission.READ}, registry)

    result = executor.execute("flaky", {})

    # 这个测试会直接暴露当前 Executor 的 retry 问题：
    # Worker 把 RuntimeError 转成 ToolResult 后，父进程看不到原始异常类型。
    assert result.success is True
    assert result.data == "success"
    assert result.attempts == 2


def test_unknown_tool():
    registry = ToolRegistry()
    executor = ToolExecutor({Permission.READ}, registry)

    with pytest.raises(Exception):
        executor.execute("not_exist", {})
