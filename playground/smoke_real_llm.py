import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.core.loop import build_agent
from src.core.models import LLMClient


def main() -> None:
    query = " ".join(sys.argv[1:]) or "请列出当前的笔记文件。"
    client = LLMClient()
    agent = build_agent(client)
    print(agent.run(query))


if __name__ == "__main__":
    main()
