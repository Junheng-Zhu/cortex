from .registry import ToolRegistry
from typing import Any
from dataclasses import dataclass


@dataclass
class ToolResult:
    tool_name: str
    attempts: int
    duration_ms: int
    success: bool
    error_type: str | None
    error_message: str | None
    data: Any
    validation_passed: bool | None = None


@dataclass
class ExecutionOutcome:
    success: bool
    error: Exception | None
    attempts: int
    data: Any

    @property
    def error_message(self) -> str | None:
        return str(self.error) if self.error else None

    @property
    def error_type(self) -> str | None:
        return type(self.error).__name__ if self.error else None


from .base import ConcurrencyPolicy, ExecutionStrategy, Tool, ToolTimeoutError
from copy import deepcopy
import time
from pydantic import ValidationError
import multiprocessing
import asyncio
import queue
from uuid import uuid4
from collections.abc import Awaitable, Callable


class ToolExecutor:
    def __init__(
        self, allowed_permissions: set, registry: ToolRegistry,
        max_tool_concurrency: int = 4,
    ):
        if max_tool_concurrency < 1:
            raise ValueError("max_tool_concurrency must be at least 1")
        self.registry = registry
        self.allowed_permissions = allowed_permissions
        self.timeout = 10
        self.max_retries = 5
        self.max_tool_concurrency = max_tool_concurrency
        self.last_batch_metrics: dict[str, Any] = {}
        # These are executor-owned, so concurrent batches/runs share limits.
        self._concurrency_semaphore = asyncio.Semaphore(max_tool_concurrency)
        self._serial_lock = asyncio.Lock()
        # 全局的 timeout 和 max_retries

    # def execute(self,tool_name,**kwargs):

    def _get_tool(self, tool_name: str) -> Tool:
        tool = self.registry.get(tool_name)
        return tool

    def _check_permission(self, tool: Tool) -> bool:
        return tool.permission in self.allowed_permissions

    def _validate(self, tool: Tool, arguments) -> Any:
        normalized_arguments = dict(arguments)
        for name, field in tool.input_model.model_fields.items():
            if normalized_arguments.get(name) is None and not field.is_required():
                # In a strict provider schema a defaulted field is required but
                # nullable. Null therefore means "use the Pydantic default".
                normalized_arguments.pop(name, None)
        return tool.input_model(**normalized_arguments)

    def _runtime_timeout(self, tool: Tool) -> float:
        """Return the executor-enforced hard limit for one tool attempt."""
        return min(tool.timeout, self.timeout)

    def _apply_runtime_limits(self, tool: Tool, validated_input: Any) -> Any:
        """Clamp a tool's requested timeout to the runtime hard limit."""
        if not hasattr(validated_input, "timeout"):
            return validated_input
        runtime_limit = self._runtime_timeout(tool)
        requested = validated_input.timeout
        effective_timeout = (
            min(requested, runtime_limit) if requested else runtime_limit
        )
        return validated_input.model_copy(update={"timeout": effective_timeout})

    @staticmethod
    def _strict_parameters_schema(schema: dict) -> dict:
        """Adapt Pydantic JSON Schema to the Responses strict-tool contract."""
        normalized = deepcopy(schema)

        def allow_null(property_schema: dict) -> None:
            """Preserve Pydantic omission semantics after making a key required."""
            property_schema.pop("default", None)
            variants = property_schema.get("anyOf")
            if isinstance(variants, list):
                if not any(item.get("type") == "null" for item in variants):
                    variants.append({"type": "null"})
                return

            original = dict(property_schema)
            property_schema.clear()
            property_schema["anyOf"] = [original, {"type": "null"}]

        def visit(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    properties = node.get("properties", {})
                    required = set(node.get("required", []))
                    for name, property_schema in properties.items():
                        if name not in required and isinstance(property_schema, dict):
                            allow_null(property_schema)
                    node["required"] = list(properties)
                    node["additionalProperties"] = False

                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(normalized)
        return normalized

    def to_schema(self, tool_name: str) -> dict:
        tool = self._get_tool(tool_name)

        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": self._strict_parameters_schema(
                tool.input_model.model_json_schema()
            ),
            "strict": True,
        }

    def list_tool_schemas(self) -> list:
        schemas = []

        for tool_name in self.registry.list_tools():
            schema = self.to_schema(tool_name)
            schemas.append(schema)

        return schemas

    def _execute_once(self, tool: Tool, validated_input) -> Any:
        return tool.execute(validated_input)

    def _should_retry(
        self, outcome: ExecutionOutcome, tool: Tool, attempts: int
    ) -> bool:
        if not tool.retryable:
            return False
        elif attempts >= min(tool.max_retries, self.max_retries) + 1:
            return False
        elif outcome.error_type == "ToolTimeoutError":
            return True

        else:
            return False

    def _worker_with_queur(
        self, tool: Tool, validated_input, queue: multiprocessing.Queue, attempts: int
    ) -> ExecutionOutcome:
        try:
            result = tool.execute(validated_input)
            outcome = ExecutionOutcome(
                success=True, error=None, attempts=attempts, data=result
            )
            queue.put(outcome)
        except Exception as e:
            outcome = ExecutionOutcome(
                success=False, error=e, attempts=attempts, data=None
            )
            queue.put(outcome)

    def _execute_with_retry(self, tool: Tool, validated_input) -> ToolResult:
        attempts = 0
        tool_retries = min(tool.max_retries, self.max_retries)
        tool_timeout = self._runtime_timeout(tool)

        for i in range(tool_retries + 1):
            attempts += 1
            if tool.execution_strategy is ExecutionStrategy.BACKEND_SUPERVISED:
                outcome = self._execute_backend_supervised(tool, validated_input, attempts)
                if outcome.success:
                    return self._result(tool, outcome)
                if self._should_retry(outcome, tool, attempts):
                    continue
                return self._result(tool, outcome)
            tool_queue = multiprocessing.Queue()
            p = multiprocessing.Process(
                target=self._worker_with_queur,
                args=(tool, validated_input, tool_queue, attempts),
            )
            p.start()
            p.join(tool_timeout)

            if p.is_alive():
                p.terminate()
                outcome = ExecutionOutcome(
                    success=False,
                    error=ToolTimeoutError("Tool timed out"),
                    attempts=attempts,
                    data=None,
                )
            else:
                outcome = tool_queue.get()
            p.join()
            tool_queue.close()
            tool_queue.join_thread()

            if outcome.success:
                return self._result(tool, outcome)
            else:
                if self._should_retry(outcome, tool, attempts):
                    continue
                else:
                    return self._result(tool, outcome)

    def _execute_backend_supervised(
        self, tool: Tool, validated_input: Any, attempts: int
    ) -> ExecutionOutcome:
        """Execute in the owning process; the backend enforces its own deadline."""
        try:
            return ExecutionOutcome(True, None, attempts, tool.execute(validated_input))
        except Exception as exc:
            return ExecutionOutcome(False, exc, attempts, None)

    @staticmethod
    def _result(tool: Tool, outcome: ExecutionOutcome) -> ToolResult:
        return ToolResult(
            tool_name=tool.name,
            attempts=outcome.attempts,
            duration_ms=0,
            success=outcome.success,
            error_type=outcome.error_type,
            error_message=outcome.error_message,
            data=outcome.data if outcome.success else None,
        )

    def close(self) -> None:
        """Idempotently release resources owned by registered tools."""
        first_error = None
        for name in self.registry.list_tools():
            try:
                self.registry.get(name).close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def execute(self, tool_name: str, arguments) -> ToolResult:
        start = time.perf_counter()
        tool = self._get_tool(tool_name)
        if not self._check_permission(tool):
            return ToolResult(
                tool_name=tool.name,
                attempts=0,
                duration_ms=0,
                success=False,
                error_type="ToolPermissionError",
                error_message="Permission denied",
                data=None,
                validation_passed=None,
            )
        try:
            validated_input = self._validate(tool, arguments)
            validated_input = self._apply_runtime_limits(tool, validated_input)
        except ValidationError as e:
            return ToolResult(
                tool_name=tool.name,
                attempts=0,
                duration_ms=0,
                success=False,
                error_type="ToolValidationError",
                error_message=str(e),
                data=None,
                validation_passed=False,
            )

        tool_result = self._execute_with_retry(tool, validated_input)
        tool_result.validation_passed = True
        end = time.perf_counter()
        tool_result.duration_ms = int((end - start) * 1000)
        return tool_result

    async def aexecute(self, tool_name: str, arguments) -> ToolResult:
        """Execute one tool without blocking the event loop."""
        start = time.perf_counter()
        try:
            tool = self._get_tool(tool_name)
        except Exception as exc:
            return ToolResult(tool_name, 0, 0, False, type(exc).__name__,
                              str(exc), None, None)
        if not self._check_permission(tool):
            return ToolResult(tool.name, 0, 0, False, "ToolPermissionError",
                              "Permission denied", None, None)
        try:
            validated = self._apply_runtime_limits(
                tool, self._validate(tool, arguments)
            )
        except ValidationError as exc:
            return ToolResult(tool.name, 0, 0, False, "ToolValidationError",
                              str(exc), None, False)
        result = await self._aexecute_with_retry(tool, validated)
        result.validation_passed = True
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        return result

    async def _aexecute_with_retry(self, tool: Tool, validated: Any) -> ToolResult:
        attempts = 0
        retries = min(tool.max_retries, self.max_retries)
        while attempts <= retries:
            attempts += 1
            if tool.execution_strategy is ExecutionStrategy.BACKEND_SUPERVISED:
                try:
                    outcome = ExecutionOutcome(True, None, attempts,
                                               await tool.aexecute(validated))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = ExecutionOutcome(False, exc, attempts, None)
            else:
                outcome = await self._aexecute_process(tool, validated, attempts)
            if outcome.success or not self._should_retry(outcome, tool, attempts):
                return self._result(tool, outcome)
        raise AssertionError("unreachable")

    async def _aexecute_process(
        self, tool: Tool, validated: Any, attempts: int
    ) -> ExecutionOutcome:
        result_queue = multiprocessing.Queue()
        process = multiprocessing.Process(
            target=self._worker_with_queur,
            args=(tool, validated, result_queue, attempts),
        )
        process.start()
        deadline = asyncio.get_running_loop().time() + self._runtime_timeout(tool)
        try:
            while process.is_alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join)
                return ExecutionOutcome(False, ToolTimeoutError("Tool timed out"),
                                        attempts, None)
            await asyncio.to_thread(process.join)
            for _ in range(100):
                try:
                    return result_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.001)
            return ExecutionOutcome(False, RuntimeError("Tool worker exited without a result"),
                                    attempts, None)
        except asyncio.CancelledError:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join)
            raise
        finally:
            result_queue.close()
            result_queue.join_thread()

    async def aexecute_many(
        self, calls: list[tuple[str, dict[str, Any]]]
    ) -> list[ToolResult]:
        """Execute ordered policy waves and return results in input order."""
        return await self.aexecute_waves(calls)

    def _concurrency_policy(self, tool_name: str) -> ConcurrencyPolicy:
        try:
            return self._get_tool(tool_name).concurrency_policy
        except Exception:
            # Unknown calls are normalized by aexecute and act as a barrier.
            return ConcurrencyPolicy.SERIAL

    async def aexecute_waves(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        on_wave: Callable[[list[tuple[int, ToolResult]], dict[str, Any]],
                          Awaitable[bool]] | None = None,
    ) -> list[ToolResult]:
        """Execute waves, optionally committing each before scheduling the next.

        The callback runs in the owning coroutine and returns whether scheduling
        may continue. This is the Agent's durable observe/reflect boundary.
        """
        batch_id = str(uuid4())
        started = time.perf_counter()
        results: list[ToolResult | None] = [None] * len(calls)
        active = peak = 0
        waves: list[dict[str, Any]] = []

        async def run(index: int, policy: ConcurrencyPolicy) -> None:
            nonlocal active, peak
            async with self._concurrency_semaphore:
                active += 1
                peak = max(peak, active)
                try:
                    name, arguments = calls[index]
                    if policy is ConcurrencyPolicy.SERIAL:
                        async with self._serial_lock:
                            results[index] = await self.aexecute(name, arguments)
                    else:
                        results[index] = await self.aexecute(name, arguments)
                finally:
                    active -= 1

        index = 0
        while index < len(calls):
            policy = self._concurrency_policy(calls[index][0])
            end = index + 1
            if policy is ConcurrencyPolicy.PARALLEL_SAFE:
                while end < len(calls) and self._concurrency_policy(
                    calls[end][0]
                ) is ConcurrencyPolicy.PARALLEL_SAFE:
                    end += 1
            wave_started = time.perf_counter()
            async with asyncio.TaskGroup() as group:
                for item_index in range(index, end):
                    group.create_task(run(item_index, policy))
            wave = {
                "wave_id": f"{batch_id}:{len(waves)}",
                "policy": policy.value,
                "size": end - index,
                "duration_ms": int((time.perf_counter() - wave_started) * 1000),
                "peak_concurrency": peak,
            }
            waves.append(wave)
            if on_wave is not None:
                wave_results = [
                    (item_index, results[item_index])
                    for item_index in range(index, end)
                    if results[item_index] is not None
                ]
                if not await on_wave(wave_results, wave):
                    break
            index = end
        self.last_batch_metrics = {
            "batch_id": batch_id,
            "concurrency_limit": self.max_tool_concurrency,
            "peak_concurrency": peak,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "waves": waves,
        }
        return [result for result in results if result is not None]
