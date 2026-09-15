from dataclasses import dataclass,field
from enum import Enum
from typing import Any
from src.tools.result import ToolResult

class AgentPhase(Enum):
    "DECIDE"="DECIDE"
    "EXECUTE"="EXECUTE"
    "OBSERVE"="OBSERVE"
    "FINAL"="FINAL"
    

@dataclass
class AgentState:
    messages: list[dict] = field(default_factory=list)
    phase: AgentPhase = AgentPhase.DECIDE
    actions: list[dict] = field(default_factory=list)
    observations: list[Any] = field(default_factory=list)
    step_count: int = 0
    max_steps: int = 6
    pending_tool_name: str | None = None
    pending_tool_arguments: dict | None = None
    last_tool_result:ToolResult | None = None
    final_response: str | None = None

    

