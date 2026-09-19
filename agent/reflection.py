from runtime.observation import Observation
from runtime.reflection import ReflectionResult


class Reflector:
    def reflect(self, observation: Observation) -> ReflectionResult:
        """Reflect on a tool observation without making another LLM call."""
        # Accept an AgentState during the migration from the old reflector API.
        if not isinstance(observation, Observation):
            observations = getattr(observation, "observations", [])
            if not observations:
                return ReflectionResult(
                    status="FAILED",
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

        return ReflectionResult(
            status="FAILED",
            summary=observation.error or "Tool execution failed.",
            next_hint=None,
        )
