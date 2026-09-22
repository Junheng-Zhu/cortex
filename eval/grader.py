"""Deterministic grading primitives; task success is not runtime success."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .tasks import Task


@dataclass(frozen=True)
class GradeResult:
    task_success: bool
    expected_tools_matched: bool
    reason: str


class Grader(Protocol):
    def grade(self, task: Task, run: "EvalRun") -> GradeResult: ...


class DeterministicGrader:
    """Grade only facts declared by a Task, without an LLM judge."""

    def grade(self, task: Task, run: "EvalRun") -> GradeResult:
        matched = run.actions[: len(task.expected_tools)] == task.expected_tools
        # Deliberately separate from run.success: runtime health alone does not
        # prove that the task requirements were satisfied.
        task_success = matched and run.success == task.expected_success
        reason = (
            "expectations matched"
            if task_success
            else (f"tools={run.actions!r}, run_success={run.success!r}")
        )
        return GradeResult(task_success, matched, reason)


if TYPE_CHECKING:
    from .runner import EvalRun
