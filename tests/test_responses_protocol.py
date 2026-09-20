from types import SimpleNamespace

from agent.loop_new import AgentLoop
from src.core.models import OpenAIResponsesClient
from src.tools.executor import ToolExecutor
from src.tools.file_tools import ReadNoteTool
from src.tools.permission import Permission
from src.tools.registry import ToolRegistry


class FakeResponsesResource:
    def __init__(self):
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return SimpleNamespace(
                id="resp_1",
                output_text="",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        call_id="call_1",
                        name="read_note",
                        arguments='{"filename": "python.md"}',
                    )
                ],
            )

        return SimpleNamespace(
            id="resp_2",
            output_text="The note explains Python basics.",
            output=[SimpleNamespace(type="message")],
        )


def build_runtime():
    resource = FakeResponsesResource()
    sdk = SimpleNamespace(responses=resource)
    llm = OpenAIResponsesClient(
        api_key="test-key",
        model="test-model",
        client=sdk,
    )

    registry = ToolRegistry()
    registry.register(ReadNoteTool())
    executor = ToolExecutor({Permission.READ}, registry)
    return AgentLoop(llm, executor, max_steps=2), resource, executor


def test_executor_emits_native_strict_responses_schema():
    _, _, executor = build_runtime()

    schema = executor.list_tool_schemas()[0]

    assert schema["type"] == "function"
    assert schema["name"] == "read_note"
    assert "function" not in schema
    assert schema["strict"] is True
    assert schema["parameters"]["additionalProperties"] is False


def test_responses_tool_loop_uses_call_id_and_previous_response_id():
    agent, resource, _ = build_runtime()

    answer = agent.run("Read python.md and summarize it.")

    assert answer == "The note explains Python basics."
    assert len(resource.requests) == 2

    first, second = resource.requests
    assert first["parallel_tool_calls"] is False
    assert first["tools"][0]["name"] == "read_note"
    assert second["previous_response_id"] == "resp_1"
    assert second["input"][0]["type"] == "function_call_output"
    assert second["input"][0]["call_id"] == "call_1"


def test_invalid_function_arguments_are_rejected():
    response = SimpleNamespace(
        id="resp_bad",
        output_text="",
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="call_bad",
                name="read_note",
                arguments="not-json",
            )
        ],
    )

    try:
        OpenAIResponsesClient._parse_response(response)
    except ValueError as exc:
        assert "invalid JSON arguments" in str(exc)
    else:
        raise AssertionError("invalid arguments must raise ValueError")
