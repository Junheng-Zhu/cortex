#!/usr/bin/env python3
"""Run the fixed task catalog against the configured real LLM provider."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.grader import DeterministicGrader
from eval.metrics import aggregate_metrics
from eval.runner import EvalRun, EvalRunner
from eval.tasks import Task
from cortex.app.bootstrap import build_agent
from cortex.llm.client import LLMClient
from cortex.tools.permission import Permission

DEFAULT_TASKS = ROOT / "eval" / "baseline_tasks.json"
DEFAULT_OUTPUT = ROOT / "eval" / "online_baseline.json"
DEFAULT_RAW_DIR = ROOT / "eval" / "raw"


def load_tasks(path: Path) -> list[Task]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Task(**item) for item in payload]


def _agent_factory(client: LLMClient):
    all_permissions = set(Permission)

    def create(recorder, task: Task):
        excluded = {Permission[name] for name in task.excluded_permissions}
        return build_agent(
            client,
            recorder=recorder,
            allowed_permissions=all_permissions - excluded,
        )

    return create


def _task_summary(task: Task, run: EvalRun) -> dict:
    result = {
        "task_id": task.task_id,
        "task_success": bool(run.grade and run.grade.task_success),
        "run_success": run.run_success,
        "actions": run.actions,
        "steps": run.steps,
        "tokens": run.total_tokens,
        "latency_ms": run.latency_ms,
    }
    if run.grade and not run.grade.task_success:
        result.update(
            expected={
                "required_tools": task.required_tools,
                "forbidden_tools": task.forbidden_tools,
                "expected_outcome": task.expected_outcome,
                "expected_error_type": task.expected_error_type,
            },
            actual={
                "actions": run.actions,
                "outcome": run.outcome,
                "error_types": run.error_types,
            },
            failure_reason=run.grade.reason,
            failed_criteria=run.grade.failed_criteria,
        )
    if not run.run_success:
        result.update(
            error_type=run.error_type,
            error_message=run.error_message,
            termination_reason=run.termination_reason,
        )
    return result


def run_online_baseline(client: LLMClient, tasks: list[Task]) -> tuple[dict, dict]:
    runs = EvalRunner(_agent_factory(client), DeterministicGrader()).run(tasks)
    metrics = aggregate_metrics(runs).to_dict()
    timestamp = datetime.now(timezone.utc).isoformat()
    task_summaries = [_task_summary(task, run) for task, run in zip(tasks, runs)]
    report = {
        "baseline_id": "online-baseline-v2",
        "model": client.model,
        "task_count": len(tasks),
        "summary": metrics,
        "failed_task_count": sum(not item["task_success"] for item in task_summaries),
        "tasks": task_summaries,
    }
    raw = {
        "baseline_id": "online-baseline-v2",
        "generated_at": timestamp,
        "model": client.model,
        "base_url": client.base_url,
        "tasks": [asdict(task) for task in tasks],
        "runs": [run.to_dict() for run in runs],
    }
    return report, raw


def _print_report(report: dict, raw_path: Path | None, summary_path: Path) -> None:
    summary = report["summary"]
    succeeded = report["task_count"] - report["failed_task_count"]
    run_succeeded = sum(task["run_success"] for task in report["tasks"])
    safety = [
        task
        for task in report["tasks"]
        if task["task_id"]
        in {
            "shell_block_rm",
            "shell_bad_cwd",
            "shell_permission_denied",
            "read_path_traversal",
        }
    ]
    print("Cortex Online Baseline v0.2")
    print(f"Tasks: {report['task_count']}")
    print(f"Task Success: {succeeded}/{report['task_count']}")
    print(f"Run Success: {run_succeeded}/{report['task_count']}")
    print(f"Safety Cases: {sum(task['task_success'] for task in safety)}/{len(safety)}")
    failed = [task for task in report["tasks"] if not task["task_success"]]
    if failed:
        print("\nFailed Tasks:")
        for task in failed:
            print(f"- {task['task_id']}")
            print(f"  reason: {task['failure_reason']}")
            print(f"  actual: {' -> '.join(task['actions']) or '(no tools)'}")
    print(f"\np50 latency: {summary['p50_latency_ms']:.1f} ms")
    print(f"p95 latency: {summary['p95_latency_ms']:.1f} ms")
    print(f"Avg steps: {summary['average_steps']:.2f}")
    print(f"Tokens/success: {summary['tokens_per_successful_task']:.1f}")
    if raw_path:
        print(f"\nFull raw trace: {raw_path}")
    print(f"Summary report: {summary_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--no-raw", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    args = parser.parse_args()

    tasks = load_tasks(args.tasks)
    client = LLMClient(model=args.model, base_url=args.base_url)
    report, raw = run_online_baseline(client, tasks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    raw_path = None
    if not args.no_raw:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_path = args.raw_dir / f"online_baseline_{stamp}.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
    _print_report(report, raw_path, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
