# Cortex Eval Gate

Cortex 的 CI 门禁分成两层：

1. **Deterministic smoke Eval**：不需要 API Key，验证工具行为、文件发现、内容读取、错误处理和路径安全。
2. **Online Agent Eval（下一阶段）**：使用真实 LLM，验证完整的 Agent 工具调用轨迹。

当前 CI 默认只运行第一层，因此不会消耗模型 API，也不会要求 GitHub Secrets。

本地运行：

    python eval/smoke_eval.py

CI 运行失败时，GitHub Actions 会阻止对应的 Check 通过。

当前日程 Gold Case：

    用户请求
      ↓
    list_notes()
      ↓
    发现 calendar.txt
      ↓
    read_note("calendar.txt")
      ↓
    验证会议 / PPT / 开会
