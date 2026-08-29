import sqlite3
import json
import time
import uuid
import os
from datetime import datetime
from typing import Optional, Dict, Any

# 数据库文件路径（放在项目根目录）
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".trace.db")

def init_db():
    """初始化数据库表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            step_type TEXT NOT NULL,   -- 'user_input', 'gate', 'llm_call', 'tool_call', 'response'
            content TEXT,
            duration_ms REAL,
            tokens_used INTEGER,
            metadata TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_event(session_id: str, step_type: str, content: str = "", duration_ms: float = 0, tokens_used: int = 0, metadata: Dict[str, Any] = None):
    """记录一条事件"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO traces (session_id, timestamp, step_type, content, duration_ms, tokens_used, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            datetime.now().isoformat(),
            step_type,
            content,
            duration_ms,
            tokens_used,
            json.dumps(metadata) if metadata else "{}"
        )
    )
    conn.commit()
    conn.close()

def get_recent_traces(limit: int = 50):
    """获取最近的追踪记录（供 Dashboard 使用）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, session_id, timestamp, step_type, content, duration_ms, tokens_used, metadata FROM traces ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

# 程序启动时自动建表
init_db()