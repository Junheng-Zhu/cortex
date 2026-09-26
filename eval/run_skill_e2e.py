#!/usr/bin/env python3
"""Run fixed real-model no/preloaded/automatic Skill cohorts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cortex.app.bootstrap import build_agent
from cortex.llm.client import LLMClient
from cortex.runtime.session import Session, SessionConfig
from cortex.tools.permission import Permission
from eval.grader import GradeResult
from eval.runner import EvalRunner
from eval.tasks import Task


def load_tasks(path: Path):
    values = json.loads(path.read_text(encoding="utf-8"))
    revision = values.get("revision")
    if not revision:
        raise ValueError("task revision is required")
    return revision, [(Task(task_id=item["task_id"], input=item["input"]), item.get("skill_id"), item["expected_contains"]) for item in values["tasks"]]


class AnswerGrader:
    def __init__(self, expected): self.expected = expected
    def grade(self, task, run):
        success = self.expected[task.task_id].casefold() in (run.answer or "").casefold()
        return GradeResult(success, success, run.run_success, True, None, False, run.outcome == "COMPLETED", True, "expected answer present" if success else "expected answer missing", [] if success else ["expected_contains"])


def run_cohort(client, tasks, mode: str):
    skill_by_task = {task.task_id: skill for task, skill, _ in tasks}
    expected = {task.task_id: answer for task, _, answer in tasks}
    def factory(recorder, task):
        explicit = [skill_by_task[task.task_id]] if mode == "preloaded" and skill_by_task[task.task_id] else None
        return build_agent(client, recorder=recorder, allowed_permissions=set(Permission), session=Session(temporary_chat=True), session_config=SessionConfig(temporary_chat=True, persist_trace=False), skills_enabled=mode != "none", explicit_skills=explicit)
    runs = EvalRunner(factory, AnswerGrader(expected)).run([task for task, _, _ in tasks])
    successes = [run for run in runs if run.grade and run.grade.task_success]
    input_tokens = sum(sum(call.get("input_tokens", 0) for call in run.llm_calls) for run in runs)
    output_tokens = sum(sum(call.get("output_tokens", 0) for call in run.llm_calls) for run in runs)
    return {"task_success_rate": len(successes) / len(runs) if runs else 0, "total_tokens": input_tokens + output_tokens, "input_tokens": input_tokens, "output_tokens": output_tokens, "latency_ms": sum(run.latency_ms for run in runs), "failed_tasks": [run.task_id for run in runs if not run.grade or not run.grade.task_success], "runs": [run.to_dict() for run in runs]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True); parser.add_argument("--base-url")
    parser.add_argument("--input-cost-per-million", type=float, default=0); parser.add_argument("--output-cost-per-million", type=float, default=0)
    args = parser.parse_args(); revision, tasks = load_tasks(args.tasks); client = LLMClient(model=args.model, base_url=args.base_url)
    cohorts = {mode: run_cohort(client, tasks, mode) for mode in ("none", "preloaded", "automatic")}
    for result in cohorts.values():
        cost = result["input_tokens"] * args.input_cost_per_million / 1_000_000 + result["output_tokens"] * args.output_cost_per_million / 1_000_000
        successes = len(tasks) - len(result["failed_tasks"]); result["cost_per_success"] = cost / successes if successes else None
    baseline = cohorts["none"]["failed_tasks"]
    cohorts["preloaded"]["regressions_vs_none"] = sorted(set(cohorts["preloaded"]["failed_tasks"]) - set(baseline))
    cohorts["automatic"]["regressions_vs_none"] = sorted(set(cohorts["automatic"]["failed_tasks"]) - set(baseline))
    report = {"revision": revision, "model": args.model, "permissions": sorted(item.name for item in Permission), "isolated_sessions": True, "cohorts": cohorts}
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"); print(args.output); return 0


if __name__ == "__main__": raise SystemExit(main())
