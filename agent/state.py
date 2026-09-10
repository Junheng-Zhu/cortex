from dataclasses import dataclass, field

@dataclass
class AgentState:
    messages: list = field(default_factory=list)
    thoughts: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    final_answer: str | None = None
