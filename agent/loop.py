from .planner import Planner
from .react import ReActEngine
from .reflection import Reflector
from .state import AgentState



class Agent:
    def __init__(self, llm, executor):
        self.llm = llm
        self.executor = executor
        self.planner = Planner()
        self.react = ReActEngine()
        self.reflector = Reflector()

    def run(self, query):
        state = AgentState()
        state.messages.append({"role": "user", "content": query})
        # Planning
        plan = self.planner.plan(query)
        print("PLAN:", plan)
        while True:
            # ReAct Thought
            thought = self.react.think(state)
            response = self.llm.chat(state.messages, self.executor.schemas())
            ################################
            # Function Calling
            ################################
            if response["type"] == "tool_call":
                name = response["name"]
                args = response["arguments"]
                print("ACTION:", name, args)
                result = self.executor.execute(name, args)
                print("OBSERVATION:", result)
                state.observations.append(result)
                state.messages.append({"role": "tool", "content": str(result)})
                ################################
                # Reflection
                ################################
                ok = self.reflector.reflect(state)
                if ok:
                    continue
            else:
                state.final_answer = response["content"]
                return state.final_answer
