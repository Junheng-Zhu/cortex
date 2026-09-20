from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One function call emitted by the Responses API."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """Structured model result consumed by the Cortex runtime."""

    response_id: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def is_tool_call(self) -> bool:
        return bool(self.tool_calls)
