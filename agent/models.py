from dataclasses import dataclass, field
import json
from typing import Optional

@dataclass
class ToolCall:
    name: str
    arguments: dict
    tool_call_id:str | None = None
    

@dataclass
class LLMResponse:
    content: Optional[str] = None
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

