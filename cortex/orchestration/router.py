from .agent_spec import AgentSpec
from .task import Task


class Router:
    def route(self, task: Task, agents: list[AgentSpec]) -> AgentSpec:
        if not agents:
            raise ValueError("at least one agent is required")
        return agents[0]
