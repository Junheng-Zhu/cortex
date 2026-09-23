from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSpec:
    name: str
    instructions: str
    tools: tuple[str, ...] = field(default_factory=tuple)
