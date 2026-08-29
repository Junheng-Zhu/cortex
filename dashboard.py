import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template_string, jsonify
from src.ops.tracer import get_recent_traces

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Cortex 追踪仪表板</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        h1 { color: #333; }
        .trace-item { background: white; padding: 12px; margin: 8px 0; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .timestamp { color: #888; font-size: 0.8em; }
        .step-type { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: bold; }
        .step-user_input { background: #e3f2fd; color: #0d47a1; }
        .step-gate { background: #fff3e0; color: #e65100; }
        .step-llm_call { background: #e8f5e9; color: #1b5e20; }
        .step-tool_call { background: #fce4ec; color: #880e4f; }
        .step-response { background: #f3e5f5; color: #4a148c; }
        .content { margin-top: 4px; color: #333; word-break: break-all; }
        .meta { font-size: 0.8em; color: #666; margin-top: 4px; }
        #filter { margin-bottom: 20px; padding: 8px; width: 300px; }
    </style>
</head>
<body>
    <h1>🧠 Cortex 实时追踪</h1>
    <input type="text" id="filter" placeholder="按会话ID或内容过滤..." onkeyup="filterTable()">
    <div id="traces"></div>

    <script>
        function fetchTraces() {
            fetch('/api/traces')
                .then(res => res.json())
                .then(data => {
                    const container = document.getElementById('traces');
                    container.innerHTML = data.map(t => `
                        <div class="trace-item" data-session="${t.session_id}" data-content="${t.content}">
                            <div>
                                <span class="step-type step-${t.step_type}">${t.step_type}</span>
                                <span class="timestamp">${t.timestamp} | 会话: ${t.session_id}</span>
                                ${t.duration_ms ? `⏱️ ${t.duration_ms.toFixed(0)}ms` : ''}
                                ${t.tokens_used ? `🔢 ${t.tokens_used} tokens` : ''}
                            </div>
                            <div class="content">${t.content || '(空)'}</div>
                            <div class="meta">${t.metadata || ''}</div>
                        </div>
                    `).join('');
                });
        }

        function filterTable() {
            const filter = document.getElementById('filter').value.toLowerCase();
            const items = document.querySelectorAll('.trace-item');
            items.forEach(item => {
                const match = item.dataset.session.includes(filter) || item.dataset.content.toLowerCase().includes(filter);
                item.style.display = match ? 'block' : 'none';
            });
        }

        // 每 3 秒自动刷新
        setInterval(fetchTraces, 3000);
        fetchTraces();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/traces')
def api_traces():
    rows = get_recent_traces(limit=100)
    data = []
    for row in rows:
        data.append({
            "id": row[0],
            "session_id": row[1],
            "timestamp": row[2],
            "step_type": row[3],
            "content": row[4],
            "duration_ms": row[5],
            "tokens_used": row[6],
            "metadata": row[7]
        })
    return jsonify(data)

if __name__ == "__main__":
    # 安装 flask 依赖：pip install flask
    app.run(debug=True, port=5000)