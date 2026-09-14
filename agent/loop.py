from .planner import Planner
from .reflection import Reflector
from .state import AgentState, AgentPhase


class Agent:

    def __init__(self, llm, executor):
        self.llm = llm
        self.executor = executor

        self.planner = Planner()
        self.reflector = Reflector()

    def run(self, query: str):

        state = AgentState()

        state.messages.append(
            {
                "role": "user",
                "content": query,
            }
        )

        while not state.finished:

            # ==================================
            # Runtime budget
            # ==================================

            if state.step_count >= state.max_steps:
                state.phase = AgentPhase.FAILED
                state.error = "Agent exceeded maximum steps"
                continue

            # ==================================
            # PLAN
            # ==================================

            if state.phase == AgentPhase.PLAN:

                state.plan = self.planner.plan(query)

                print(
                    "PLAN:",
                    state.plan,
                )

                state.phase = AgentPhase.DECIDE

                continue

            # ==================================
            # DECIDE
            # ==================================

            if state.phase == AgentPhase.DECIDE:

                response = self.llm.chat(
                    state.messages,
                    self.executor.list_tool_schemas(),
                )

                if response["type"] == "tool_call":

                    state.pending_tool_name = response["name"]
                    state.pending_tool_arguments = response["arguments"]

                    state.phase = AgentPhase.ACT

                else:

                    state.final_answer = response["content"]

                    state.phase = AgentPhase.FINAL

                continue

            # ==================================
            # ACT
            # ==================================

            if state.phase == AgentPhase.ACT:

                tool_name = state.pending_tool_name
                arguments = state.pending_tool_arguments

                print(
                    "ACTION:",
                    tool_name,
                    arguments,
                )

                result = self.executor.execute(
                    tool_name,
                    arguments,
                )

                state.step_count += 1

                state.actions.append(
                    {
                        "tool": tool_name,
                        "arguments": arguments,
                    }
                )

                state.observations.append(result)

                state.last_tool_result = result

                print(
                    "OBSERVATION:",
                    result,
                )

                state.messages.append(
                    {
                        "role": "tool",
                        "content": str(result),
                    }
                )

                state.pending_tool_name = None
                state.pending_tool_arguments = None

                state.phase = AgentPhase.REFLECT

                continue

            # ==================================
            # REFLECT
            # ==================================

            if state.phase == AgentPhase.REFLECT:

                should_continue = self.reflector.reflect(state)

                if should_continue:
                    state.phase = AgentPhase.DECIDE

                else:
                    state.phase = AgentPhase.FAILED
                    state.error = "Agent reflection stopped execution"

                continue

        # ==================================
        # Terminal states
        # ==================================

        if state.phase == AgentPhase.FINAL:
            return state.final_answer

        raise RuntimeError(state.error or "Agent execution failed")
