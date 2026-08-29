import sys
import os
# 将项目根目录添加到 Python 路径（以便 import cortex）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

""" from cortex.core.models import LLMClient
from cortex.core.loop import run_loop

def main():
    client = LLMClient()
    run_loop(client)

if __name__ == "__main__":
    main() """

print("1. 开始导入模块...")
from src.core.models import LLMClient
print("2. 导入 models 成功")
from src.core.loop import run_loop
print("3. 导入 loop 成功")

def main():
    print("4. 创建 LLMClient...")
    client = LLMClient()
    print("5. LLMClient 创建成功")
    run_loop(client)

if __name__ == "__main__":
    print("6. 进入 main...")
    main()