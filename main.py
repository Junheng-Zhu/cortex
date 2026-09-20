from src.core.models import LLMClient
from src.core.loop import run_loop


def main() -> None:
    client = LLMClient()
    run_loop(client)


if __name__ == "__main__":
    main()
