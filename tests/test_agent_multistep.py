import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from cortex.runtime.loop import AgentLoop
from cortex.llm.protocol import LLMResponse, LLMUsage, ToolCall
from eval.grader import DeterministicGrader
from eval.runner import EvalRunner
from eval.tasks import Task
from cortex.app.bootstrap import build_agent
from cortex.llm.capabilities import ContextMode, ProviderCapabilities
from cortex.observability.tracer import RunRecorder
from cortex.tools.executor import ToolResult
from cortex.tools.base import ToolSandboxError
from cortex.tools.permission import Permission
from cortex.tools.builtin.shell import ShellInput
from cortex.tools.builtin.shell import ShellTool


class ScriptedLLM:
    capabilities = ProviderCapabilities(ContextMode.CLIENT_MANAGED)

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def respond(self, **request):
        self.requests.append(request)
        return next(self.responses)


class NotesExecutor:
    def __init__(self, missing_first=False):
        self.missing_first = missing_first
        self.calls = []

    def list_tool_schemas(self):
        return [
            {"type": "function", "name": "list_notes"},
            {"type": "function", "name": "read_note"},
        ]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if self.missing_first and len(self.calls) == 1:
            return ToolResult(
                name, 1, 2, False, "ToolFileNotFoundError", "missing", None, True
            )
        data = ["calendar.txt"] if name == "list_notes" else "meeting"
        return ToolResult(name, 1, 2, True, None, None, data, True)


def response(call_id=None, name=None, arguments=None, content=""):
    calls = [] if name is None else [ToolCall(call_id, name, arguments or {})]
    items = (
        []
        if name is None
        else [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments or {},
            }
        ]
    )
    return LLMResponse(
        content=content, tool_calls=calls, output_items=items, usage=LLMUsage(2, 1, 3)
    )


def test_list_then_read_is_a_real_multi_step_tool_loop():
    llm = ScriptedLLM(
        [
            response("c1", "list_notes"),
            response("c2", "read_note", {"filename": "calendar.txt"}),
            response(content="done"),
        ]
    )
    executor = NotesExecutor()
    loop = AgentLoop(llm, executor)

    assert loop.run("find my calendar") == "done"
    assert [name for name, _ in executor.calls] == ["list_notes", "read_note"]
    assert len(llm.requests) == 3


def test_recoverable_failure_reflects_and_replans():
    llm = ScriptedLLM(
        [
            response("bad", "read_note", {"filename": "missing.txt"}),
            response("list", "list_notes"),
            response(content="recovered"),
        ]
    )
    recorder = RunRecorder()
    loop = AgentLoop(llm, NotesExecutor(missing_first=True), recorder=recorder)

    assert loop.run("read a note") == "recovered"
    reflections = [e.data for e in recorder.events if e.event_type == "reflection"]
    assert reflections[0]["status"] == "REPLAN"
    assert recorder.runs[-1].success is True


def test_trace_contains_real_request_raw_response_and_reflection():
    llm = ScriptedLLM([response("c1", "list_notes"), response(content="done")])
    recorder = RunRecorder()
    AgentLoop(llm, NotesExecutor(), recorder=recorder).run("list files")

    llm_event = next(e for e in recorder.events if e.event_type == "llm_call")
    assert llm_event.data["request"]["input"] == [
        {"role": "user", "content": "list files"}
    ]
    assert llm_event.data["output_items"][0]["call_id"] == "c1"
    assert any(e.event_type == "reflection" for e in recorder.events)
    action_event = next(e for e in recorder.events if e.event_type == "action")
    assert action_event.data["validation_passed"] is True


def test_shell_tool_restricts_cwd_and_dangerous_commands():
    tool = ShellTool()
    result = tool.execute(ShellInput(command="python -c 'print(123)'"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "123"
    with pytest.raises(ToolSandboxError):
        tool.execute(ShellInput(command="rm -rf ."))
    with pytest.raises(ToolSandboxError):
        tool.execute(ShellInput(command="pwd", cwd=str(Path("..").resolve())))


def test_shell_requested_timeout_is_clamped_to_executor_limit():
    agent = build_agent(object())
    shell = agent.executor.registry.get("shell")
    validated = agent.executor._validate(shell, {"command": "pwd", "timeout": 30})
    limited = agent.executor._apply_runtime_limits(shell, validated)

    assert limited.timeout == agent.executor.timeout == shell.timeout


def test_runtime_registers_discovery_and_shell_tools():
    agent = build_agent(object())
    assert {"list_notes", "shell"}.issubset(agent.executor.registry.list_tools())


def test_eval_runner_collects_raw_metric_inputs_and_grades_task_separately():
    responses = [response("c1", "list_notes"), response(content="done")]

    def factory(recorder, task):
        return AgentLoop(ScriptedLLM(responses), NotesExecutor(), recorder=recorder)

    result = EvalRunner(factory, DeterministicGrader()).run(
        [Task("discover", "list", required_tools=["read_note"])]
    )[0]

    assert result.run_id
    assert result.steps == 1
    assert result.total_tokens == 6
    assert result.llm_calls and result.observations and result.reflections
    assert result.run_success is True
    assert result.grade.task_success is False


def test_permission_case_uses_runtime_without_execute_permission():
    llm = ScriptedLLM([response("shell", "shell", {"command": "python -V"})])

    def factory(recorder, task):
        return build_agent(
            llm,
            recorder=recorder,
            allowed_permissions={Permission.READ},
        )

    task = Task(
        "permission",
        "run shell",
        required_tools=["shell"],
        expected_outcome="PERMISSION_DENIED",
        expected_error_type="ToolPermissionError",
        excluded_permissions=["EXECUTE"],
    )
    result = EvalRunner(factory).run([task])[0]

    assert result.run_success is True
    assert result.outcome == "PERMISSION_DENIED"
    assert result.action_events[0]["status"] == "FAILED"
    assert result.action_events[0]["error_type"] == "ToolPermissionError"
    assert result.grade.task_success is True
