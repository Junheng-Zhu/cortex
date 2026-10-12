# Distributed Sub-Agent Runtime V1 — AgentTask Delegation & Execution

V1 在 [Distributed Worker Runtime V1](distributed-worker-runtime-v1.md) 上增加独立 Child AgentLoop 执行。Parent 通常运行在 Host，通过现有 ToolRegistry / ToolExecutor 中显式注册的 `delegate_task` 提交任务，立即得到 task_id；Parent 自己决定何时等待和如何汇总。没有自动拆解、Planner 或 LLM Result Aggregator。

## 职责与执行链路

```text
Parent AgentLoop / Host
    -> DistributedExecutionClient.submit_agent
    -> ExecutionStore.create（幂等）+ Redis Streams.publish
    -> WorkerRuntime.acquire（Lease + attempt + fencing token）
    -> 校验授权和工具副作用策略
    -> ExecutionStore.start + 持久化无副作用执行证据
    -> build_agent -> 独立 Session / AgentState -> AgentLoop.arun
    -> ToolExecutor -> Observation -> 既有 Step Budget / Reflect
    -> ExecutionStore.finish（结构化结果，检查 fencing/deadline）
    -> Redis Streams ACK
    -> Parent wait_agent / delegate_task(wait) 读取持久化终态
```

**逻辑 AgentTask** 是 `ExecutionTask(task_type=AGENT, agent=AgentTaskSpec)`，不是新的存储或状态机。`task_id` 是 `execution_id` 的别名。Spec 保存 `parent_run_id`、goal、显式 JSON context、canonical `child_session_id`、allowed_tools、Parent 授权快照、max_steps 和绝对 deadline。状态、attempt、result/error 仍属于 ExecutionTask。

**Execution Attempt** 是一次 Worker ownership 获取，使用已有 attempt 计数、Lease 和单调 fencing token。恢复重试保持 task_id 与 child_session_id 不变，但从空上下文构建全新的 Child 实例和 run_id。它不会加载上一 attempt 的对话、Memory、工具结果或 checkpoint。

**Child AgentLoop** 使用已有 `build_agent()` 和 `AgentLoop.arun()`，执行完整 DECIDE → ACT → OBSERVE → REFLECT 生命周期。唯一额外 Loop 选项 `decision_in_thread=True` 把同步 LLM 决策移出 Worker event loop；本地默认行为保持不变。ToolExecutor 的波次调度继续收到 AgentState execution context，以在实际 Tool 调用前验证 ownership。

Worker 使用有界 Child 并发（默认 4，`max_concurrent_agents`），收到 Agent 消息后继续消费队列。Child 借用 Worker 的工具和监督 backend，并共用 ToolExecutor semaphore / SERIAL lock；Child 结束不会关闭其他 Child 使用的工具。ToolTask 仍沿用 Worker V1 的顺序处理路径。Parent 采用提交后显式等待，提交调用不会等待整个 Child 完成；模型等待默认仅 1 秒，可在后续调用继续等待。

## Host API 与模型工具

```python
from cortex.distributed import DistributedExecutionClient
from cortex.tools.permission import Permission

client = DistributedExecutionClient(queue, store,
    agent_registry=parent.executor.registry,
    agent_allowed_permissions=parent.executor.allowed_permissions,
    agent_allowed_tools={'list_notes', 'read_note'})  # 可选 Host 名称上限

task_id = client.submit_agent(
    parent_run_id=parent_run_id,
    goal='Find the relevant note and answer the question',
    context={'question': '...'},
    allowed_tools=['list_notes', 'read_note'],
    idempotency_key='find-note-1',
    max_steps=4,
    deadline=store.now() + 60,
)
status = client.get_agent_status(task_id)  # ExecutionTask
result = await client.wait_agent(task_id, timeout=5)  # AgentTaskResult
client.cancel_agent(task_id)  # 幂等；已有终态保持不变
```

`submit_agent` / `get_agent_status` / `cancel_agent` 是同步 Host API；`asubmit_agent` 是异步提交。`wait_agent` 是异步等待，TimeoutError 只表示本次等待结束，不会取消任务。`wait_agent(..., timeout=0)` 可读取已有终态。终态结果包括 task_id、terminal_state、final_answer、error 和 statistics（attempt、child_session_id、steps、duration_ms、termination_reason、total_tokens、token_limit_enforced）。取消或恢复失败时，无法可靠取得的统计为 null。

提交的幂等键按 `agent:{parent_run_id}:{idempotency_key}` 隔离。同一 Parent run 的重复请求返回同一个 task_id；不同 goal/context/权限/budget/deadline/max_attempts 复用同一键会报告冲突。自动生成的 Child session ID 不参与请求 fingerprint；canonical ID 由首次持久化创建决定。Redis 中保留 execution 与幂等记录，V1 不支持主动删除或 trim 活跃记录。

模型工具需要显式 opt-in：

```python
parent = build_agent(llm, agent_delegation_client=client)
```

