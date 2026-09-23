"""Execute tasks and retain separate tool, run, and task result layers."""

from dataclasses import asdict, dataclass, field
import time
from typing import Callable

from cortex.observability.tracer import RunRecorder

from .grader import DeterministicGrader, GradeResult, Grader
from .tasks import Task


@dataclass
class EvalRun:
    task_id: str
    run_id: str
    answer: str | None
    run_success: bool
    outcome: str
    actions: list[str]
    steps: int
    total_tokens: int
    latency_ms: float
    llm_latency_ms: float
    tool_duration_ms: float
    termination_reason: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    error_types: list[str] = field(default_factory=list)
    action_events: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    reflections: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    grade: GradeResult | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _derive_outcome(action_events: list[dict]) -> str:
    errors = [event.get("error_type") for event in action_events]
    if "ToolPermissionError" in errors:
        return "PERMISSION_DENIED"
    if "ToolTimeoutError" in errors:
        return "TIMEOUT"

    failed_indexes = [
        index
        for index, event in enumerate(action_events)
        if event.get("status") == "FAILED"
    ]
    if failed_indexes:
        last_failure = failed_indexes[-1]
        if any(
            event.get("status") == "SUCCEEDED"
            for event in action_events[last_failure + 1 :]
        ):
            return "RECOVERED"
        return "BLOCKED"
    return "COMPLETED"


class EvalRunner:
    def __init__(
        self,
        agent_factory: Callable[[RunRecorder, Task], object],
        grader: Grader | None = None,
    ):
        self.agent_factory = agent_factory
        self.grader = grader or DeterministicGrader()

    def run(self, tasks: list[Task]) -> list[EvalRun]:
        results = []
        for task in tasks:
            recorder = RunRecorder(persist=False)
            answer = None
            runtime_error = None
            started = time.perf_counter()
            try:
                agent = self.agent_factory(recorder, task)
                answer = agent.run(task.input)
            except Exception as exc:  # retained in the eval report, not hidden
                runtime_error = exc

            summary = recorder.current_run
            elapsed_ms = (time.perf_counter() - started) * 1000
            run_id = summary.run_id if summary else recorder.run_id
            events = [event for event in recorder.events if event.run_id == run_id]
            action_events = [e.data for e in events if e.event_type == "action"]
            observations = [e.data for e in events if e.event_type == "observation"]
            error_types = [
                event["error_type"]
                for event in action_events
                if event.get("error_type")
            ]
            result = EvalRun(
                task_id=task.task_id,
                run_id=run_id,
                answer=answer,
                run_success=runtime_error is None,
                outcome=(
                    "RUNTIME_ERROR" if runtime_error else _derive_outcome(action_events)
                ),
                actions=[event["tool_name"] for event in action_events],
                steps=summary.steps if summary else len(action_events),
                total_tokens=summary.total_tokens if summary else 0,
                latency_ms=(
                    summary.latency_ms
                    if summary and summary.finished_at is not None
                    else elapsed_ms
                ),
                llm_latency_ms=summary.llm_latency_ms if summary else 0,
                tool_duration_ms=summary.tool_duration_ms if summary else 0,
                termination_reason=summary.termination_reason if summary else None,
                error_type=type(runtime_error).__name__ if runtime_error else None,
                error_message=str(runtime_error) if runtime_error else None,
                error_types=error_types,
                action_events=action_events,
                observations=observations,
                reflections=[e.data for e in events if e.event_type == "reflection"],
                llm_calls=[e.data for e in events if e.event_type == "llm_call"],
            )
            result.grade = self.grader.grade(task, result)
            results.append(result)
        return results
