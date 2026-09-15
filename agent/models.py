from dataclasses import dataclass, field
import json

@dataclass
class ToolCall:
    tool_call_id: str=""
    name: str
    arguments: dict

@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def from_dict(self, response: dict):
        self.content = response.get("content", "")

        for item in response.get("tool_calls", []):
            tool_call = ToolCall(
                name=item["name"],
                arguments=json.loads(item["arguments"]) 
            )
            self.tool_calls.append(tool_call)
        

    def is_tool_call(self) -> bool:
        return len(self.tool_calls) > 0 

