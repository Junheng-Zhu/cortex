from .state_new import AgentState, AgentPhase

class Agent:
    def __init__(self, llm, executor):
        self.llm = llm
        self.executor = executor

    def run(self,query:str):
        state = AgentState()

        state.messages.append(
            {
                "role": "user",
                "content": query
            }
        )
        while state.phase != AgentPhase.FINAL and state.step_count < state.max_steps:
            if state.phase == AgentPhase.PLAN:
                self.plan(state, query)
            elif state.phase == AgentPhase.EXECUTE:
                self.execute(state)
            elif state.phase == AgentPhase.OBSERVE:
                self.observe(state)
            state.step_count += 1

        if state.phase == AgentPhase.FINAL:
                    return state.final_tool_result

    def execute(self, state: AgentState):
        tool_name = state.pending_tool_name
        tool_args = state.pending_tool_arguments
        result = self.executor.execute(tool_name, tool_args)
        state.last_tool_result = result
        state.phase = AgentPhase.OBSERVE

    def observe(self, state: AgentState):
        observation = state.last_tool_result
        state.observations.append(observation)
        state.messages.append(
            {
                "role": "assistant",
                "content": f"Observation: {observation}"
            }
        )
        