from dataclasses import dataclass,field
from enum import Enum
from typing import Any
from src.tools.result import ToolResult

class AgentPhase(Enum):
    "PLAN"="PLAN"
    "EXECUTE"="EXECUTE"
    "OBSERVE"="OBSERVE"
    "FINAL"="FINAL"
    

@dataclass
class AgentState:
    messages: list[dict] = field(default_factory=list)
    phase: AgentPhase = AgentPhase.PLAN
    plan: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    observations: list[Any] = field(default_factory=list)
    step_count: int = 0
    max_steps: int = 6
    last_tool_result:ToolResult

    

