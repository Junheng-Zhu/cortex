import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(
    0,
    PROJECT_ROOT,
)


from agent.loop import Agent
from agent.fake_llm import FakeLLM
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry
from src.tools.file_tools import (
    DeleteNoteTool,
    ReadNoteTool,
    SlowTool,
)
from src.tools.permission import Permission

def build_executor():
    registry = ToolRegistry()
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())
    allowed_permissions = {
        Permission.READ,
        Permission.WRITE,
        Permission.DELETE,
    }
    return ToolExecutor(
        allowed_permissions,
        registry,
    )


def main():
    executor = build_executor()
    agent = Agent(
        llm=FakeLLM(),
        executor=executor,
    )
    result = agent.run("读取python.md")
    print(
        "FINAL:",
        result,
    )


if __name__ == "__main__":
    main()
