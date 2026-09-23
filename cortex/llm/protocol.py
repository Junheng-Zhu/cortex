import json
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


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return {key: value for key in ("type", "id", "call_id", "name", "arguments", "content") if (value := getattr(item, key, None)) is not None}


def parse_responses_response(response: Any) -> LLMResponse:
    """Normalize an OpenAI Responses-style object into the runtime protocol."""
    raw_output = list(_value(response, "output", []) or [])
    output_items = [_as_dict(item) for item in raw_output]
    tool_calls: list[ToolCall] = []
    text_parts: list[str] = []
    for raw, item in zip(raw_output, output_items):
        item_type = _value(raw, "type", item.get("type"))
        if item_type == "function_call":
            arguments = _value(raw, "arguments", {}) or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            tool_calls.append(ToolCall(_value(raw, "call_id", _value(raw, "id", "")), _value(raw, "name", ""), arguments))
        elif item_type == "message":
            for content in _value(raw, "content", []) or []:
                if _value(content, "type") in {"output_text", "text"}:
                    text_parts.append(_value(content, "text", ""))
    output_text = _value(response, "output_text", "") or ""
    if output_text and not text_parts:
        text_parts.append(output_text)
    usage = _value(response, "usage", {}) or {}
    input_tokens = _value(usage, "input_tokens", 0) or 0
    output_tokens = _value(usage, "output_tokens", 0) or 0
    total_tokens = _value(usage, "total_tokens", input_tokens + output_tokens) or input_tokens + output_tokens
    return LLMResponse(_value(response, "id"), "".join(text_parts), tool_calls, LLMUsage(input_tokens, output_tokens, total_tokens), output_items)
