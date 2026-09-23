"""Aggregate the six initial Cortex evaluation metrics."""

from dataclasses import asdict, dataclass
from math import ceil

from .runner import EvalRun


@dataclass(frozen=True)
class EvalMetrics:
    task_success_rate: float
    run_success_rate: float
    tool_selection_accuracy: float
    argument_valid_rate: float
    average_steps: float
    p50_latency_ms: float
    p95_latency_ms: float
    tokens_per_successful_task: float
    task_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _percentile(values: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile, suitable for small baseline sets."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def aggregate_metrics(runs: list[EvalRun]) -> EvalMetrics:
    """Compute metrics from raw EvalRun records without an LLM judge."""
    if not runs:
        return EvalMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    successful = [run for run in runs if run.grade and run.grade.task_success]
    tool_matches = [
        run.grade.required_tools_matched and not run.grade.forbidden_tool_used
        for run in runs
        if run.grade is not None
    ]
    validation_results = [
        event["validation_passed"]
        for run in runs
        for event in run.action_events
        if event.get("validation_passed") is not None
    ]
    latencies = [run.latency_ms for run in runs]

    return EvalMetrics(
        task_success_rate=len(successful) / len(runs),
        run_success_rate=sum(run.run_success for run in runs) / len(runs),
        tool_selection_accuracy=(
            sum(tool_matches) / len(tool_matches) if tool_matches else 0.0
        ),
        argument_valid_rate=(
            sum(validation_results) / len(validation_results)
            if validation_results
            else 0.0
        ),
        average_steps=sum(run.steps for run in runs) / len(runs),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        tokens_per_successful_task=(
            sum(run.total_tokens for run in successful) / len(successful)
            if successful
            else 0.0
        ),
        task_count=len(runs),
    )
