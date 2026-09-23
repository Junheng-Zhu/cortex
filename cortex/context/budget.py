from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBudget:
    """Character budget used before a provider tokenizer is available."""
    max_chars: int = 60_000

    def exceeded(self, items: list[dict]) -> bool:
        return sum(len(str(item)) for item in items) > self.max_chars
