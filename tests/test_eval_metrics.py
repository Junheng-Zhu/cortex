import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from eval.grader import GradeResult
from eval.metrics import aggregate_metrics
from eval.runner import EvalRun


def make_run(
    index: int,
    *,
    task_success: bool,
    tools_matched: bool,
    validation_passed: bool | None,
    steps: int,
    latency_ms: float,
    tokens: int,
) -> EvalRun:
    action_events = []
    if validation_passed is not None:
        action_events.append({"validation_passed": validation_passed})
    return EvalRun(
        task_id=f"task-{index}",
        run_id=f"run-{index}",
        answer="done",
        success=True,
        actions=["tool"] if action_events else [],
        steps=steps,
        total_tokens=tokens,
        latency_ms=latency_ms,
        llm_latency_ms=latency_ms / 2,
        tool_duration_ms=1,
        action_events=action_events,
        grade=GradeResult(task_success, tools_matched, "fixture"),
    )


def test_aggregate_six_eval_metrics():
    runs = [
        make_run(
            1,
            task_success=True,
            tools_matched=True,
            validation_passed=True,
            steps=1,
            latency_ms=10,
            tokens=30,
        ),
        make_run(
            2,
            task_success=True,
            tools_matched=False,
            validation_passed=False,
            steps=2,
            latency_ms=20,
            tokens=50,
        ),
        make_run(
            3,
            task_success=False,
            tools_matched=True,
            validation_passed=None,
            steps=0,
            latency_ms=100,
            tokens=70,
        ),
    ]

    metrics = aggregate_metrics(runs)

    assert metrics.task_success_rate == 2 / 3
    assert metrics.tool_selection_accuracy == 2 / 3
    assert metrics.argument_valid_rate == 1 / 2
    assert metrics.average_steps == 1
    assert metrics.p50_latency_ms == 20
    assert metrics.p95_latency_ms == 100
    assert metrics.tokens_per_successful_task == 40


def test_empty_eval_metrics_are_zero():
    metrics = aggregate_metrics([])
    assert all(value == 0 for value in metrics.to_dict().values())
