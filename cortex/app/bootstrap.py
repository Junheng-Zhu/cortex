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
from cortex.runtime.execution_checkpoint import (
    ExecutionCheckpointStore, MutationLedger, SQLiteExecutionCheckpointStore,
    WorkspaceRecoveryRuntime,
)
from cortex.runtime.workspace import ShadowGitSnapshotStore
from cortex.runtime.workspace_session import WorkspaceManager, WorkspaceSession, WorkspaceMode
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
    workspace_mode: WorkspaceMode | str = WorkspaceMode.DIRECT,
    workspace_manager: WorkspaceManager | None = None,
    workspace_session: WorkspaceSession | None = None,
    workspace_owner_id: str = "user",
    max_tool_concurrency: int = 4,
    distributed_execution_client=None,
    distributed_tool_names: set[str] | None = None,
    workspace_recovery_enabled: bool = True,
    workspace_recovery_runtime: WorkspaceRecoveryRuntime | None = None,
    workspace_snapshot_store: ShadowGitSnapshotStore | None = None,
    execution_checkpoint_store: ExecutionCheckpointStore | None = None,
    mutation_ledger: MutationLedger | None = None,
    workspace_snapshot_root: str | Path | None = None,
) -> AgentLoop:
    """Build the runtime agent with the note tools supported by this app."""
    registry = ToolRegistry()
    workspace = Path(execution_workspace or PROJECT_ROOT).resolve()
    mode = WorkspaceMode(workspace_mode)
    manager = workspace_manager
    if workspace_session is not None:
        if manager is None:
            raise ValueError("workspace_session requires its WorkspaceManager")
        manager.validate(workspace_session)
        workspace = Path(workspace_session.execution_root)
        mode = workspace_session.mode
    else:
        manager = manager or WorkspaceManager((workspace_snapshot_root if mode is WorkspaceMode.ISOLATED else None) or (workspace.parent / ".cortex-runtime-v2"))
        workspace_session = manager.create(workspace, mode=mode, owner_id=workspace_owner_id)
        workspace = Path(workspace_session.execution_root)
    if mode is WorkspaceMode.ISOLATED and not workspace_recovery_enabled:
        raise ValueError("isolated execution requires workspace recovery")
    isolated_storage = manager.root / ("state-" + workspace_session.session_id) if mode is WorkspaceMode.ISOLATED else None
    artifact_store = ArtifactStore(isolated_storage / "artifacts") if isolated_storage else ArtifactStore()
    if isolated_storage:
        # Explicitly injected persistent stores must also respect the main boundary.
        for component in (checkpoint_store, execution_checkpoint_store, mutation_ledger,
                          session_store, getattr(memory_manager, "store", None), recorder):
            location = getattr(component, "path", None) or getattr(component, "db_path", None)
            if location and str(location) != ":memory:" and Path(location).resolve().is_relative_to(Path(workspace_session.main_root)):
                raise ValueError("isolated runtime persistence must be outside main_root")
    registry.register(ListNotesTool(workspace))
    registry.register(ReadNoteTool(workspace))
    registry.register(DeleteNoteTool(workspace))
    registry.register(SlowTool())
    if execution_backend is None or execution_backend == "local":
        shell_backend = LocalBackend(discover_bash, MAX_OUTPUT_CHARS, workspace, isolated=mode is WorkspaceMode.ISOLATED)
    elif execution_backend == "docker":
        shell_backend = DockerBackend(DockerBackendConfig(workspace=workspace, isolated=mode is WorkspaceMode.ISOLATED))
    elif isinstance(execution_backend, ExecutionBackend):
        shell_backend = execution_backend
        if mode is WorkspaceMode.ISOLATED:
            if isinstance(shell_backend, LocalBackend):
                if shell_backend.workspace != workspace or not shell_backend.isolated:
                    raise ValueError("isolated backend must be sandboxed and bound to execution_root")
            elif isinstance(shell_backend, DockerBackend):
                if shell_backend.config.workspace.resolve() != workspace or not shell_backend.config.isolated:
                    raise ValueError("Docker workspace mismatch")
            else:
                raise ValueError("custom backends are not validated for isolated execution")
    else:
        raise ValueError("execution_backend must be 'local', 'docker', or an ExecutionBackend")
    if isinstance(shell_backend, LocalBackend) and shell_backend.workspace != workspace:
        raise ValueError("Local backend workspace mismatch")
    if isinstance(shell_backend, DockerBackend) and shell_backend.config.workspace.resolve() != workspace:
        raise ValueError("Docker backend workspace mismatch")
    registry.register(ShellTool(shell_backend, workspace))
    registry.register(ReadArtifactChunkTool(artifact_store))
    skill_registry = None
    skill_index = None
    if skills_enabled:
        registry_options = {"index_body": skill_index_body}
        if isolated_storage:
            registry_options["snapshot_root"] = isolated_storage / "skill-snapshots"
        if skill_snapshot_root is not None:
            registry_options["snapshot_root"] = Path(skill_snapshot_root)
        skill_registry = SkillRegistry(
            [Path(path) for path in (skill_roots if skill_roots is not None else [(workspace if isolated_storage else Path.cwd()) / "skills"])],
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
    agent_checkpoint_store = checkpoint_store or (SQLiteCheckpointStore(isolated_storage / "agents.db") if isolated_storage else SQLiteCheckpointStore())
    runtime_recorder = recorder or RunRecorder(
        persist=(session_config or SessionConfig()).persist_trace,
        db_path=isolated_storage / "trace.db" if isolated_storage else None,
    )
    recovery = workspace_recovery_runtime
    if recovery is not None and recovery.snapshots.workspace != workspace:
        raise ValueError("workspace recovery runtime does not match execution_workspace")
    if recovery is None and workspace_recovery_enabled:
        snapshots = workspace_snapshot_store or ShadowGitSnapshotStore(
            workspace, (manager.root / "snapshots") if isolated_storage else workspace_snapshot_root
        )
        if snapshots.workspace != workspace:
            raise ValueError("workspace snapshot store does not match execution_workspace")
        recovery = WorkspaceRecoveryRuntime(
            snapshots,
            agent_checkpoint_store,
            execution_checkpoint_store or SQLiteExecutionCheckpointStore(
                (isolated_storage / "executions.db") if isolated_storage else Path.cwd() / ".cortex" / "execution_checkpoints.db"
            ),
            mutation_ledger,
            runtime_recorder,
        )
    executor_type = ToolExecutor
    distributed_options = {}
    if distributed_execution_client is not None:
        from cortex.distributed.executor import DistributedToolExecutor
        if not distributed_tool_names:
            raise ValueError('distributed execution requires explicit distributed_tool_names')
        executor_type = DistributedToolExecutor
        distributed_options = dict(distributed_client=distributed_execution_client,
                                   distributed_tools=distributed_tool_names)
    elif distributed_tool_names:
        raise ValueError('distributed_tool_names requires distributed_execution_client')
    executor = executor_type(
        allowed_permissions
        if allowed_permissions is not None
        else set(Permission),
        registry,
        max_tool_concurrency=max_tool_concurrency,
        recovery_runtime=recovery,
        **distributed_options,
    )
    durable_memory = memory_manager or MemoryManager(SQLiteMemoryStore(isolated_storage / "memory.db") if isolated_storage else SQLiteMemoryStore())
    episodic_store = session_store or (SQLiteSessionStore(isolated_storage / "sessions.db") if isolated_storage else SQLiteSessionStore())
    if session is None and session_id is not None:
        session = episodic_store.load(session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
    if session is None and workspace_session is not None:
        session = Session.from_config(session_config or SessionConfig(), session_id=workspace_session.session_id)
    agent = AgentLoop(
        llm=client,
        executor=executor,
        max_steps=max_steps,
        recorder=runtime_recorder,
        artifact_store=artifact_store,
        session=session,
        session_config=session_config,
        memory_manager=durable_memory,
        session_store=episodic_store,
        checkpoint_store=agent_checkpoint_store,
        skill_registry=skill_registry,
        skill_index=skill_index,
        skills_enabled=skills_enabled,
        explicit_skills=explicit_skills,
        workspace_recovery_runtime=recovery,
    )

    agent.workspace_manager = manager
    agent.workspace_session = workspace_session
    if workspace_session:
        executor.workspace_guard = lambda: manager.validate(workspace_session)
        from cortex.runtime.workspace import WorkspaceWriteLock
        executor.workspace_lock = WorkspaceWriteLock.for_workspace(workspace)
        if recovery is not None:
            recovery.workspace_guard = executor.workspace_guard
            recovery.workspace_session = workspace_session
            recovery.workspace_manager = manager
    return agent


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
