#!/usr/bin/env python3
"""Run the fixed task catalog against the configured real LLM provider."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.grader import DeterministicGrader
from eval.metrics import aggregate_metrics
from eval.runner import EvalRunner
from eval.tasks import Task
from src.core.loop import build_agent
from src.core.models import LLMClient

DEFAULT_TASKS = ROOT / "eval" / "baseline_tasks.json"
DEFAULT_OUTPUT = ROOT / "eval" / "online_baseline.json"


def load_tasks(path: Path) -> list[Task]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Task(**item) for item in payload]


def run_online_baseline(client: LLMClient, tasks: list[Task]) -> dict:
    runner = EvalRunner(
        agent_factory=lambda recorder: build_agent(client, recorder=recorder),
        grader=DeterministicGrader(),
    )
    runs = runner.run(tasks)
    return {
        "baseline_id": "online-baseline-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_count": len(tasks),
        "model": client.model,
        "base_url": client.base_url,
        "metrics": aggregate_metrics(runs).to_dict(),
        "runs": [run.to_dict() for run in runs],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    args = parser.parse_args()

    tasks = load_tasks(args.tasks)
    client = LLMClient(model=args.model, base_url=args.base_url)
    report = run_online_baseline(client, tasks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"Saved online baseline to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
