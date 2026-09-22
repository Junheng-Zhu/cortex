from .registry import ToolRegistry
from typing import Any
from .exceptions import *
from .base import Tool
from .result import ToolResult
from .execution_outcome import ExecutionOutcome
from copy import deepcopy
import time
from pydantic import ValidationError
import multiprocessing


class ToolExecutor:
    def __init__(self, allowed_permissions: set, registry: ToolRegistry):
        self.registry = registry
        self.allowed_permissions = allowed_permissions
        self.timeout = 10
        self.max_retries = 5
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
                return ToolResult(
                    tool_name=tool.name,
                    attempts=attempts,
                    duration_ms=0,
                    success=True,
                    error_type=None,
                    error_message=None,
                    data=outcome.data,
                )
            else:
                if self._should_retry(outcome, tool, attempts):
                    continue
                else:
                    return ToolResult(
                        tool_name=tool.name,
                        attempts=attempts,
                        duration_ms=0,
                        success=False,
                        error_type=outcome.error_type,
                        error_message=outcome.error_message,
                        data=None,
                    )

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
            )

        tool_result = self._execute_with_retry(tool, validated_input)
        end = time.perf_counter()
        tool_result.duration_ms = int((end - start) * 1000)
        return tool_result
