"""Web dashboard for run-oriented Cortex traces."""

import json

from flask import Flask, jsonify, render_template

from tracer import get_recent_runs, get_recent_traces


app = Flask(__name__)


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