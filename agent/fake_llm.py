
from agent.models import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        last = messages[-1]
        if "读取" in last.get("content", ""):
            return {
                "type": "tool_call",
                "name": "read_note",
                "arguments": {"filename": "python.md"},
            }
        return {"type": "final", "content": "任务完成"}
