from runtime.action import Action
from runtime.observation import Observation

from .reflection import Reflector
from .state_new import AgentPhase, AgentState


class AgentLoop:
    """Agent state machine: DECIDE -> ACT -> OBSERVE -> REFLECT."""

    def __init__(self, llm, executor, max_steps: int = 10):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.llm = llm
        self.executor = executor
        self.max_steps = max_steps
        self.reflector = Reflector()

    def run(self, query: str) -> str | None:
        state = AgentState(max_steps=self.max_steps)
        state.messages.append({"role": "user", "content": query})

        while state.phase is not AgentPhase.FINAL:
            self.step(state)
        return state.final_answer

    def step(self, state: AgentState) -> AgentState:
        if state.phase is AgentPhase.DECIDE:
            self.decide(state)
        elif state.phase is AgentPhase.ACT:
            self.act(state)
        elif state.phase is AgentPhase.OBSERVE:
            self.observe(state)
        elif state.phase is AgentPhase.REFLECT:
            self.reflect(state)
        return state

    def decide(self, state: AgentState) -> None:
        if state.step_count >= state.max_steps:
            state.final_answer = "Maximum number of action steps reached."
            state.phase = AgentPhase.FINAL
            return

        response = self.llm.chat(
            state.messages,
            self.executor.list_tool_schemas(),
        )
        if response["type"] == "tool_call":
            action = Action(
                tool_name=response["name"],
                arguments=response.get("arguments", {}),
            )
            state.actions.append(action)
            state.pending_actions.append(action)
            state.phase = AgentPhase.ACT
            return

        state.final_answer = response.get("content", "")
        state.phase = AgentPhase.FINAL

    def act(self, state: AgentState) -> None:
        if not state.pending_actions:
            state.final_answer = "No pending action to execute."
            state.phase = AgentPhase.FINAL
            return

        action = state.pending_actions[0]
        action.status = "RUNNING"
        try:
            state.last_tool_result = self.executor.execute(
                action.tool_name,
                action.arguments,
            )
            action.status = (
                "SUCCEEDED" if state.last_tool_result.success else "FAILED"
            )
        except Exception as exc:
            action.status = "FAILED"
            state.last_tool_result = exc

        state.step_count += 1
        state.phase = AgentPhase.OBSERVE

    def observe(self, state: AgentState) -> None:
        action = state.pending_actions.pop(0)
        result = state.last_tool_result
        if isinstance(result, Exception):
            observation = Observation(
                action_id=action.action_id,
                success=False,
                error=str(result),
            )
        else:
            observation = Observation(
                action_id=action.action_id,
                success=result.success,
                output=result.data if result.success else None,
                error=(
                    None
                    if result.success
                    else result.error_message or result.error_type
                ),
            )

        state.observations.append(observation)
        state.messages.append({"role": "tool", "content": str(observation.output)})
        state.phase = AgentPhase.REFLECT

    def reflect(self, state: AgentState) -> None:
        reflection = self.reflector.reflect(state.observations[-1])
        state.reflections.append(reflection)

        if reflection.status == "FAILED":
            state.final_answer = reflection.summary
            state.phase = AgentPhase.FINAL
        elif state.step_count >= state.max_steps:
            state.final_answer = "Maximum number of action steps reached."
            state.phase = AgentPhase.FINAL
        else:
            state.phase = AgentPhase.DECIDE


# Keep the concise name convenient for callers constructing an agent.
Agent = AgentLoop
