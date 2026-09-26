"""Web dashboard for run-oriented Cortex traces."""

import json

from flask import Flask, jsonify, render_template

from .tracer import get_recent_runs, get_recent_traces


app = Flask(__name__)


def _human_summary(events: list[dict]) -> dict:
    context = next((e["data"] for e in events if e["event_type"] == "llm_call"), {})
    request_items = context.get("request", {}).get("input", [])
    names = {item.get("name") for item in request_items}
    outcome = next((e["data"] for e in reversed(events) if e["event_type"] == "skill_outcome"), {})
    steps = []
    llm_number = 0
    for event in events:
        if event["event_type"] == "llm_call":
            llm_number += 1
            calls = [item.get("name") for item in event["data"].get("output_items", []) if item.get("type") in {"function_call", "tool_call"}]
            steps.append({"phase": "decide", "summary": f"模型决策 #{llm_number}", "tools": calls})
        elif event["event_type"] == "action":
            steps.append({"phase": "action", "summary": event["data"].get("status", ""), "tools": [event["data"].get("tool_name")]})
    return {
        "session_id": context.get("session_id", "unknown"),
        "semantic_retrieval": "cortex_memory" in names,
        "episodic_retrieval": any("Episodic (" in str(item.get("content", "")) for item in request_items),
        "skill_searched": bool(outcome.get("searched")),
        "selected_skills": outcome.get("selected", []),
        "steps": steps,
    }


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/api/runs")
def api_runs():
    events_by_run: dict[str, list[dict]] = {}
    for row in get_recent_traces(limit=500):
        events_by_run.setdefault(row[1], []).append(
            {
                "timestamp": row[2],
                "event_type": row[3],
                "data": json.loads(row[7] or "{}"),
            }
        )
    runs = get_recent_runs(limit=100)
    for run in runs:
        run["success"] = bool(run["success"])
        run["events"] = list(reversed(events_by_run.get(run["run_id"], [])))
        run["human_summary"] = _human_summary(run["events"])
    return jsonify(runs)


@app.get("/api/traces")
def api_traces():
    """Return flat events for integrations that consume the legacy endpoint."""
    return jsonify(
        [
            {
                "id": row[0],
                "run_id": row[1],
                "timestamp": row[2],
                "event_type": row[3],
                "content": row[4],
                "duration_ms": row[5],
                "tokens_used": row[6],
                "data": json.loads(row[7] or "{}"),
            }
            for row in get_recent_traces(limit=100)
        ]
    )


def main() -> None:
    app.run(port=5000)


if __name__ == "__main__":
    main()

""" python d:/pyproject/cortex/cortex/observability/dashboard.py """
