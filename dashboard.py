"""Small, dependency-light dashboard for run-oriented Cortex traces."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template_string

from src.ops.tracer import get_recent_runs, get_recent_traces

app = Flask(__name__)

HTML_TEMPLATE = r"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cortex Runs</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#131b30;--muted:#91a0bd;--line:#283553;--good:#42d392;--bad:#ff6b7a;--accent:#76a8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef3ff;font:14px system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:32px 20px}
h1{margin:0 0 6px;font-size:28px}.sub{color:var(--muted);margin-bottom:24px}.toolbar{display:flex;gap:12px;margin-bottom:18px}input{width:360px;max-width:100%;padding:11px 14px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:inherit}
.run{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:12px 0;overflow:hidden}.run>summary{cursor:pointer;padding:16px;list-style:none;display:grid;grid-template-columns:1fr repeat(4,auto);gap:18px;align-items:center}.id{font-family:ui-monospace,monospace;color:var(--accent)}.metric{color:var(--muted);white-space:nowrap}.ok{color:var(--good)}.fail{color:var(--bad)}
.events{border-top:1px solid var(--line);padding:8px 16px 16px}.event{display:grid;grid-template-columns:150px 100px 1fr;gap:10px;padding:9px 0;border-bottom:1px solid #202b45}.type{font-weight:700}.data{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;color:#cad5ea;font:12px ui-monospace,monospace}@media(max-width:700px){.run>summary{grid-template-columns:1fr 1fr}.event{grid-template-columns:1fr}.time{display:none}}
</style></head><body><main><h1>Cortex Run Traces</h1><div class="sub">Run 汇总、模型延迟、工具执行与 token 使用情况</div>
<div class="toolbar"><input id="filter" placeholder="筛选 run id、结束原因或事件…"></div><section id="runs"></section>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ms=n=>`${Number(n||0).toFixed(1)} ms`;
const eventData=e=>e.event_type==='llm_call'
  ? `<b>LLM Request</b><pre class="data">${esc(JSON.stringify(e.data.request,null,2))}</pre><b>Raw Response</b><pre class="data">${esc(JSON.stringify({output_items:e.data.output_items,output_text:e.data.output_text,response_id:e.data.response_id},null,2))}</pre>`
  : `<pre class="data">${esc(JSON.stringify(e.data,null,2))}</pre>`;
let lastView='';
async function refresh(){const openRuns=new Set([...document.querySelectorAll('details.run[open]')].map(e=>e.dataset.runId));const data=await fetch('/api/runs').then(r=>r.json());const q=document.querySelector('#filter').value.toLowerCase();const view=JSON.stringify([data,q]);if(view===lastView)return;lastView=view;
document.querySelector('#runs').innerHTML=data.filter(r=>JSON.stringify(r).toLowerCase().includes(q)).map(r=>`<details class="run" data-run-id="${esc(r.run_id)}" ${openRuns.has(String(r.run_id))?'open':''}><summary><span><b class="${r.success?'ok':'fail'}">${r.success?'SUCCESS':'FAILED'}</b><br><span class="id">${esc(r.run_id)}</span><br><small>${esc(r.termination_reason)}</small></span><span class="metric">${r.steps} steps</span><span class="metric">${r.total_tokens} tokens</span><span class="metric">LLM ${ms(r.llm_latency_ms)}</span><span class="metric">总计 ${ms(r.latency_ms)}</span></summary><div class="events">${r.events.map(e=>`<div class="event"><span class="time">${esc(e.timestamp)}</span><span class="type">${esc(e.event_type)}</span><div>${eventData(e)}</div></div>`).join('')}</div></details>`).join('')||'<p class="sub">没有匹配的 Run。</p>'}
document.querySelector('#filter').addEventListener('input',refresh);refresh();setInterval(refresh,3000);
</script></main></body></html>"""


@app.get("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.get("/api/runs")
def api_runs():
    events_by_run = {}
    for row in get_recent_traces(limit=500):
        data = json.loads(row[7] or "{}")
        events_by_run.setdefault(row[1], []).append(
            {"timestamp": row[2], "event_type": row[3], "data": data}
        )
    runs = get_recent_runs(limit=100)
    for run in runs:
        run["success"] = bool(run["success"])
        run["events"] = list(reversed(events_by_run.get(run["run_id"], [])))
    return jsonify(runs)


@app.get("/api/traces")
def api_traces():
    """Compatibility endpoint for integrations still consuming flat events."""
    return jsonify(
        [
            {
                "id": r[0],
                "run_id": r[1],
                "timestamp": r[2],
                "event_type": r[3],
                "content": r[4],
                "duration_ms": r[5],
                "tokens_used": r[6],
                "data": json.loads(r[7] or "{}"),
            }
            for r in get_recent_traces(limit=100)
        ]
    )


if __name__ == "__main__":
    app.run(port=5000)
