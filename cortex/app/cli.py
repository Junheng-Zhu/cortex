from cortex.llm.client import LLMClient

from .bootstrap import run_loop


def main() -> None:
    run_loop(LLMClient())
