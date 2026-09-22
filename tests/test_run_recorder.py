import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.loop_new import AgentLoop
from src.core import loop as core_loop
from src.core.models import ContextMode
from src.ops.tracer import RunRecorder

from tests.test_responses_protocol import Executor, ResponsesLLM


def test_production_loop_injects_persistent_recorder(monkeypatch):
    captured = {}

    class RecorderStub:
        def __init__(self, *, persist=False):
            self.persist = persist

    class ExitImmediately:
        def run(self, query):
            raise AssertionError("the shell should exit before running a query")

    def fake_build_agent(client, recorder=None, max_steps=10):
        captured["recorder"] = recorder
        return ExitImmediately()

    monkeypatch.setattr(core_loop, "build_agent", fake_build_agent)
    monkeypatch.setattr(core_loop, "RunRecorder", RecorderStub)
    monkeypatch.setattr("builtins.input", lambda prompt: "exit")

    core_loop.run_loop(object())

    assert captured["recorder"].persist is True


def test_production_loop_respects_injected_nonpersistent_recorder(monkeypatch):
    recorder = RunRecorder(persist=False)
    captured = {}

    def fake_build_agent(client, recorder=None, max_steps=10):
        captured["recorder"] = recorder
        return object()

    monkeypatch.setattr(core_loop, "build_agent", fake_build_agent)
    monkeypatch.setattr("builtins.input", lambda prompt: "quit")

    core_loop.run_loop(object(), recorder=recorder)

    assert captured["recorder"] is recorder
    assert captured["recorder"].persist is False


def test_run_recorder_links_action_and_observation():
    recorder = RunRecorder(run_id="run-1")
    loop = AgentLoop(
        ResponsesLLM(ContextMode.SERVER_MANAGED),
        Executor(),
        recorder=recorder,
    )

    loop.run("echo hello")

    assert [event.event_type for event in recorder.events] == [
        "llm_call",
        "action",
        "observation",
        "reflection",
        "llm_call",
        "final",
    ]
    action = recorder.events[1]
    observation = recorder.events[2]
    assert action.run_id == observation.run_id == "run-1"
    assert action.data["action_id"] == observation.data["action_id"]
    assert action.data["call_id"] == observation.data["call_id"] == "call-1"
    assert action.data["action_id"] != action.data["call_id"]
    assert "output" not in observation.data
    assert observation.data["output_preview"] == "hello"
    assert observation.data["output_length"] == 5


def test_each_run_gets_a_different_run_id():
    recorder = RunRecorder()
    loop = AgentLoop(
        ResponsesLLM(ContextMode.SERVER_MANAGED), Executor(), recorder=recorder
    )
    loop.run("A")
    first_id = recorder.runs[-1].run_id

    loop.llm = ResponsesLLM(ContextMode.SERVER_MANAGED)
    loop.run("B")

    assert recorder.runs[-1].run_id != first_id
    assert {event.run_id for event in recorder.events} == {
        recorder.runs[0].run_id,
        recorder.runs[1].run_id,
    }


def test_run_aggregates_tokens_steps_and_latencies():
    class UsageLLM(ResponsesLLM):
        def respond(self, **request):
            response = super().respond(**request)
            return type(response)(
                response_id=response.response_id,
                content=response.content,
                tool_calls=response.tool_calls,
                usage=type(response.usage)(2, 3, 5),
                output_items=response.output_items,
            )

    recorder = RunRecorder()
    loop = AgentLoop(
        UsageLLM(ContextMode.SERVER_MANAGED), Executor(), recorder=recorder
    )
    loop.run("echo")
    run = recorder.runs[-1]

    assert run.success is True
    assert run.steps == 1
    assert run.total_tokens == 10
    assert run.llm_latency_ms >= 0
    assert run.latency_ms >= run.llm_latency_ms
    assert run.termination_reason == "completed"
    action = next(event for event in recorder.events if event.event_type == "action")
    assert action.data["attempts"] == 1
    assert "duration_ms" in action.data
