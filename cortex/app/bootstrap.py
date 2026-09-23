from cortex.runtime.loop import AgentLoop
from cortex.observability.tracer import RunRecorder
from cortex.context.artifacts import ArtifactStore
from cortex.tools.builtin.artifacts import ReadArtifactChunkTool
from cortex.tools.executor import ToolExecutor
from cortex.tools.builtin.notes import DeleteNoteTool, ListNotesTool, ReadNoteTool, SlowTool
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry
from cortex.tools.builtin.shell import ShellTool

from cortex.llm.client import LLMClient


def build_agent(
    client: LLMClient,
    recorder: RunRecorder | None = None,
    max_steps: int = 10,
    allowed_permissions: set[Permission] | None = None,
) -> AgentLoop:
    """Build the runtime agent with the note tools supported by this app."""
    registry = ToolRegistry()
    artifact_store = ArtifactStore()
    registry.register(ListNotesTool())
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())
    registry.register(ShellTool())
    registry.register(ReadArtifactChunkTool(artifact_store))
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
        artifact_store=artifact_store,
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
