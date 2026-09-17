from dataclasses import dataclass,field
from enum import Enum
from typing import Any
from src.tools.result import ToolResult
from .models import ToolCall

class AgentPhase(Enum):
    "DECIDE"="DECIDE"
    "EXECUTE"="EXECUTE"
    "OBSERVE"="OBSERVE"
    "FINAL"="FINAL"

@dataclass
class AgentState:
    messages: list[dict] = field(default_factory=list)
    phase: AgentPhase = AgentPhase.DECIDE
    # 当前任务
    goal: str | None = None
    # action历史
    actions: list[dict] = field(default_factory=list)
    # observation历史
    observations: list[Any] = field(default_factory=list)
    # 思考结果
    reflections: list[str] = field(default_factory=list)
    step_count: int = 0
    max_steps: int = 6
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    last_tool_result:ToolResult | None = None
    final_response: str | None = None
    # token预算
    token_budget:int
    # trace
    execution_trace: list[ToolResult]=field(default_factory=list)
    #上下文
    context:list[dict]=field(default_factort=list)
