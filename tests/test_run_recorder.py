import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.loop_new import AgentLoop
from src.core.models import ContextMode
from src.ops.tracer import RunRecorder

from tests.test_responses_protocol import Executor, ResponsesLLM


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
        "llm_call",
        "final",
    ]
    action = recorder.events[1]
    observation = recorder.events[2]
    assert action.run_id == observation.run_id == "run-1"
    assert action.data["action_id"] == observation.data["action_id"] == "call-1"
