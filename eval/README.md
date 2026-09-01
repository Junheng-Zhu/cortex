# Cortex Eval Gate

## 目的

把 Agent 的关键行为变成可自动检查的门禁，而不是只看“最后回答像不像”。

Cortex 的 CI 门禁分成两层：

1. **Deterministic Smoke Eval**：不需要 API Key，验证工具行为、文件发现、内容读取、错误处理和路径安全。
2. **Online Agent Eval（下一阶段）**：使用真实 LLM，验证完整的 Agent 工具调用轨迹。

当前 CI 默认只运行第一层，因此不会消耗模型 API，也不会要求 GitHub Secrets。

## 当前检查

当前 Trace Eval 主要检查：

1. `user_input / llm_call / tool_call / response` 事件链完整。
2. 工具调用序列符合测试 case。
3. `read_note` 使用正确参数名 `filename`。
4. 工具调用数量不超过 case 上限。
5. Agent 的 `tool_round` 不超过 6。
6. 每轮 LLM 有 `prompt_tokens` 和 `completion_tokens`。
7. 最终回答包含 case 要求的事实。

## 当前黄金路径

```text
用户请求
    ↓
list_notes()
    ↓
发现 calendar.txt
    ↓
read_note(filename="calendar.txt")
    ↓
验证会议 / PPT / 开会
    ↓
最终回答
```

## 本地运行

### 1. Deterministic Smoke Eval

不需要 API Key：

```bash
python eval/smoke_eval.py
```

### 2. Trace Eval

先运行 Cortex 完成一次测试，再执行：

```bash
python eval/trace_eval.py --db .trace.db
```

指定会话：

```bash
python eval/trace_eval.py --db .trace.db --session-id 801fbf85
```

> `801fbf85` 仅为示例，请替换为实际的 session ID。

## CI

CI 运行失败时，GitHub Actions 会阻止对应的 Check 通过。

当前 CI 默认运行 Deterministic Smoke Eval，不需要模型 API Key，也不会消耗模型 API。

Online Agent Eval 属于下一阶段能力，后续可以接入真实 LLM 和 GitHub Secrets。

## 退出码

- `0`：PASS，可继续。
- `1`：FAILED，阻止通过门禁。
- `2`：Eval 环境或配置错误。

## 测试数据

Eval case 和门禁阈值位于：

```text
eval/
├── README.md
├── cases.json
├── thresholds.json
├── smoke_eval.py
├── trace_eval.py
└── eval_gate.sh
```

其中：

- `cases.json`：定义测试 case、预期工具调用和最终回答要求。
- `thresholds.json`：定义工具调用数量、`tool_round` 等门禁阈值。
- `smoke_eval.py`：执行确定性 Smoke Eval。
- `trace_eval.py`：分析 Agent Trace 并执行行为门禁。
- `eval_gate.sh`：CI 中统一执行 Eval Gate。

## 门禁原则

Eval Gate 关注的是 **Agent 行为是否符合预期**，而不仅仅是最终答案是否正确。

核心验证链路：

```text
输入
 ↓
LLM 调用
 ↓
Tool Call
 ↓
Tool Result
 ↓
下一轮 LLM
 ↓
最终 Response
```

通过对 Trace 进行结构化检查，可以发现：

- 工具没有被正确调用；
- 工具参数名称错误；
- 工具调用次数异常；
- Agent 工具循环过深；
- Token 使用信息缺失；
- 最终回答缺少关键事实。

这使 Eval Gate 可以作为 Cortex 持续集成中的自动质量门禁。
