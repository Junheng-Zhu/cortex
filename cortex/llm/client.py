import os
from typing import Any

import dotenv
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .capabilities import ProviderCapabilities
from .protocol import LLMResponse, parse_responses_response


dotenv.load_dotenv()


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, temperature: float = 0.7, capabilities: ProviderCapabilities | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("MODEL_NAME", "gpt-4o-mini")
        self.temperature = temperature
        self.capabilities = capabilities or ProviderCapabilities()
        if not self.api_key:
            raise ValueError("OpenAI API Key 未设置，请检查 .env 文件")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def respond(self, input: list[dict[str, Any]], tools: list[dict[str, Any]], previous_response_id: str | None = None) -> LLMResponse:
        response_tools = [{"type": tool["type"], **tool["function"]} if "function" in tool else tool for tool in tools]
        request: dict[str, Any] = {"model": self.model, "input": input, "tools": response_tools}
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        return parse_responses_response(self.client.responses.create(**request))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(Exception), reraise=True)
    def chat_completion(self, messages, temperature=None, model=None, **kwargs) -> str:
        response = self.client.chat.completions.create(model=model or self.model, messages=messages, temperature=self.temperature if temperature is None else temperature, **kwargs)
        return response.choices[0].message.content
