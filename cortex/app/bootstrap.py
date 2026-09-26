from cortex.runtime.loop import AgentLoop
from cortex.observability.tracer import RunRecorder
from cortex.context.artifacts import ArtifactStore
from cortex.tools.builtin.artifacts import ReadArtifactChunkTool
from cortex.tools.executor import ToolExecutor
from cortex.tools.builtin.notes import DeleteNoteTool, ListNotesTool, ReadNoteTool, SlowTool
from cortex.tools.permission import Permission
from cortex.tools.registry import ToolRegistry
from cortex.tools.builtin.shell import ShellTool
from cortex.execution import DockerBackend, DockerBackendConfig, ExecutionBackend, LocalBackend
from cortex.tools.builtin.shell import discover_bash, MAX_OUTPUT_CHARS, PROJECT_ROOT

from cortex.llm.client import LLMClient
from cortex.memory import MemoryManager, SQLiteMemoryStore
from cortex.memory.session_store import SessionStore, SQLiteSessionStore
from cortex.runtime.checkpoint import CheckpointStore, SQLiteCheckpointStore
from cortex.runtime.session import Session, SessionConfig
from cortex.skills import BM25SkillIndex, DenseSkillIndex, EmbeddingCache, HybridSkillIndex, OpenAIEmbeddingBackend, SkillLoadTool, SkillReadResourceTool, SkillRegistry, SkillSearchTool
from pathlib import Path


def build_agent(
    client: LLMClient,
    recorder: RunRecorder | None = None,
    max_steps: int = 10,
    allowed_permissions: set[Permission] | None = None,
    session: Session | None = None,
    session_config: SessionConfig | None = None,
    memory_manager: MemoryManager | None = None,
    session_store: SessionStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    session_id: str | None = None,
    skill_roots: list[str | Path] | None = None,
    skills_enabled: bool = True,
    explicit_skills: list[str] | None = None,
    skill_index_body: bool = False,
    skill_retriever: str = "bm25",
    skill_embedding_backend=None,
    skill_snapshot_root: str | Path | None = None,
    execution_backend: ExecutionBackend | str | None = None,
    execution_workspace: str | Path | None = None,
) -> AgentLoop:
    """Build the runtime agent with the note tools supported by this app."""
    registry = ToolRegistry()
    artifact_store = ArtifactStore()
    registry.register(ListNotesTool())
    registry.register(ReadNoteTool())
    registry.register(DeleteNoteTool())
    registry.register(SlowTool())
    workspace = Path(execution_workspace or PROJECT_ROOT).resolve()
    if execution_backend is None or execution_backend == "local":
        shell_backend = LocalBackend(discover_bash, MAX_OUTPUT_CHARS, workspace)
    elif execution_backend == "docker":
        shell_backend = DockerBackend(DockerBackendConfig(workspace=workspace))
    elif isinstance(execution_backend, ExecutionBackend):
        shell_backend = execution_backend
    else:
        raise ValueError("execution_backend must be 'local', 'docker', or an ExecutionBackend")
    registry.register(ShellTool(shell_backend, workspace))
    registry.register(ReadArtifactChunkTool(artifact_store))
    skill_registry = None
    skill_index = None
    if skills_enabled:
        registry_options = {"index_body": skill_index_body}
        if skill_snapshot_root is not None:
            registry_options["snapshot_root"] = Path(skill_snapshot_root)
        skill_registry = SkillRegistry(
            [Path(path) for path in (skill_roots if skill_roots is not None else [Path.cwd() / "skills"])],
            **registry_options,
        )
        skill_registry.scan()
        sparse = BM25SkillIndex(skill_registry)
        if skill_retriever not in {"bm25", "dense", "hybrid"}:
            raise ValueError(f"unknown Skill retriever: {skill_retriever}")
        if skill_retriever == "bm25":
            skill_index = sparse
        else:
            backend = skill_embedding_backend or OpenAIEmbeddingBackend()
            dense = DenseSkillIndex(skill_registry, backend, EmbeddingCache())
            skill_index = dense if skill_retriever == "dense" else HybridSkillIndex(sparse, dense)
        registry.register(SkillSearchTool(skill_index))
        registry.register(SkillLoadTool(skill_registry))
        registry.register(SkillReadResourceTool(skill_registry))
    executor = ToolExecutor(
        allowed_permissions
        if allowed_permissions is not None
        else set(Permission),
        registry,
    )
    durable_memory = memory_manager or MemoryManager(SQLiteMemoryStore())
    episodic_store = session_store or SQLiteSessionStore()
    if session is None and session_id is not None:
        session = episodic_store.load(session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
    return AgentLoop(
        llm=client,
        executor=executor,
        max_steps=max_steps,
        recorder=recorder,
        artifact_store=artifact_store,
        session=session,
        session_config=session_config,
        memory_manager=durable_memory,
        session_store=episodic_store,
        checkpoint_store=checkpoint_store or SQLiteCheckpointStore(),
        skill_registry=skill_registry,
        skill_index=skill_index,
        skills_enabled=skills_enabled,
        explicit_skills=explicit_skills,
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
    config = SessionConfig(temporary_chat=False, persist_trace=True)
    runtime_recorder = recorder if recorder is not None else RunRecorder(persist=config.persist_trace)
    agent = build_agent(client, recorder=runtime_recorder)
    try:
        print("Cortex 已启动（工具模式），输入 'exit' 退出。")
        while True:
            user_input = input("\n你: ")
            if user_input.strip().lower() in {"exit", "quit", "q"}:
                print("再见！")
                return
            print(f"Cortex: {agent.run(user_input)}")
    finally:
        close = getattr(agent, "close", None)
        if close is not None:
            close()
