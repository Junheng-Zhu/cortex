from dataclasses import dataclass, field


@dataclass
class Observation:

    action_id: str
    success: bool
    output: any
    error: str | None
    latency: float
    token_cost: float
    tool_name: str | None
