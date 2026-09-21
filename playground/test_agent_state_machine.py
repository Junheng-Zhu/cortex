from agent.loop_new import AgentLoop
from agent.state_new import AgentPhase, AgentState
from src.tools.result import ToolResult


class FakeExecutor:
    def list_tool_schemas(self):
        return []

    def execute(self, name, arguments):
        return ToolResult(name, 1, 0, True, None, None, "contents")


class SequenceLLM:
    def __init__(self, always_call_tool=False):
        self.calls = 0
        self.always_call_tool = always_call_tool

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1 or self.always_call_tool:
            return {
                "type": "tool_call",
                "name": "read_note",
                "arguments": {"filename": "python.md"},
            }
        return {"type": "final", "content": "done"}


def test_complete_state_machine_flow():
    loop = AgentLoop(SequenceLLM(), FakeExecutor())
    state = AgentState(messages=[{"role": "user", "content": "read"}])

    phases = []
    while state.phase is not AgentPhase.FINAL:
        phases.append(state.phase)
        loop.step(state)
    phases.append(state.phase)

    assert phases == [
        AgentPhase.DECIDE,
        AgentPhase.ACT,
        AgentPhase.OBSERVE,
        AgentPhase.REFLECT,
        AgentPhase.DECIDE,
        AgentPhase.FINAL,
    ]
    assert state.step_count == 1
    assert state.observations[0].action_id == state.actions[0].action_id


def test_successful_tool_does_not_go_directly_to_final():
    loop = AgentLoop(SequenceLLM(), FakeExecutor())
    state = AgentState(messages=[{"role": "user", "content": "read"}])

    loop.decide(state)
    loop.act(state)
    loop.observe(state)

    assert state.phase is AgentPhase.REFLECT
    assert state.step_count == 1


def test_max_steps_forces_termination():
    loop = AgentLoop(SequenceLLM(always_call_tool=True), FakeExecutor(), max_steps=1)
    state = AgentState(
        messages=[{"role": "user", "content": "read forever"}],
        max_steps=1,
    )

    while state.phase is not AgentPhase.FINAL:
        loop.step(state)

    assert state.step_count == 1
    assert state.phase is AgentPhase.FINAL
