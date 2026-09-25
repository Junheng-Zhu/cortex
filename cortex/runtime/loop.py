import time
from typing import Any
from uuid import uuid4

from .action import Action
from .observation import Observation
from cortex.llm.capabilities import ContextMode
from cortex.context.artifacts import ArtifactStore
from cortex.context.manager import ContextManager
from cortex.context.artifacts import ObservationPolicy
from cortex.observability.tracer import RunRecorder

from cortex.llm.protocol import LLMResponse, LLMUsage, ToolCall
from .recovery import Reflector
from .state import AgentPhase, AgentState
from .session import Session, SessionConfig
from cortex.memory.manager import MemoryManager
from cortex.memory.consolidation import MemoryConsolidator, ScopedMemoryCandidate
from cortex.memory.session_store import SessionEvent, SessionStore
from .checkpoint import Checkpoint, CheckpointStore


class AgentLoop:
    """DECIDE -> ACT -> OBSERVE -> REFLECT agent state machine."""

    def __init__(
        self,
        llm,
        executor,
        max_steps: int = 10,
        context_mode: ContextMode | None = None,
        recorder: RunRecorder | None = None,
        artifact_store: ArtifactStore | None = None,
        observation_policy: ObservationPolicy | None = None,
        context_manager: ContextManager | None = None,
        session: Session | None = None,
        session_config: SessionConfig | None = None,
        memory_manager: MemoryManager | None = None,
        session_store: SessionStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
        consolidator: MemoryConsolidator | None = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.llm = llm
        self.executor = executor
        self.max_steps = max_steps
        configured_context_mode = context_mode or getattr(
            getattr(llm, "capabilities", None),
            "context_mode",
            ContextMode.CLIENT_MANAGED,
        )
        self.context_mode = ContextMode(configured_context_mode)
        self.session_config = session_config or SessionConfig(persist_trace=False)
        self.session = session or Session.from_config(self.session_config)
        self.memory_manager = memory_manager
        self.session_store = session_store
        self.checkpoint_store = checkpoint_store
        self.consolidator = consolidator or MemoryConsolidator()
        self.recorder = recorder or RunRecorder(persist=self.session_config.persist_trace)
        self.reflector = Reflector()
        self.artifact_store = artifact_store or ArtifactStore()
        self.observation_policy = observation_policy or ObservationPolicy(
            self.artifact_store
        )
        self.context_manager = context_manager or ContextManager(
            memory_manager=memory_manager, session_store=session_store
        )
        if memory_manager is not None and self.context_manager.memory_manager is None:
            self.context_manager.memory_manager = memory_manager
        if session_store is not None and self.context_manager.session_store is None:
            self.context_manager.session_store = session_store
        self.last_state: AgentState | None = None

    def run(self, query: str) -> str | None:
        run = self.recorder.start_run()
        started = time.perf_counter()
        state = AgentState(
            run_id=run.run_id,
            session_id=self.session.session_id,
            max_steps=self.max_steps,
            pending_input=[{"role": "user", "content": query}],
            context_history=list(self.session.history),
            previous_response_id=self.session.previous_response_id,
            compact_summary=self.session.compact_summary,
            current_goal=query,
        )
        self.session.last_run_id = run.run_id
        self.last_state = state
        self._begin_episode(query, state)
        self._remember_candidates(self.consolidator.explicit(query, self.session.metadata), state)
        while state.phase is not AgentPhase.FINAL:
            self.step(state)
        # _finish owns semantic completion; run() owns wall-clock accounting.
        self.recorder.finish_run(
            success=getattr(state, "_run_success", False),
            steps=state.step_count,
            latency_ms=(time.perf_counter() - started) * 1000,
            termination_reason=getattr(state, "_termination_reason", "unknown"),
        )
        self.session.compact_summary = state.compact_summary
        self.session.history = list(state.context_history)
        for artifact_ref in state.artifact_references:
            if artifact_ref not in self.session.artifact_refs:
                self.session.artifact_refs.append(artifact_ref)
        self._persist_episode(state)
        self._remember_candidates(
            self.consolidator.after_run(
                query,
                state.final_answer or "",
                state.important_decisions,
                self.session.metadata,
            ),
            state,
        )
        return state.final_answer

    def resume(self, checkpoint_id: str | None = None) -> str | None:
        if self.checkpoint_store is None:
            raise RuntimeError("checkpoint_store is not configured")
        checkpoint = (
            self.checkpoint_store.load(checkpoint_id)
            if checkpoint_id
            else self.checkpoint_store.latest(self.session.session_id)
        )
        if checkpoint is None:
            raise KeyError("checkpoint not found")
        if checkpoint.session_id != self.session.session_id:
            raise ValueError("checkpoint belongs to a different session")
        run = self.recorder.start_run()
        started = time.perf_counter()
        state = checkpoint.restore(new_run_id=run.run_id, max_steps=self.max_steps)
        state.context_history = list(self.session.history)
        state.compact_summary = self.session.compact_summary
        state.previous_response_id = self.session.previous_response_id
        if not state.pending_actions:
            state.pending_input = [{"role": "user", "content": f"Continue the task: {state.current_goal}"}]
        self.session.last_run_id = run.run_id
        self.last_state = state
        while state.phase is not AgentPhase.FINAL:
            self.step(state)
        self.recorder.finish_run(
            success=getattr(state, "_run_success", False),
            steps=state.step_count,
            latency_ms=(time.perf_counter() - started) * 1000,
            termination_reason=getattr(state, "_termination_reason", "unknown"),
        )
        self.session.history = list(state.context_history)
        self.session.compact_summary = state.compact_summary
        self._persist_episode(state)
        self._remember_candidates(
            self.consolidator.after_run(
                state.current_goal or "",
                state.final_answer or "",
                state.important_decisions,
                self.session.metadata,
            ),
            state,
        )
        return state.final_answer

    def _remember_candidates(
        self, candidates: list[ScopedMemoryCandidate], state: AgentState
    ) -> None:
        if self.memory_manager is None:
            return
        for scoped in candidates:
            candidate = scoped.candidate
            self.memory_manager.remember(
                candidate.content,
                scoped.scope_type,
                scoped.scope_id,
                kind=candidate.kind,
                source=candidate.source,
                session_id=self.session.session_id,
                run_id=state.run_id,
                temporary_chat=self.session.temporary_chat,
            )

    def _persist_episode(self, state: AgentState) -> None:
        if self.session.temporary_chat or self.session_store is None:
            return
        if state.final_answer:
            self.session_store.append(
                SessionEvent(
                    self.session.session_id,
                    state.run_id,
                    "assistant",
                    state.final_answer,
                    metadata=self._episode_metadata(),
                )
            )
        self.session_store.save(self.session)

    def _begin_episode(self, query: str, state: AgentState) -> None:
        if self.session.temporary_chat or self.session_store is None:
            return
        self.session_store.append(
            SessionEvent(
                self.session.session_id,
                state.run_id,
                "user",
                query,
                metadata=self._episode_metadata(),
            )
        )
        self.session_store.save(self.session)

    def _episode_metadata(self, **values: Any) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "user_id": self.session.metadata.get("user_id"),
                "project_id": self.session.metadata.get("project_id"),
                **values,
            }.items()
            if value is not None
        }

    def step(self, state: AgentState) -> AgentState:
        if state.phase is AgentPhase.DECIDE:
            self.decide(state)
        elif state.phase is AgentPhase.ACT:
            self.act(state)
        elif state.phase is AgentPhase.OBSERVE:
            self.observe(state)
        elif state.phase is AgentPhase.REFLECT:
            self.reflect(state)
        return state

    def _finish(
        self,
        state: AgentState,
        answer: str,
        *,
        termination_reason: str = "completed",
        success: bool = True,
    ) -> None:
        state.final_answer = answer
        state.phase = AgentPhase.FINAL
        state._termination_reason = termination_reason
        state._run_success = success
        self.recorder.record(
            "final",
            content=answer,
            success=success,
            termination_reason=termination_reason,
        )

    def _request(self, state: AgentState) -> LLMResponse:
        if (
            not state.pending_input
            and state.messages
            and not state.context_history
            and self.session.previous_response_id is None
            and not state.actions
        ):
            state.pending_input = list(state.messages)

        request_input, previous_response_id = self.context_manager.build_context(
            state, self.context_mode, self.session
        )

        request_tools = self.executor.list_tool_schemas()
        started = time.perf_counter()
        if hasattr(self.llm, "respond"):
            response = self.llm.respond(
                input=request_input,
                tools=request_tools,
                previous_response_id=previous_response_id,
            )
        else:
            response = self.llm.chat(
                request_input,
                request_tools,
            )
        normalized = self._normalize_response(response)
        duration_ms = (time.perf_counter() - started) * 1000

        self.recorder.record(
            "llm_call",
            request={
                "input": request_input,
                "tools": request_tools,
                "previous_response_id": previous_response_id,
            },
            output_items=normalized.output_items,
            output_text=normalized.content,
            response_id=normalized.response_id,
            input_tokens=normalized.usage.input_tokens,
            output_tokens=normalized.usage.output_tokens,
            total_tokens=normalized.usage.total_tokens,
            duration_ms=duration_ms,
        )
        if self.context_mode is ContextMode.SERVER_MANAGED:
            self.session.previous_response_id = normalized.response_id
            state.previous_response_id = normalized.response_id
        else:
            durable_input = [item for item in request_input if item.get("name") != "cortex_memory"]
            state.context_history = [*durable_input, *normalized.output_items]
        state.pending_input.clear()
        return normalized

    @staticmethod
    def _normalize_response(response: Any) -> LLMResponse:
        if isinstance(response, LLMResponse):
            return response
        if response.get("type") == "tool_call":
            call = ToolCall(
                call_id=response.get("call_id", ""),
                name=response["name"],
                arguments=response.get("arguments", {}),
            )
            return LLMResponse(tool_calls=[call])
        return LLMResponse(content=response.get("content", ""), usage=LLMUsage())

    def decide(self, state: AgentState) -> None:
        if state.step_count >= state.max_steps:
            self._finish(
                state,
                "Maximum number of action steps reached.",
                termination_reason="max_steps",
                success=False,
            )
            return

        response = self._request(state)
        if response.tool_calls:
            for call in response.tool_calls:
                action = Action(
                    call_id=call.call_id or str(uuid4()),
                    tool_name=call.name,
                    arguments=call.arguments,
                )
                state.actions.append(action)
                state.pending_actions.append(action)
            state.phase = AgentPhase.ACT
            return

        self._finish(state, response.content)

    def act(self, state: AgentState) -> None:
        if not state.pending_actions:
            self._finish(
                state,
                "No pending action to execute.",
                termination_reason="no_pending_action",
                success=False,
            )
            return

        action = state.pending_actions[0]
        action.status = "RUNNING"
        started = time.perf_counter()
        try:
            state.last_tool_result = self.executor.execute(
                action.tool_name, action.arguments
            )
            action.status = "SUCCEEDED" if state.last_tool_result.success else "FAILED"
        except Exception as exc:
            action.status = "FAILED"
            state.last_tool_result = exc

        state.step_count += 1
        measured_duration_ms = (time.perf_counter() - started) * 1000
        self.recorder.record(
            "action",
            action_id=action.action_id,
            tool_name=action.tool_name,
            arguments=action.arguments,
            status=action.status,
            call_id=action.call_id,
            duration_ms=getattr(
                state.last_tool_result, "duration_ms", measured_duration_ms
            ),
            attempts=getattr(state.last_tool_result, "attempts", 1),
            validation_passed=getattr(
                state.last_tool_result, "validation_passed", None
            ),
            error_type=getattr(
                state.last_tool_result,
                "error_type",
                type(state.last_tool_result).__name__
                if isinstance(state.last_tool_result, Exception)
                else None,
            ),
        )
        state.phase = AgentPhase.OBSERVE

    def observe(self, state: AgentState) -> None:
        action = state.pending_actions.pop(0)
        result = state.last_tool_result
        if isinstance(result, Exception):
            observation = Observation(
                action.action_id,
                False,
                error=str(result),
                error_type=type(result).__name__,
            )
        else:
            if result.success:
                observation = self.observation_policy.success(
                    action.action_id, action.tool_name, result.data
                )
            else:
                observation = Observation(
                    action_id=action.action_id,
                    success=False,
                    error=result.error_message or result.error_type,
                    error_type=result.error_type,
                )

        state.observations.append(observation)
        if observation.artifact_ref:
            state.artifact_references.append(observation.artifact_ref)
        state.pending_input.append(
            {
                "type": "function_call_output",
                "call_id": action.call_id,
                "output": (
                    self.observation_policy.model_output(action.tool_name, observation)
                    if observation.success
                    else str(observation.error)
                ),
            }
        )
        self.recorder.record(
            "observation",
            action_id=observation.action_id,
            call_id=action.call_id,
            success=observation.success,
            output_preview=self.recorder.preview(observation.output),
            output_length=self.recorder.output_length(observation.output),
            artifact_ref=observation.artifact_ref,
            artifact_path=observation.artifact_path,
            size_chars=observation.size_chars,
            truncated=observation.truncated,
            error=observation.error,
            error_type=observation.error_type,
        )
        state.phase = AgentPhase.REFLECT
        if not self.session.temporary_chat and self.session_store is not None:
            self.session_store.append(
                SessionEvent(
                    self.session.session_id,
                    state.run_id,
                    "tool",
                    action.tool_name,
                    metadata=self._episode_metadata(
                        status=action.status,
                        artifact_ref=observation.artifact_ref,
                    ),
                )
            )
            self.session.history = list(state.context_history)
            self.session.compact_summary = state.compact_summary
            self.session_store.save(self.session)
        if not self.session.temporary_chat and self.checkpoint_store is not None:
            self.checkpoint_store.save(Checkpoint.capture(state))

    def reflect(self, state: AgentState) -> None:
        reflection = self.reflector.reflect(state.observations[-1])
        state.reflections.append(reflection)
        self.recorder.record(
            "reflection",
            action_id=state.observations[-1].action_id,
            status=reflection.status,
            summary=reflection.summary,
            next_hint=reflection.next_hint,
        )
        if reflection.status == "ABORT":
            self._finish(
                state,
                reflection.summary,
                termination_reason="tool_error",
                success=False,
            )
        elif reflection.status == "REPLAN":
            state.pending_actions.clear()
            state.phase = AgentPhase.DECIDE
        elif state.pending_actions:
            state.phase = AgentPhase.ACT
        elif state.step_count >= state.max_steps:
            self._finish(
                state,
                "Maximum number of action steps reached.",
                termination_reason="max_steps",
                success=False,
            )
        else:
            state.phase = AgentPhase.DECIDE


Agent = AgentLoop
