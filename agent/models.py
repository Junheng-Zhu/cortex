from dataclasses import dataclass, field

@dataclass
class LLMResponse:
    content: str
    tool_name: str | None = None
    tool_arguments: dict | None = None

    def __init__(self,response:dict):
        self.content = response.get("content", "")
        self.tool_name = response.get("name")
        self.tool_arguments = response.get("arguments")

    def is_tool_call(self) -> bool:
        return self.tool_name is not None