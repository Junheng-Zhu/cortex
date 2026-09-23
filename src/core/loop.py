from agent.loop_new import AgentLoop
from src.ops.tracer import RunRecorder
from src.tools.executor import ToolExecutor
from src.tools.file_tools import DeleteNoteTool, ListNotesTool, ReadNoteTool, SlowTool
from src.tools.permission import Permission
from src.tools.registry import ToolRegistry
from src.tools.shell_tool import ShellTool

from .models import LLMClient


def build_agent(
    client: LLMClient,
    recorder: RunRecorder | None = None,
    max_steps: int = 10,
    allowed_permissions: set[Permission] | None = None,
) -> AgentLoop:
    """Build the runtime agent with the note tools supported by this app."""
    registry = ToolRegistry()
    registry.register(ListNotesTool())
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())
    registry.register(ShellTool())
    executor = ToolExecutor(
        allowed_permissions
        if allowed_permissions is not None
        else {Permission.READ, Permission.WRITE, Permission.DELETE, Permission.EXECUTE},
        registry,
    )
    return AgentLoop(
        llm=client,
        executor=executor,
        max_steps=max_steps,
        recorder=recorder,
    )


def run_loop(
    client: LLMClient,
    recorder: RunRecorder | None = None,
) -> None:
    """Run the production shell with persistent tracing enabled.

    Persistence is an entrypoint concern: callers such as tests can inject an
    in-memory recorder, while the interactive runtime writes data for the
    dashboard by default.
    """
    runtime_recorder = recorder if recorder is not None else RunRecorder(persist=True)
    agent = build_agent(client, recorder=runtime_recorder)
    print("Cortex 已启动（工具模式），输入 'exit' 退出。")
    while True:
        user_input = input("\n你: ")
        if user_input.strip().lower() in {"exit", "quit", "q"}:
            print("再见！")
            return
        print(f"Cortex: {agent.run(user_input)}")