这注册 `delegate_task`，受 `Permission.DELEGATE` 控制。每次调用按该 Parent 的实际 ToolRegistry 和当前 allowed_permissions 绑定授权；若传入的 client 已设置 Host 授权上限，还会取两者的交集。模型参数不能扩大这个范围。

模型提交参数示例：

```json
{
  "operation": "submit",
  "goal": "Answer the supplied question",
  "context_json": "{\"question\":\"...\"}",
  "allowed_tools": ["read_note"],
  "idempotency_key": "question-1",
  "max_steps": 4,
  "deadline": null
}
```

提交返回 `{"task_id":"..."}`。模型以 `{"operation":"wait","task_id":"...","timeout":1}` 显式等待；未完成时返回 `waiting: true`，完成时返回 AgentTaskResult。context_json 使用字符串以适配现有 Responses strict schema，必须解码为 JSON 对象。cancel/status 是 Host API，V1 不增加额外模型工具。`delegate_task` 本身有持久化提交副作用、不可自动重试，Child 从不注册它。

## 隔离、权限与执行限制

- Child 的 Session 和 AgentState 全新，初始输入只有 goal 和显式 context。没有 Parent history、previous_response_id、compact_summary、Tool Results 或 Memory 引用。Child 使用 temporary chat、独立空内存 Memory/Session/Checkpoint store，不读取默认 SQLite 持久记忆。
- Child artifact storage 与 WorkspaceManager metadata 位于 attempt 临时目录。Workspace 使用现有 DIRECT Runtime，仅绑定 Worker 配置的执行目录；不创建新 Workspace 引擎、Apply 或 Merge 流程。授权的读取工具仍可能访问 Worker 绑定的工作区文件；对话隔离不等于隐藏所有工作区文件。
- Host 校验 allowed_tools 是 Parent 实际授权工具的子集。Worker 再按自己的可信 ToolRegistry、allowed_permissions 与 SideEffectPolicy 校验。Child 的 schema 只包含 allowed_tools；实际 Tool 调用也执行白名单和权限检查，因此直接构造未公开 Tool 名称无法绕过。
- 普通 AgentTask 仅允许 `SideEffectPolicy.NONE`，不接受 workspace mutation binding。shell、delete_note、外部写入或补偿/不可逆工具均不能通过它开放。运行中的注册工具还会再次检查 Worker 策略。SideEffectPolicy 是可信工具作者/Host 的声明，不由模型推断。
- V1 拒绝 Worker 中绑定其他 runtime context 的 `read_artifact_chunk` 和 `skill_*`。这些工具需要未来版本提供 Child 专属绑定后才可委派。其他自定义 NONE 工具也应只包含可授权给 Child 的数据能力；不实现任意 Python 工具的安全沙箱或多租户认证。
- max_steps 直接使用现有 AgentLoop action budget，包括一次响应中的多个 Tool calls；剩余 action 额度耗尽后不启动新的 Tool。
- deadline 是绝对 Unix timestamp，以 Redis ExecutionStore 的时钟检查。Worker 在启动、监督期间、每次 Tool 启动前及结果提交前检查；Redis finish 在同一 fencing 原子操作内拒绝过期成功结果。过期任务返回 FAILED / DEADLINE_EXCEEDED。
- cancel 使用已有 ExecutionStore.cancel，持久化 CANCELLED 终态并撤销 ownership。Worker 最多约 100 ms 一次检查，取消 Child coroutine；每次 Tool 的真实启动边界也验证 Lease，阻止后续调用。现有 ToolExecutor 的 async cancellation 路径负责终止 process-supervised Tool / 取消 backend-supervised Tool，后者清理由 backend 自身负责。
- 同步 LLM 请求在独立线程中执行。取消/过期可停止 AgentLoop 后续执行和 Tool 调用，但 Python 无法强制终止已经发出的同步 LLM HTTP 请求；迟到响应不会恢复 Child 或提交有效成功结果。Host 必须为客户端配置合理请求超时。已发生的外部费用不撤销。
- 记录客户端报告的累计 token usage；`token_limit_enforced=false`。不实现 token 上限、精确分布式配额或层级分配，也不把缺失 usage 当作可靠配额证据。

## 故障恢复与安全边界

继续使用 Redis Streams PEL、WorkerRegistry heartbeat、ExecutionStore Lease/Fencing 与 RecoveryController，没有新的队列、Scheduler 或状态机。

| 故障位置 | 恢复行为 |
| --- | --- |
| create/publish/queued 间隙 | 既有 orphan 扫描补交付；幂等 execution 不重建 |
| QUEUED / CLAIMED 未启动 | 到期后通过既有 LOST → RECOVERING → QUEUED 安全交接 |
| RUNNING 且有 Worker 持久化的 `agent_replay_safe=true` | 工具已验证均 NONE，允许在 max_attempts/deadline 内从头运行完整 Child |
| RUNNING 缺少无副作用证明，或策略不允许 | FAILED / SIDE_EFFECT_UNCERTAIN，明确 uncertain，不自动重放 |
| 过期 ownership 的旧 Worker 尝试提交 | fenced store.finish 拒绝；保留 PEL 给 RecoveryController |
| 结果已持久化、ACK 前崩溃 | 恢复只 ACK 终态，不重新运行 Child |
| 已取消任务/重复终态消息 | 不执行新 AgentLoop；重复取消/交付保持 canonical 终态 |

