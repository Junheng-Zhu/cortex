
from agent.models import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def create_response(
        self,
        input_items,
        tools,
        previous_response_id=None,
        instructions=None,
    ):
        self.calls += 1
        if self.calls == 1 and tools:
            return LLMResponse(
                response_id="resp_1",
                tool_calls=[
                    ToolCall(
                        call_id="call_1",
                        name="read_note",
                        arguments={"filename": "python.md"},
                    )
                ],
            )
        return LLMResponse(response_id=f"resp_{self.calls}", content="任务完成")
