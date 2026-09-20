from agent.loop_new import AgentLoop
from src.tools.executor import ToolExecutor
from src.tools.file_tools import DeleteNoteTool, ReadNoteTool, SlowTool
from src.tools.permission import Permission
from src.tools.registry import ToolRegistry


DEFAULT_INSTRUCTIONS = """You are Cortex, a note-management assistant.
Use the supplied tools whenever the user asks you to inspect or modify current
note files. Base your final answer on the function outputs you receive.
"""


def build_default_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())

    return ToolExecutor(
        allowed_permissions={
            Permission.READ,
            Permission.WRITE,
            Permission.DELETE,
        },
        registry=registry,
    )


def run_loop(client) -> None:
    """Interactive CLI backed exclusively by the Responses API runtime."""
    agent = AgentLoop(
        llm=client,
        executor=build_default_executor(),
        max_steps=6,
        instructions=DEFAULT_INSTRUCTIONS,
    )

    print("Cortex 已启动，输入 'exit' 退出。")
    while True:
        query = input("\n你: ").strip()
        if query.lower() in {"exit", "quit", "q"}:
            print("再见！")
            return

        try:
            answer = agent.run(query)
        except Exception as exc:
            print(f"Cortex 调用失败: {exc}")
            continue

        print(f"Cortex: {answer}")
