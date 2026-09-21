import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import dotenv
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.models import LLMResponse, LLMUsage, ToolCall


dotenv.load_dotenv()


class ContextMode(str, Enum):
    SERVER_MANAGED = "SERVER_MANAGED"
    CLIENT_MANAGED = "CLIENT_MANAGED"


@dataclass(frozen=True)
class ProviderCapabilities:
    context_mode: ContextMode = ContextMode.SERVER_MANAGED


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return {
        key: value
        for key in ("type", "id", "call_id", "name", "arguments", "content")
        if (value := getattr(item, key, None)) is not None
    }


def parse_responses_response(response: Any) -> LLMResponse:
    """Normalize an OpenAI Responses-style object into the agent protocol."""
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
            tool_calls.append(
                ToolCall(
                    call_id=_value(raw, "call_id", _value(raw, "id", "")),
                    name=_value(raw, "name", ""),
                    arguments=arguments,
                )
            )
        elif item_type == "message":
            for content in _value(raw, "content", []) or []:
                if _value(content, "type") in {"output_text", "text"}:
                    text_parts.append(_value(content, "text", ""))

    output_text = _value(response, "output_text", "") or ""
    if output_text and not text_parts:
        text_parts.append(output_text)

    raw_usage = _value(response, "usage", {}) or {}
    input_tokens = _value(raw_usage, "input_tokens", 0) or 0
    output_tokens = _value(raw_usage, "output_tokens", 0) or 0
    total_tokens = (
        _value(raw_usage, "total_tokens", input_tokens + output_tokens)
        or input_tokens + output_tokens
    )
    return LLMResponse(
        response_id=_value(response, "id"),
        content="".join(text_parts),
        tool_calls=tool_calls,
        usage=LLMUsage(input_tokens, output_tokens, total_tokens),
        output_items=output_items,
    )


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        capabilities: ProviderCapabilities | None = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        self.model = model or os.getenv("MODEL_NAME", "gpt-4o-mini")
        self.temperature = temperature
        self.capabilities = capabilities or ProviderCapabilities()
        if not self.api_key:
            raise ValueError("OpenAI API Key 未设置，请检查 .env 文件")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def respond(
        self,
        input: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
    ) -> LLMResponse:
        request = {"model": self.model, "input": input, "tools": tools}
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        return parse_responses_response(self.client.responses.create(**request))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def chat_completion(self, messages, temperature=None, model=None, **kwargs) -> str:
        response = self.client.chat.completions.create(
            model=model or self.model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            **kwargs,
        )
        return response.choices[0].message.content
