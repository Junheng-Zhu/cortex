import sys
import os
# 获取项目根目录：playground 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from agent.loop import Agent
from agent.fake_llm import FakeLLM
from src.tools.executor import ToolExecutor

from src.tools.registry import ToolRegistry
from src.tools.file_tools import DeleteNoteTool, ReadNoteTool, SlowTool
from src.tools.permission import Permission


def main():
    registry = ToolRegistry()
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())

    allowed_permissions = {
        Permission.READ,
        Permission.WRITE,
        Permission.DELETE,
    }
    executor = ToolExecutor(allowed_permissions, registry)

    agent = Agent(llm=FakeLLM(), executor=executor)
    answer = agent.run("读取python.md")
    print(answer)


if __name__ == "__main__":
    main()
