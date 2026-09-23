"""Execute small task sets and retain raw data for later metric design."""

from dataclasses import asdict, dataclass, field
from typing import Callable

from src.ops.tracer import RunRecorder

from .grader import DeterministicGrader, GradeResult, Grader
from .tasks import Task


@dataclass
class EvalRun:
    task_id: str
    run_id: str
    answer: str | None
    success: bool
    actions: list[str]
    steps: int
    total_tokens: int
    latency_ms: float
    llm_latency_ms: float
    tool_duration_ms: float
    action_events: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    reflections: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    grade: GradeResult | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class EvalRunner:
    def __init__(
        self,
        agent_factory: Callable[[RunRecorder], object],
        grader: Grader | None = None,
    ):
        self.agent_factory = agent_factory
        self.grader = grader or DeterministicGrader()

    def run(self, tasks: list[Task]) -> list[EvalRun]:
        results = []
        for task in tasks:
            recorder = RunRecorder(persist=False)
            agent = self.agent_factory(recorder)
            answer = agent.run(task.input)
            summary = recorder.runs[-1]
            events = [
                event for event in recorder.events if event.run_id == summary.run_id
            ]
            action_events = [e.data for e in events if e.event_type == "action"]
            result = EvalRun(
                task_id=task.task_id,
                run_id=summary.run_id,
                answer=answer,
                success=bool(summary.success),
                actions=[event["tool_name"] for event in action_events],
                action_events=action_events,
                steps=summary.steps,
                total_tokens=summary.total_tokens,
                latency_ms=summary.latency_ms,
                llm_latency_ms=summary.llm_latency_ms,
                tool_duration_ms=summary.tool_duration_ms,
                observations=[e.data for e in events if e.event_type == "observation"],
                reflections=[e.data for e in events if e.event_type == "reflection"],
                llm_calls=[e.data for e in events if e.event_type == "llm_call"],
            )
            result.grade = self.grader.grade(task, result)
            results.append(result)
        return results
