#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, json, sqlite3, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass
class Check:
    name: str
    passed: bool
    detail: str

def load_events(db_path: Path, session_id: str | None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if session_id:
        sid = session_id
    else:
        row = conn.execute("SELECT session_id FROM traces ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("trace DB 为空")
        sid = row["session_id"]
    rows = conn.execute("""
        SELECT id, session_id, timestamp, step_type, content,
               duration_ms, tokens_used, metadata
        FROM traces WHERE session_id = ? ORDER BY id ASC
    """, (sid,)).fetchall()
    conn.close()
    events = []
    for row in rows:
        e = dict(row)
        try:
            e["metadata"] = json.loads(e["metadata"] or "{}")
        except json.JSONDecodeError:
            e["metadata"] = {}
        events.append(e)
    return sid, events

def parse_tool_call(content: str):
    name, sep, arg_text = (content or "").partition("(")
    if not sep or not arg_text.endswith(")"):
        return name.strip(), {}
    try:
        args = ast.literal_eval(arg_text[:-1])
    except (ValueError, SyntaxError):
        args = {}
    return name.strip(), args if isinstance(args, dict) else {}

def get_tool_calls(events):
    result = []
    for e in events:
        if e["step_type"] == "tool_call":
            name, args = parse_tool_call(e.get("content", ""))
            result.append((name, args, e))
    return result

def run_checks(events, case, max_tool_rounds=6):
    checks = []
    types = {e["step_type"] for e in events}
    required = set(case.get("required_event_types", ["user_input", "llm_call", "tool_call", "response"]))
    missing = sorted(required - types)
    checks.append(Check(
        "event_contract", not missing,
        "required events are present" if not missing else f"missing events: {missing}"
    ))

    llms = [e for e in events if e["step_type"] == "llm_call"]
    min_llm = int(case.get("min_llm_calls", 1))
    checks.append(Check("llm_round_count", len(llms) >= min_llm, f"LLM rounds={len(llms)}, required>={min_llm}"))

    calls = get_tool_calls(events)
    names = [x[0] for x in calls]
    expected = case.get("expected_tool_sequence", [])
    checks.append(Check(
        "tool_sequence", names[:len(expected)] == expected,
        f"actual={names}, expected_prefix={expected}"
    ))

    max_tools = int(case.get("max_tool_calls", 10))
    checks.append(Check("tool_call_count", len(calls) <= max_tools, f"tool calls={len(calls)}, max={max_tools}"))

    expected_filename = case.get("expected_filename")
    if expected_filename:
        ok = any(name == "read_note" and args.get("filename") == expected_filename for name, args, _ in calls)
        checks.append(Check(
            "read_note_argument", ok,
            f"read_note.filename expected {expected_filename!r}"
        ))

    invalid_rounds = []
    for e in llms:
        round_no = (e.get("metadata") or {}).get("tool_round")
        if isinstance(round_no, int) and round_no > max_tool_rounds:
            invalid_rounds.append(round_no)
    checks.append(Check(
        "loop_bound", not invalid_rounds,
        f"all tool_round values <= {max_tool_rounds}" if not invalid_rounds else f"exceeded rounds: {invalid_rounds}"
    ))

    response_events = [e for e in events if e["step_type"] == "response"]
    response = response_events[-1].get("content", "") if response_events else ""

    all_terms = case.get("response_contains_all", [])
    missing_terms = [t for t in all_terms if t not in response]
    any_terms = case.get("response_contains_any", [])
    any_ok = True if not any_terms else any(t in response for t in any_terms)
    response_ok = not missing_terms and any_ok
    detail = "response content checks passed"
    if missing_terms:
        detail += f"; missing={missing_terms}"
    if any_terms and not any_ok:
        detail += f"; none of any={any_terms} found"
    checks.append(Check("response_content", response_ok, detail))

    token_ok = True
    missing_token_fields = []
    for e in llms:
        md = e.get("metadata") or {}
        if md.get("prompt_tokens") is None or md.get("completion_tokens") is None:
            token_ok = False
            missing_token_fields.append(e["id"])
    checks.append(Check(
        "llm_observability", token_ok,
        "prompt/completion tokens present for every LLM round"
        if token_ok else f"missing token fields at events={missing_token_fields}"
    ))
    return checks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=".trace.db")
    ap.add_argument("--session-id")
    ap.add_argument("--case", default="calendar_discovery")
    ap.add_argument("--cases-file", default="eval/cases.json")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"EVAL GATE: ERROR - DB not found: {db}")
        return 2

    cases = json.loads(Path(args.cases_file).read_text(encoding="utf-8"))
    case = next((c for c in cases if c["id"] == args.case), None)
    if not case:
        print(f"EVAL GATE: ERROR - case not found: {args.case}")
        return 2

    try:
        sid, events = load_events(db, args.session_id)
    except Exception as exc:
        print(f"EVAL GATE: ERROR - {exc}")
        return 2

    checks = run_checks(events, case)
    passed = sum(c.passed for c in checks)
    total = len(checks)

    print("=" * 64)
    print(f"Cortex Eval Gate | case={case['id']} | session={sid}")
    print("=" * 64)
    for c in checks:
        print(("PASS" if c.passed else "FAIL") + f"  {c.name}: {c.detail}")
    print("-" * 64)
    print(f"Result: {passed}/{total} checks passed")
    if passed != total:
        print("EVAL GATE: FAILED")
        return 1
    print("EVAL GATE: PASSED")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
