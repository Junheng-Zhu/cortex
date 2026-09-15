from .state_new import AgentState, AgentPhase
from .models import LLMResponse


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
        
        response = self.llm.generate_response(state.messages)
        state.messages.append(
            {
                "role": "assistant",
                "content": response
            }
        )
        llm_response = LLMResponse(response)
        if llm_response.is_tool_call():
            state.pending_tool_calls = llm_response.tool_calls
            state.phase = AgentPhase.EXECUTE
        else:
            state.final_response = llm_response.content
            state.phase = AgentPhase.FINAL

         

    def execute(self, state: AgentState):
        if len(state.pending_tool_calls) > 0:
            for tool_call in state.pending_tool_calls:
                tool_name = tool_call.name
                tool_args = tool_call.arguments
                result = self.executor.execute(tool_name, tool_args)
                self.state.messages.append(
                    {
                        "role": "tool",
                        "content": f"Executed {tool_name} with result: {result}"
                    }
                )

        state.phase = AgentPhase.OBSERVE

    def observe(self, state: AgentState):
        
        state.phase = AgentPhase.DECIDE
        