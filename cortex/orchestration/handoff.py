from dataclasses import dataclass

from .agent_spec import AgentSpec
from .task import Task


@dataclass(frozen=True)
class Handoff:
    task: Task
    source: AgentSpec
    target: AgentSpec
    reason: str
