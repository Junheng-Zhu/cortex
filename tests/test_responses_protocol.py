import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.loop_new import AgentLoop
from agent.models import LLMResponse, LLMUsage, ToolCall
from src.core.models import (
    ContextMode,
    ProviderCapabilities,
    parse_responses_response,
)
from src.tools.result import ToolResult


class Executor:
    def list_tool_schemas(self):
        return [{"type": "function", "name": "echo"}]

    def execute(self, name, arguments):
        return ToolResult(name, 1, 0, True, None, None, arguments["text"])


class ResponsesLLM:
    def __init__(self, context_mode):
        self.capabilities = ProviderCapabilities(context_mode)
        self.requests = []

    def respond(self, **request):
        self.requests.append(request)
        if len(self.requests) == 1:
            item = {
                "type": "function_call",
                "call_id": "call-1",
                "name": "echo",
                "arguments": '{"text": "hello"}',
            }
            return LLMResponse(
                response_id="resp-1",
                tool_calls=[ToolCall("call-1", "echo", {"text": "hello"})],
                output_items=[item],
            )
        return LLMResponse(
            response_id="resp-2",
            content="done",
            output_items=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ],
        )


def test_response_parser_preserves_usage_and_output_items():
    raw = {
        "id": "resp-123",
        "output": [
            {
                "type": "function_call",
                "call_id": "call-123",
                "name": "echo",
                "arguments": '{"text": "hi"}',
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }

    parsed = parse_responses_response(raw)

    assert parsed.response_id == "resp-123"
    assert parsed.usage == LLMUsage(10, 4, 14)
    assert parsed.output_items == raw["output"]
    assert parsed.tool_calls == [ToolCall("call-123", "echo", {"text": "hi"})]


def test_server_managed_context_uses_openai_style_cursor():
    llm = ResponsesLLM(ContextMode.SERVER_MANAGED)

    assert AgentLoop(llm, Executor()).run("echo hello") == "done"

    assert llm.requests[0]["previous_response_id"] is None
    assert llm.requests[1]["previous_response_id"] == "resp-1"
    assert llm.requests[1]["input"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": "hello"}
    ]


def test_client_managed_context_replays_full_history_without_cursor():
    llm = ResponsesLLM(ContextMode.CLIENT_MANAGED)

    assert AgentLoop(llm, Executor()).run("echo hello") == "done"

    assert llm.requests[1]["previous_response_id"] is None
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "echo hello"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "echo",
            "arguments": '{"text": "hello"}',
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "hello"},
    ]
