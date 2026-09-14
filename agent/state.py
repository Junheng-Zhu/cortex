from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentPhase(str, Enum):
    PLAN = "plan"
    DECIDE = "decide"
    ACT = "act"
    REFLECT = "reflect"

    FINAL = "final"
    FAILED = "failed"


@dataclass
class AgentState:

    # LLM conversation state
    messages: list[dict] = field(default_factory=list)

    # Agent runtime state
    phase: AgentPhase = AgentPhase.PLAN

    # Planning
    plan: list[str] = field(default_factory=list)

    # Execution history
    actions: list[dict] = field(default_factory=list)
    observations: list[Any] = field(default_factory=list)

    # Current pending action
    pending_tool_name: str | None = None
    pending_tool_arguments: dict | None = None

    # Latest execution result
    last_tool_result: Any = None

    # Runtime budget
    step_count: int = 0
    max_steps: int = 6

    # Terminal state
    final_answer: str | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.phase in {
            AgentPhase.FINAL,
            AgentPhase.FAILED,
        }
