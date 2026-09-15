from .state_new import AgentState, AgentPhase
from .models import LLMResponse


class Agent:
    def __init__(self, llm, executor):
        self.llm = llm
        self.executor = executor

    def run(self,query:str):
        state = AgentState()

        
        while state.phase != AgentPhase.FINAL and state.step_count < state.max_steps:
            if state.phase == AgentPhase.DECIDE:
                self.decide(state, query)
            elif state.phase == AgentPhase.EXECUTE:
                self.execute(state)
            elif state.phase == AgentPhase.OBSERVE:
                self.observe(state)
            state.step_count += 1

        if state.phase == AgentPhase.FINAL:
                    return state.final_response

    def decide(self, state: AgentState, query:str):
        state.messages.append(
            {
                "role": "user",
                "content": query
            }
        )
        response = self.llm.generate_response(state.messages)
        state.messages.append(
            {
                "role": "assistant",
                "content": response
            }
        )
        llm_response = LLMResponse(response)
        if llm_response.is_tool_call():
            state.pending_tool_name = llm_response.tool_name
            state.pending_tool_arguments = llm_response.tool_arguments
            state.phase = AgentPhase.EXECUTE
        else:
            state.final_response = llm_response.content
            state.phase = AgentPhase.FINAL

         

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
                "role": "tool",
                "content": f"Observation: {observation}"
            }
        )
        state.phase = AgentPhase.DECIDE
        