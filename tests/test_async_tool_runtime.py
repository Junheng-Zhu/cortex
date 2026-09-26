import asyncio
import multiprocessing
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from cortex.tools.base import (
    ConcurrencyPolicy, ExecutionStrategy, Tool,
)
from cortex.tools.executor import ToolExecutor
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry
from cortex.execution import LocalBackend
from cortex.tools.builtin.shell import ShellInput, ShellTool
from cortex.tools.builtin.notes import ListNotesTool, ReadNoteTool
from cortex.runtime.loop import AgentLoop
from cortex.llm.protocol import LLMResponse, ToolCall


class DelayInput(BaseModel):
    delay: float = 0.05
    value: str = "ok"
    fail: bool = False


class AsyncProbeTool(Tool):
    name = "probe"
    description = "test probe"
    input_model = DelayInput
    permission = Permission.READ
    timeout = 2
    max_retries = 0
    retryable = False
    execution_strategy = ExecutionStrategy.BACKEND_SUPERVISED
    concurrency_policy = ConcurrencyPolicy.PARALLEL_SAFE

    async def aexecute(self, input):
        await asyncio.sleep(input.delay)
        if input.fail:
            raise RuntimeError("probe failed")
        return input.value

    def execute(self, input):
        return input.value


class SerialProbeTool(AsyncProbeTool):
    name = "serial_probe"
    concurrency_policy = ConcurrencyPolicy.SERIAL


class BlockingProcessTool(AsyncProbeTool):
    name = "blocking_process"
    execution_strategy = ExecutionStrategy.PROCESS_SUPERVISED

    def execute(self, input):
        time.sleep(input.delay)
        return input.value


def executor(limit=4):
    registry = ToolRegistry()
    registry.register(AsyncProbeTool())
    registry.register(SerialProbeTool())
    return ToolExecutor({Permission.READ}, registry, max_tool_concurrency=limit)


def test_builtin_concurrency_policies_are_explicitly_safe():
    assert ShellTool().concurrency_policy is ConcurrencyPolicy.SERIAL
    assert ListNotesTool().concurrency_policy is ConcurrencyPolicy.PARALLEL_SAFE
    assert ReadNoteTool().concurrency_policy is ConcurrencyPolicy.PARALLEL_SAFE


@pytest.mark.asyncio
async def test_parallel_safe_calls_overlap_and_preserve_input_order():
    runtime = executor()
    started = time.perf_counter()
    results = await runtime.aexecute_many([
        ("probe", {"delay": 0.15, "value": "first"}),
        ("probe", {"delay": 0.01, "value": "second"}),
    ])
    assert time.perf_counter() - started < 0.25
    assert [result.data for result in results] == ["first", "second"]
    assert runtime.last_batch_metrics["peak_concurrency"] == 2


@pytest.mark.asyncio
async def test_concurrency_limit_and_ordered_serial_barrier():
    runtime = executor(limit=2)
    results = await runtime.aexecute_many([
        ("probe", {"delay": 0.04, "value": "a"}),
        ("probe", {"delay": 0.04, "value": "b"}),
        ("serial_probe", {"delay": 0.01, "value": "barrier"}),
        ("probe", {"delay": 0.01, "value": "c"}),
        ("probe", {"delay": 0.01, "value": "d"}),
        ("probe", {"delay": 0.01, "value": "e"}),
    ])
    assert [result.data for result in results] == ["a", "b", "barrier", "c", "d", "e"]
    assert runtime.last_batch_metrics["peak_concurrency"] == 2
    assert [wave["policy"] for wave in runtime.last_batch_metrics["waves"]] == [
        "parallel_safe", "serial", "parallel_safe"
    ]


@pytest.mark.asyncio
async def test_normal_failure_does_not_cancel_wave_sibling():
    results = await executor().aexecute_many([
        ("probe", {"fail": True}),
        ("probe", {"value": "survived"}),
    ])
    assert not results[0].success
    assert results[0].error_type == "RuntimeError"
    assert results[1].success and results[1].data == "survived"


@pytest.mark.asyncio
async def test_cancellation_propagates_to_backend_supervised_call():
    task = asyncio.create_task(executor().aexecute("probe", {"delay": 10}))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_process_supervision_cancellation_terminates_child():
    registry = ToolRegistry()
    registry.register(BlockingProcessTool())
    runtime = ToolExecutor({Permission.READ}, registry)
    baseline = {child.pid for child in multiprocessing.active_children()}
    task = asyncio.create_task(
        runtime.aexecute("blocking_process", {"delay": 10})
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {child.pid for child in multiprocessing.active_children()} <= baseline


@pytest.mark.asyncio
async def test_local_shell_cancellation_kills_underlying_process(tmp_path):
    backend = LocalBackend(lambda: Path("/bin/bash"), workspace=tmp_path)
    tool = ShellTool(backend, tmp_path)
    task = asyncio.create_task(tool.aexecute(
        ShellInput(command="sleep 0.4; touch should-not-exist", timeout=2)
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.45)
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_agent_async_outputs_keep_original_call_id_order():
    class Client:
        def __init__(self):
            self.inputs = []

        def respond(self, *, input, tools, previous_response_id):
            self.inputs.append(input)
            if len(self.inputs) == 1:
                return LLMResponse(tool_calls=[
                    ToolCall("call-slow", "probe", {"delay": 0.08, "value": "slow"}),
                    ToolCall("call-fast", "probe", {"delay": 0.01, "value": "fast"}),
                ])
            return LLMResponse(content="done")

    client = Client()
    runtime = executor()
    agent = AgentLoop(client, runtime)
    assert await agent.arun("run both") == "done"
    outputs = [item for item in client.inputs[1]
               if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["call-slow", "call-fast"]
    assert [item["output"] for item in outputs] == ["slow", "fast"]
