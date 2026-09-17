from dataclasses import dataclass
from agent.state_new import AgentState
from .observation import Observation


@dataclass
class ReflectionResult:

    success: bool

    issue: str

    next_strategy: str

    need_retry: bool


class Reflection:

    def analyze(state: AgentState, observation: Observation):
        return ReflectionResult
