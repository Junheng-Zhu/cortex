from agent.state_new import AgentState
from runtime.action import Action
from runtime.observation import Observation
from runtime.reflection import ReflectionResult


def test_runtime_models_and_action_id_relationship():
    action = Action(
        tool_name="read_note",
        tool_call_id="call_1",
        arguments={"filename": "python.md"},
    )
    observation = Observation(
        action_id=action.action_id,
        success=True,
        output="note contents",
    )
    reflection = ReflectionResult(
        status="CONTINUE",
        summary="The note was read.",
        next_hint="Produce an answer.",
    )
    state = AgentState(
        actions=[action],
        observations=[observation],
        reflections=[reflection],
    )

    assert state.observations[0].action_id == state.actions[0].action_id
    assert state.reflections[0].status == "CONTINUE"
