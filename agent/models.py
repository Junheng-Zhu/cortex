from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    response_id: str | None = None
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    output_items: list[dict[str, Any]] = field(default_factory=list)
