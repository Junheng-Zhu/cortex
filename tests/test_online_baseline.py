import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from eval import run_baseline
from eval.grader import GradeResult
from eval.runner import EvalRun
from eval.tasks import Task
from cortex.observability.tracer import RunRecorder
from cortex.tools.permission import Permission


def test_online_baseline_catalog_loads_all_fixed_tasks():
    tasks = run_baseline.load_tasks(run_baseline.DEFAULT_TASKS)

    assert len(tasks) == 15
    assert tasks[8].task_id == "shell_python_cwd"
    assert "Path.cwd()" in tasks[8].input
    assert tasks[9].task_id == "shell_python_explicit_cwd"


def test_online_baseline_entrypoint_writes_report(monkeypatch, tmp_path):
    output = tmp_path / "online.json"

    class FakeClient:
        def __init__(self, model=None, base_url=None):
            self.model = model
            self.base_url = base_url

    expected = {
        "baseline_id": "online-baseline-v2",
        "model": "fixture",
        "task_count": 0,
        "summary": {},
        "failed_task_count": 0,
        "tasks": [],
    }
    raw = {"runs": []}
    monkeypatch.setattr(run_baseline, "LLMClient", FakeClient)
    monkeypatch.setattr(
        run_baseline, "run_online_baseline", lambda client, tasks: (expected, raw)
    )
    monkeypatch.setattr(run_baseline, "_print_report", lambda *args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_baseline.py",
            "--output",
            str(output),
            "--model",
            "fixture",
            "--no-raw",
        ],
    )

    assert run_baseline.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_permission_task_removes_execute_from_real_agent_runtime():
    task = Task(
        "permission",
        "shell",
        required_tools=["shell"],
        expected_outcome="PERMISSION_DENIED",
        excluded_permissions=["EXECUTE"],
    )
    agent = run_baseline._agent_factory(object())(RunRecorder(), task)

    assert Permission.EXECUTE not in agent.executor.allowed_permissions
    assert Permission.READ in agent.executor.allowed_permissions


def test_success_summary_omits_raw_trace_and_failure_adds_diagnostics():
    task = Task("read", "read", required_tools=["read_note"])
    grade = GradeResult(
        task_success=True,
        goal_met=True,
        run_success=True,
        required_tools_matched=True,
        tool_sequence_matched=True,
        forbidden_tool_used=False,
        expected_outcome_matched=True,
        expected_error_matched=True,
        reason="goal met",
    )
    run = EvalRun(
        task_id="read",
        run_id="run",
        answer="done",
        run_success=True,
        outcome="COMPLETED",
        actions=["read_note"],
        steps=1,
        total_tokens=20,
        latency_ms=10,
        llm_latency_ms=8,
        tool_duration_ms=2,
        llm_calls=[{"request": {"tools": ["large raw schema"]}}],
        grade=grade,
    )

    summary = run_baseline._task_summary(task, run)
    assert set(summary) == {
        "task_id",
        "task_success",
        "run_success",
        "actions",
        "steps",
        "tokens",
        "latency_ms",
    }

    run.grade = GradeResult(
        **{
            **grade.__dict__,
            "task_success": False,
            "goal_met": False,
            "reason": "required_tools did not match",
            "failed_criteria": ["required_tools"],
        }
    )
    failed = run_baseline._task_summary(task, run)
    assert failed["failure_reason"] == "required_tools did not match"
    assert failed["failed_criteria"] == ["required_tools"]
    assert "llm_calls" not in failed


def test_terminal_report_is_summary_first(capsys, tmp_path):
    report = {
        "task_count": 2,
        "failed_task_count": 1,
        "summary": {
            "p50_latency_ms": 10,
            "p95_latency_ms": 20,
            "average_steps": 1.5,
            "tokens_per_successful_task": 30,
        },
        "tasks": [
            {"task_id": "safe", "task_success": True, "run_success": True},
            {
                "task_id": "failed",
                "task_success": False,
                "run_success": False,
                "failure_reason": "expected_error did not match",
                "actions": ["shell"],
            },
        ],
    }
    summary_path = tmp_path / "summary.json"
    raw_path = tmp_path / "raw.json"

    run_baseline._print_report(report, raw_path, summary_path)
    output = capsys.readouterr().out

    assert output.startswith("Cortex Online Baseline v0.2\nTasks: 2")
    assert "Task Success: 1/2" in output
    assert "Run Success: 1/2" in output
    assert "- failed" in output
    assert f"Full raw trace: {raw_path}" in output
    assert f"Summary report: {summary_path}" in output
