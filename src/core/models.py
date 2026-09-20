import json
import os
from typing import Any

import dotenv
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.models import LLMResponse, ToolCall


dotenv.load_dotenv()


RETRYABLE_OPENAI_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)


class OpenAIResponsesClient:
    """Stateless OpenAI Responses API client for the Cortex runtime."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        store: bool = True,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        )
        self.model = model or os.getenv("MODEL_NAME", "gpt-4.1-mini")
        self.max_output_tokens = max_output_tokens
        self.store = store

        if client is not None:
            self.client = client
            return

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not configured")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(RETRYABLE_OPENAI_ERRORS),
        reraise=True,
    )
    def create_response(
        self,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        instructions: str | None = None,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            # Copy the turn delta because AgentState clears its pending input
            # after the request completes.
            "input": list(input_items),
            "store": self.store,
        }

        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        if instructions:
            request["instructions"] = instructions
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if tools:
            request.update(
                {
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )

        response = self.client.responses.create(**request)
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        tool_calls: list[ToolCall] = []

        for item in response.output:
            if item.type != "function_call":
                continue

            try:
                arguments = json.loads(item.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Model returned invalid JSON arguments for {item.name}: "
                    f"{item.arguments}"
                ) from exc

            if not isinstance(arguments, dict):
                raise ValueError(
                    f"Model arguments for {item.name} must decode to an object"
                )

            tool_calls.append(
                ToolCall(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=arguments,
                )
            )

        return LLMResponse(
            response_id=response.id,
            content=response.output_text or "",
            tool_calls=tool_calls,
        )


LLMClient = OpenAIResponsesClient
