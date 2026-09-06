import concurrent.futures
import time

def long_running_task():
    time.sleep(1)
    return "任务完成"

with concurrent.futures.ThreadPoolExecutor() as executor:
    future = executor.submit(long_running_task)

    try:
        # 只等待 2 秒，但任务需要 10 秒，必定超时
        result = future.result(timeout=2)
        print(result)
    except concurrent.futures.TimeoutError:
        print("捕获到超时异常！主线程不再等待了。")
        # 注意：此时 long_running_task 依然在后台运行，直到 10 秒后自己结束。
        print(f"任务是否还在运行？{future.running()}")  # 可能输出 True

        """捕获到超时异常！主线程不再等待了。
任务是否还在运行？True  """