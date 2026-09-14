class Reflector:

    def reflect(self, state):

        result = state.last_tool_result

        if result is None:
            return False

        # Tool 成功：
        # 把 Observation 给模型，让模型决定任务是否完成
        if result.success:
            return True

        # Tool 失败：
        # 只要还有运行预算，也允许模型尝试恢复
        if state.step_count < state.max_steps:
            return True

        return False
