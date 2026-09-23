import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from eval.grader import GradeResult
from eval.metrics import aggregate_metrics
from eval.runner import EvalRun
from eval.grader import DeterministicGrader
from eval.tasks import Task


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
        run_success=True,
        outcome="COMPLETED",
        actions=["tool"] if action_events else [],
        steps=steps,
        total_tokens=tokens,
        latency_ms=latency_ms,
        llm_latency_ms=latency_ms / 2,
        tool_duration_ms=1,
        action_events=action_events,
        grade=GradeResult(
            task_success=task_success,
            goal_met=task_success,
            run_success=True,
            required_tools_matched=tools_matched,
            tool_sequence_matched=tools_matched,
            forbidden_tool_used=False,
            expected_outcome_matched=True,
            expected_error_matched=True,
            reason="fixture",
        ),
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
    assert metrics.run_success_rate == 1
    assert metrics.tool_selection_accuracy == 2 / 3
    assert metrics.argument_valid_rate == 1 / 2
    assert metrics.average_steps == 1
    assert metrics.p50_latency_ms == 20
    assert metrics.p95_latency_ms == 100
    assert metrics.tokens_per_successful_task == 40


def test_empty_eval_metrics_are_zero():
    metrics = aggregate_metrics([])
    assert all(value == 0 for value in metrics.to_dict().values())


def test_tool_sequence_is_diagnostic_not_task_success():
    run = make_run(
        1,
        task_success=True,
        tools_matched=True,
        validation_passed=True,
        steps=2,
        latency_ms=10,
        tokens=10,
    )
    run.actions = ["read_note", "list_notes"]
    task = Task(
        "known",
        "read calendar",
        required_tools=["read_note"],
        expected_tool_sequence=["read_note"],
    )

    grade = DeterministicGrader().grade(task, run)

    assert grade.task_success is True
    assert grade.required_tools_matched is True
    assert grade.tool_sequence_matched is False
    assert grade.unnecessary_tool_calls == 1


def test_expected_sandbox_block_is_success_despite_failed_tool():
    run = make_run(
        2,
        task_success=True,
        tools_matched=True,
        validation_passed=True,
        steps=1,
        latency_ms=10,
        tokens=10,
    )
    run.actions = ["read_note"]
    run.outcome = "BLOCKED"
    run.error_types = ["ToolSandboxError"]
    task = Task(
        "sandbox",
        "read ../.gitignore",
        required_tools=["read_note"],
        forbidden_tools=["shell"],
        expected_outcome="BLOCKED",
        expected_error_type="ToolSandboxError",
    )

    grade = DeterministicGrader().grade(task, run)

    assert run.run_success is True
    assert grade.task_success is True
    assert grade.expected_outcome_matched is True
    assert grade.expected_error_matched is True
