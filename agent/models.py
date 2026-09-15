from dataclasses import dataclass, field

@dataclass
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict

@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] | None = None

    def __init__(self,response:dict):
        self.content = response.get("content", "")
        self.tool_calls = response.get("tool_calls", None)
        

    def is_tool_call(self) -> bool:
        return len(self.tool_calls) > 0 

