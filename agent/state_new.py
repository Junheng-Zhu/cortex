from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.action import Action
from runtime.observation import Observation
from runtime.reflection import ReflectionResult


class AgentPhase(str, Enum):
    DECIDE = "DECIDE"
    ACT = "ACT"
    OBSERVE = "OBSERVE"
    REFLECT = "REFLECT"
    FINAL = "FINAL"


Phase = AgentPhase


@dataclass
class AgentState:
    phase: AgentPhase = AgentPhase.DECIDE
    pending_input: list[dict[str, Any]] = field(default_factory=list)
    context_history: list[dict[str, Any]] = field(default_factory=list)
    previous_response_id: str | None = None
    # Temporary compatibility surface for callers still constructing messages.
    messages: list[dict[str, Any]] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    reflections: list[ReflectionResult] = field(default_factory=list)
    pending_actions: list[Action] = field(default_factory=list)
    last_tool_result: Any = None
    final_answer: str | None = None
    step_count: int = 0
    max_steps: int = 10
    token_budget: int = 4096
