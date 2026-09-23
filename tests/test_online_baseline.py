import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from eval import run_baseline


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

    expected = {"metrics": {"task_success_rate": 1.0}, "runs": []}
    monkeypatch.setattr(run_baseline, "LLMClient", FakeClient)
    monkeypatch.setattr(
        run_baseline, "run_online_baseline", lambda client, tasks: expected
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_baseline.py", "--output", str(output), "--model", "fixture"],
    )

    assert run_baseline.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == expected
