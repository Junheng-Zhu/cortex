from runtime.observation import Observation
from runtime.reflection import ReflectionResult


class Reflector:
    RECOVERABLE_ERRORS = {
        "ToolFileNotFoundError",
        "ToolNotFoundError",
        "ToolSandboxError",
        "ToolTimeoutError",
        "ToolValidationError",
    }

    def reflect(self, observation: Observation) -> ReflectionResult:
        """Reflect on a tool observation without making another LLM call."""
        # Accept an AgentState during the migration from the old reflector API.
        if not isinstance(observation, Observation):
            observations = getattr(observation, "observations", [])
            if not observations:
                return ReflectionResult(
                    status="ABORT",
                    summary="No observation is available to reflect on.",
                    next_hint=None,
                )
            observation = observations[-1]

        if observation.success:
            return ReflectionResult(
                status="CONTINUE",
                summary="Tool execution succeeded.",
                next_hint="Use the observation to decide the next step.",
            )

        if observation.error_type in self.RECOVERABLE_ERRORS:
            return ReflectionResult(
                status="REPLAN",
                summary=observation.error or "Tool execution failed.",
                next_hint="Revise the plan using the failed observation and available tools.",
            )

        return ReflectionResult(
            status="ABORT",
            summary=observation.error or "Tool execution failed.",
            next_hint=None,
        )
