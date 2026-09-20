from agent.loop_new import AgentLoop
from src.core.loop import DEFAULT_INSTRUCTIONS, build_default_executor
from src.core.models import OpenAIResponsesClient


def main() -> None:
    client = OpenAIResponsesClient()
    agent = AgentLoop(
        llm=client,
        executor=build_default_executor(),
        max_steps=3,
        instructions=DEFAULT_INSTRUCTIONS,
    )

    answer = agent.run(
        "请读取 python.md，然后用两句话概括这个文件的主要内容。"
    )
    print(answer)


if __name__ == "__main__":
    main()