Worker 在 RUNNING 后、任何 Child LLM/Tool 调用前写入无副作用证明。若在 start 和该写入之间崩溃，V1 保守失败，不假装知道执行进度。NONE 允许重放的是本地 Agent/Tool 的执行安全性；LLM 服务可能再次计费、回答也可能不同。只允许一个 canonical 终态，消息交付与 attempt 本身仍是 at-least-once。

没有 Mid-Loop Checkpoint Resume，不从最后一次 LLM/Tool 调用继续。恢复逻辑不调用模型判断幂等性，也不扩大现有 Workspace rollback 能力。

## Fake LLM + 真实 Redis 的最小手动验证

安装 requirements 并启动本机真实 Redis。测试使用独立 namespace，只删除自身 Redis keys：

```bash
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no
CORTEX_TEST_REDIS_URL=redis://127.0.0.1:6379/15 python -m pytest -q tests/test_distributed_subagent_runtime.py
```

无需 OPENAI_API_KEY；Fake LLM 不发送网络请求。六个函数分别覆盖 Basic、Concurrent、Isolation、Budget/Cancellation、Recovery、Compatibility，使用现有 Redis fixture 和 spawn Worker 基础设施。并发测试验证一个 Worker 的三个真实 Tool 执行时间区间重叠。

也可以手动通过生产 Worker CLI 运行可信 Fake factory（以下仅供本地验证，测试工具的日志写入属于验证观测）：

```bash
mkdir -p work/subagent-demo/workspace work/subagent-demo/state
PYTHONPATH=.:tests python -m cortex.distributed worker \
  --redis-url redis://127.0.0.1:6379/15 --namespace cortex-subagent-demo \
  --workspace "$PWD/work/subagent-demo/workspace" \
  --storage "$PWD/work/subagent-demo/state" \
  --factory subagent_support:make_executor \
  --agent-client-factory subagent_support:make_client
```

另一个终端从同一仓库执行：

```bash
PYTHONPATH=.:tests python - <<'PY'
import asyncio
from pathlib import Path
from subagent_support import make_executor
from cortex.distributed import DistributedExecutionClient
from cortex.distributed.redis_adapter import connect, RedisExecutionQueue, RedisExecutionStore
from cortex.tools.permission import Permission

r = connect('redis://127.0.0.1:6379/15')
q, s = RedisExecutionQueue(r, 'cortex-subagent-demo'), RedisExecutionStore(r, 'cortex-subagent-demo')
tools = make_executor({'storage': str(Path('work/subagent-demo/state').resolve())})
api = DistributedExecutionClient(q, s, agent_registry=tools.registry,
                                agent_allowed_permissions={Permission.READ})
try:
    task_id = api.submit_agent(parent_run_id='manual-parent', goal='Compute answer',
        context={'label':'manual'}, allowed_tools=['echo'], idempotency_key='manual-1',
        max_steps=3, deadline=s.now()+30)
    print(task_id)
    print(asyncio.run(api.wait_agent(task_id, timeout=10)))
finally:
    tools.close()
    r.close()
PY
```

预期 SUCCEEDED、`final_answer='answer:manual'`、steps=1、attempt=1。重复同一请求返回同一 task_id。可查看 state/fake-agent.jsonl 的两次 Fake LLM 与一次 Tool 记录。生产部署可通过同一个 `--agent-client-factory module:function(config)` 注入现有 LLMClient；仓库提供显式选项 `--agent-client-factory cortex.distributed.factory:make_agent_client` 使用已有环境中的 LLM 配置。未配置该 factory 的旧 Worker 不会偷偷启用 LLM API，而是对 AGENT 返回 POLICY_MISMATCH，ToolTask 正常执行。升级时应把 AGENT 发布到支持该能力的 Worker 所用 namespace。

## 已实现与下一版本

已实现一层显式委派、Host submit/wait/cancel/status、隔离 Child loop、工具授权白名单、现有 action budget、deadline/cancellation、bounded Child concurrency、持久化结果和保守的从头重试。ToolTask 默认类型、原 fingerprint、Redis 老记录解码和既有工具监督路径保持兼容。

不实现 DAG、Scheduler 重构、多层递归、实时对话、Message Bus、Handoff、Planner、LLM 汇总、全局/层级 token quota、Workspace Merge/Apply、Mid-Loop Resume、K8s 或跨集群调度、MCP、多租户授权系统。Child trace/对话暂不长期保存，只保存 terminal result 和已有 execution history；被取消或 crashed attempt 的详细统计可能不可得。

下一版本建议优先提供真正可取消的 async LLM 请求、Child 专属 artifact/skill 工具绑定、更完整的 attempt 诊断和明确的 capability routing。任何有副作用的委派应单独设计安全与恢复协议，不能仅扩大 allowed_tools。
