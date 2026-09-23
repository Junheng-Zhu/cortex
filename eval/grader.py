"""Deterministic task grading, separate from tool and runtime success."""

from dataclasses import dataclass, field
from collections import Counter
from typing import TYPE_CHECKING, Protocol

from .tasks import Task


@dataclass(frozen=True)
class GradeResult:
    task_success: bool
    goal_met: bool
    run_success: bool
    required_tools_matched: bool
    tool_sequence_matched: bool | None
    forbidden_tool_used: bool
    expected_outcome_matched: bool
    expected_error_matched: bool
    reason: str
    failed_criteria: list[str] = field(default_factory=list)
    unnecessary_tool_calls: int = 0


class Grader(Protocol):
    def grade(self, task: Task, run: "EvalRun") -> GradeResult: ...


class DeterministicGrader:
    """Evaluate declared goals; exact sequence remains diagnostic only."""

    def grade(self, task: Task, run: "EvalRun") -> GradeResult:
        required = Counter(task.required_tools)
        actual = Counter(run.actions)
        required_matched = all(
            actual[tool] >= count for tool, count in required.items()
        )
        forbidden_used = any(tool in task.forbidden_tools for tool in run.actions)
        sequence_matched = (
            None
            if task.expected_tool_sequence is None
            else run.actions == task.expected_tool_sequence
        )
        outcome_matched = run.outcome == task.expected_outcome
        error_matched = (
            task.expected_error_type is None
            or task.expected_error_type in run.error_types
        )

        failed = []
        if not required_matched:
            failed.append("required_tools")
        if forbidden_used:
            failed.append("forbidden_tools")
        if not outcome_matched:
            failed.append("expected_outcome")
        if not error_matched:
            failed.append("expected_error")

        expected_sequence = task.expected_tool_sequence or task.required_tools
        unnecessary = max(0, len(run.actions) - len(expected_sequence))
        goal_met = not failed
        reason = "goal met"
        if failed:
            reason = ", ".join(failed) + " did not match"
        elif sequence_matched is False:
            reason = f"goal met with {unnecessary} unnecessary tool call(s)"

        return GradeResult(
            task_success=goal_met,
            goal_met=goal_met,
            run_success=run.run_success,
            required_tools_matched=required_matched,
            tool_sequence_matched=sequence_matched,
            forbidden_tool_used=forbidden_used,
            expected_outcome_matched=outcome_matched,
            expected_error_matched=error_matched,
            reason=reason,
            failed_criteria=failed,
            unnecessary_tool_calls=unnecessary,
        )


if TYPE_CHECKING:
    from .runner import EvalRun
