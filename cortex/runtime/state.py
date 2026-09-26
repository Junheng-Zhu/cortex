from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .action import Action
from .observation import Observation
from .recovery import ReflectionResult


class AgentPhase(str, Enum):
    DECIDE = "DECIDE"
    ACT = "ACT"
    OBSERVE = "OBSERVE"
    REFLECT = "REFLECT"
    FINAL = "FINAL"


Phase = AgentPhase


@dataclass
class AgentState:
    run_id: str = ""
    session_id: str = ""
    phase: AgentPhase = AgentPhase.DECIDE
    pending_input: list[dict[str, Any]] = field(default_factory=list)
    context_history: list[dict[str, Any]] = field(default_factory=list)
    # A read-only snapshot for diagnostics/backwards compatibility. Session owns
    # and updates the actual provider conversation cursor.
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
    current_goal: str | None = None
    current_plan: list[str] = field(default_factory=list)
    important_decisions: list[str] = field(default_factory=list)
    artifact_references: list[str] = field(default_factory=list)
    compact_summary: str = ""
    skill_candidates: list[dict[str, Any]] = field(default_factory=list)
    # File reads and model disclosure are intentionally separate states.
    # selected -> loaded -> pending disclosure -> accepted by a successful
    # provider request -> resident in the current client working set.
    skill_versions: dict[str, str] = field(default_factory=dict)
    skill_bodies: dict[str, str] = field(default_factory=dict, repr=False)
    skill_pending_disclosures: set[str] = field(default_factory=set)
    skill_accepted_disclosures: set[str] = field(default_factory=set)
    skill_resident: set[str] = field(default_factory=set)
    skill_disclosed: set[str] = field(default_factory=set)  # V1 compatibility only
    skill_request_disclosures: set[str] = field(default_factory=set, repr=False)
    skill_candidates_accepted: bool = False
    skill_searches: int = 0
    skill_search_limit: int = 1
    skill_local_queries: int = 0
    skill_extra_llm_requests: int = 0
    skill_supplemental_pending: bool = False
