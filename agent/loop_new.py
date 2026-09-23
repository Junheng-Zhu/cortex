import time
from typing import Any
from uuid import uuid4

from runtime.action import Action
from runtime.observation import Observation
from src.core.models import ContextMode
from src.ops.tracer import RunRecorder

from .models import LLMResponse, LLMUsage, ToolCall
from .reflection import Reflector
from .state_new import AgentPhase, AgentState


class AgentLoop:
    """DECIDE -> ACT -> OBSERVE -> REFLECT agent state machine."""

    def __init__(
        self,
        llm,
        executor,
        max_steps: int = 10,
        context_mode: ContextMode | None = None,
        recorder: RunRecorder | None = None,
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
        self.recorder = recorder or RunRecorder()
        self.reflector = Reflector()
        self.last_state: AgentState | None = None

    def run(self, query: str) -> str | None:
        self.recorder.start_run()
        started = time.perf_counter()
        state = AgentState(
            max_steps=self.max_steps,
            pending_input=[{"role": "user", "content": query}],
        )
        self.last_state = state
        while state.phase is not AgentPhase.FINAL:
            self.step(state)
        # _finish owns semantic completion; run() owns wall-clock accounting.
        self.recorder.finish_run(
            success=getattr(state, "_run_success", False),
            steps=state.step_count,
            latency_ms=(time.perf_counter() - started) * 1000,
            termination_reason=getattr(state, "_termination_reason", "unknown"),
        )
        return state.final_answer

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
            and state.previous_response_id is None
            and not state.actions
        ):
            state.pending_input = list(state.messages)

        if self.context_mode is ContextMode.SERVER_MANAGED:
            request_input = list(state.pending_input)
            previous_response_id = state.previous_response_id
        else:
            request_input = [*state.context_history, *state.pending_input]
            previous_response_id = None

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
            state.previous_response_id = normalized.response_id
        else:
            state.context_history = [*request_input, *normalized.output_items]
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
            observation = Observation(
                action_id=action.action_id,
                success=result.success,
                output=result.data if result.success else None,
                error=(
                    None
                    if result.success
                    else result.error_message or result.error_type
                ),
                error_type=None if result.success else result.error_type,
            )

        state.observations.append(observation)
        state.pending_input.append(
            {
                "type": "function_call_output",
                "call_id": action.call_id,
                "output": str(
                    observation.output if observation.success else observation.error
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
            error=observation.error,
            error_type=observation.error_type,
        )
        state.phase = AgentPhase.REFLECT

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
