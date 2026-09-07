import sys
import os
import time

# 允许从项目根目录导入 src
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from pydantic import BaseModel

from src.tools.base import Tool
from src.tools.permission import Permission
from src.tools.registry import ToolRegistry
from src.tools.executor import ToolExecutor


# ---------- 测试输入模型 ----------

class EmptyInput(BaseModel):
    """本次实验的 Tool 不需要参数。"""
    pass


# ---------- 测试 Tool ----------

class SlowTool(Tool):
    name = "slow_tool"
    description = "一个故意执行很慢的 Tool，用来测试 timeout。"
    input_model = EmptyInput
    permission = Permission.READ

    def execute(self, input):
        print("[SlowTool] 开始执行")
        time.sleep(3)
        print("[SlowTool] 执行结束")
        return "slow success"


class FlakyTool(Tool):
    name = "flaky_tool"
    description = "第一次执行超时，第二次执行成功，用来测试 retry。"
    input_model = EmptyInput
    permission = Permission.READ

    def __init__(self):
        self.attempts = 0

    def execute(self, input):
        self.attempts += 1
        print(f"[FlakyTool] attempt={self.attempts}")

        if self.attempts == 1:
            # 用 sleep 模拟第一次请求超时
            time.sleep(3)

        return "flaky success"


# ---------- 工具初始化 ----------

registry = ToolRegistry()
registry.register(SlowTool())
registry.register(FlakyTool())

# 当前 executor.py 的构造函数虽然接收 timeout，
# 但目前内部实际上写死成了 5 秒。
# 这里故意传 1 秒，用来验证这个 bug。
executor = ToolExecutor(
    {Permission.READ},
    registry
    
)


# ============================================================
# Test 1: Timeout
# ============================================================

print("\n========== Test 1: Timeout ==========")

start = time.perf_counter()

try:
    result = executor.execute("slow_tool", {})
    print("[ERROR] SlowTool 居然没有超时，result =", result)
except Exception as e:
    elapsed = time.perf_counter() - start
    print("捕获异常:", type(e).__name__)
    print("耗时:", round(elapsed, 2), "秒")


# ============================================================
# Test 2: Retry
# ============================================================

print("\n========== Test 2: Retry ==========")

start = time.perf_counter()

try:
    result = executor.execute("flaky_tool", {})
    elapsed = time.perf_counter() - start

    print("最终结果:", result)
    print("总耗时:", round(elapsed, 2), "秒")

    flaky_tool = registry.get("flaky_tool")
    print("总执行次数:", flaky_tool.attempts)

except Exception as e:
    print("Retry 测试最终抛出:", type(e).__name__)
    print("错误:", e)


# ============================================================
# 你需要观察：
#
# 1. timeout=1 是否真的 1 秒左右返回？
# 2. 如果第一次 timeout，FlakyTool 是否真的进行了第二次执行？
# 3. ThreadPoolExecutor 的 with 是否导致 timeout 后仍等待？
# 4. SlowTool 在 Executor 报 timeout 后，后台是否还继续执行？
# ============================================================

""" ========== Test 1: Timeout ==========
[SlowTool] 开始执行
[SlowTool] 执行结束
[ERROR] SlowTool 居然没有超时，result = slow success

========== Test 2: Retry ==========
[FlakyTool] attempt=1
最终结果: flaky success
总耗时: 3.01 秒
总执行次数: 1 """

""" 
这里我删除了executor初始化的timeout参数，直接让他写死，之前你提到的增加tool类的matadata比较好
========== Test 1: Timeout ==========
[SlowTool] 开始执行
[SlowTool] 执行结束
[SlowTool] 开始执行
[SlowTool] 执行结束
[ERROR] SlowTool 居然没有超时，result = None

========== Test 2: Retry ==========
[FlakyTool] attempt=1
[FlakyTool] attempt=2
最终结果: flaky success
总耗时: 3.01 秒
总执行次数: 2
 """