# Cortex MCP System v1

## 架构检查与最小集成

现有 `ToolExecutor.aexecute()` 完成权限检查、Pydantic 参数校验和工作区权限边界，随后按 `ExecutionStrategy` 调用工具。`PROCESS_SUPERVISED` 使用子进程；`BACKEND_SUPERVISED` 在当前进程调用原生异步 hook，资源和超时由 backend 管理。`aexecute_waves()` 已有并发信号量、serial barrier 与取消传播。原有同步 `close()` 不足以关闭 SDK 的异步上下文，因此增加通用 `aclose()`，未重构 AgentLoop 的执行逻辑。

调用链：

```text
AgentLoop.arun
  → ToolExecutor.aexecute_waves / aexecute
  → permission + Pydantic validation + existing scheduling
  → BACKEND_SUPERVISED → aexecute_with_timeout
  → MCPToolAdapter → official ClientSession.call_tool
  → official stdio transport → MCP server
  → ToolResult → existing observation / recorder / LLM tool output
```

没有 MCPExecutor。AgentLoop 的执行过程无需知道工具来源。新增 generic backend deadline hook 默认委托现有 `aexecute()`，保持 Shell 等工具兼容；MCP 将工具超时和 executor 上限取最小值。远程参数中名为 `timeout` 的字段是业务参数，不会被执行器改写。

模块：`config.py` 本地配置与可信授权；`client.py` 官方 SDK 会话和进程生命周期；`manager.py` 多 Server、发现、注册和所有权；`schema.py` 严格 JSON Schema 校验的 Pydantic 模型；`adapter.py` 名称映射、原生异步调用和结果映射；`logging.py` SDK 诊断脱敏。

## 使用与生命周期

安装 `requirements.txt`。复制 `configs/mcp.example.json`，使用实际 Python 可执行文件（建议绝对路径），按需设置 `args`、`cwd`、`env`，并显式启用。示例从仓库根目录启动本地 SDK fixture，只授权 echo/add。`cwd` 相对调用进程解析，不全局 chdir；命令以 argv 启动，不通过 shell。Windows/Linux 的进程启动和进程树清理由 SDK 的平台实现负责。仅继承 SDK 的最小平台环境，加上用户明确给出的 env，不复制整个宿主环境。

```python
from cortex.app.bootstrap import async_build_agent

async def run(client):
    agent = await async_build_agent(client, mcp_config="configs/mcp.json")
    async with agent:
        answer = await agent.arun("调用已授权的外部工具")
        return answer
```

异步构建、调用和关闭必须位于同一事件循环中。同步 `build_agent()` 保持兼容，默认不导入或启动 MCP，也不启动嵌套事件循环。同步运行 MCP 工具会返回明确的失败信息；MCP 使用 `arun()` / `aexecute()`。启用 MCP 后调用同步 `close()` 会提示使用 `await agent.aclose()`，防止静默泄漏。

可选依赖注入：先 `await manager.start(registry, allowed_permissions)`，再 `build_agent(client, tool_registry=registry, mcp_manager=manager, ...)`。成功注入后 agent/executor 接管 manager 的异步关闭；构建前和构建失败时，注入方仍负责关闭。不要把同一个 manager 交给多个 agent。直接使用 manager/executor 的调用方应把 manager 放入 executor 的 `async_resources` 或显式在 finally 中关闭 manager。

每个 Server 有独立的长驻 owner task，独占 SDK stdio/ClientSession 上下文的进入和退出，避免跨任务退出 AnyIO cancel scope。工具调用复用同一连接，不重新启动进程。状态为 new → connecting → ready；异常进入 failed，清理后 closed；v1 不允许重新启动同一连接。

initialize 和完整分页发现共同受 connect_timeout 约束；检测重复远程工具名和重复 cursor。required Server 初始化失败使整个 MCP 初始化失败，撤销已注册工具并关闭此前启动的 Server；optional 失败被记录并跳过。未在 allowed_servers 中的 Server 不会启动，即使标记 required。

调用超时或取消时关闭该 Server 的整个 Session/stdio 连接，确保远程任务不会在本地取消后继续运行；该 Server 上其他并发调用也会失败，其他 Server 不受影响。适配器默认 SERIAL，未从远程 annotations 推断并发安全。SDK 先关闭 stdin，等待退出，再按平台终止进程树；宽限期可能使超时结果比配置的截止时间晚约两秒。运行中异常退出在下一次调用检测，不提供后台健康监控或自动重连。Executor/Agent 异步关闭会关闭全部 MCP 连接，并按对象身份安全注销自身的工具。

## Schema 与名称

Pydantic 模型在 before validator 中使用 jsonschema Draft 2020-12 校验原始对象，保留原值、字段遗漏与显式 null，不进行字符串到数字等类型转换，也不注入远程 default。`model_json_schema()` 返回原始 Schema。MCP Schema 以 `strict: false` 披露，避免本地 strict-provider 适配将可选字段变必填、关闭 additionalProperties。最终参数始终由执行器的 Pydantic 校验再检查。

支持：object/properties/required/additionalProperties（boolean 或 Schema）、array/items、string/number/integer/boolean/null、type 数组、enum/const、anyOf/oneOf/allOf、非递归局部 `$defs`/`$ref`、数值范围与 multipleOf、字符串长度与 pattern、数组长度与 uniqueItems、对象属性数量，以及 title/description/default。根输入必须 object；声明 `$schema` 时只接受 2020-12。未知关键词、format、递归或外部引用、旧 draft、非法或无法解析的引用均跳过并记录 unsupported_schema，不放宽验证。v1 的 Pydantic 模型以保留原值为主，不为每个远程字段生成原生 Python 类型声明。

工具名称为 `mcp_<server_id>_<32位SHA256摘要>`，摘要输入为 `server_id + NUL + remote_tool_name`。server_id 限制为 24 个安全字符，总长不超过 61，兼容常用 provider 的 64 字符限制。不同 Server 的同名工具产生不同名称，标点不会在规范化时碰撞。Adapter 保留 server_id、原始名称、description 和输入 Schema，`manager.resolve(name)` 可反向映射。哈希冲突仍由 registry 重复检测拒绝，不会覆盖。`unregister(name, expected=adapter)` 只删除调用方持有的对象。

## 权限和副作用边界

三层检查：Server 必须在本地 allowed_servers；工具必须在对应 Server 的 tools 精确 allowlist；本地 grant 的 Permission 必须在 executor.allowed_permissions。未授权工具不会注册或披露。执行器在 Schema 披露和每次实际执行时重新检查 Permission，运行时收紧权限立即生效；扩大权限需要重新构建以发现先前未注册的工具。远程 annotations 完全不用于授权。配置中 grant 必须显式声明 permission 与 side_effect_policy。

READ + none 可授权只读调用。其他 Permission 或副作用分类必须明确 `authorize_external_effects: true`，且分类为 compensatable 或 irreversible。例如：

```json
"write_marker": {
  "permission": "WRITE",
  "side_effect_policy": "irreversible",
  "authorize_external_effects": true
}
```

这是本地可信操作方的预授权扩展点；v1 没有交互审批 UI。外部服务不会因为自报 readOnlyHint 而获得权限。所有 MCP 工具均禁用自动重试，包括外部写操作，避免不确定结果造成重复写入。

禁止配置 WORKSPACE_REVERSIBLE。远程操作可能影响 API、数据库或其他文件系统，无法由 Cortex 的工作区 Git snapshot 恢复；因此不建立 MutationBoundary，也不会自动 Workspace Rollback。COMPENSATABLE 仅分类，不代表 v1 会执行补偿。

这些边界控制 Cortex 披露和发起哪些工具请求，不是操作系统沙箱：stdio 程序以当前用户身份运行，command 本身属于可信配置，连接/initialize 阶段也可能有副作用。本地授权无法证明远程程序遵守工具声明。需要更强隔离时应由部署环境提供权限受限的账号或沙箱。

## 结果、安全与可观测性

成功结果保留 `content`（包括非文本块）、`structuredContent`、`isError`。远程 isError 映射为失败的 ToolResult/MCPToolError，同时保留错误 content；传输错误映射 MCPTransportError，超时为 ToolTimeoutError，取消继续传播 CancelledError。通用执行器接受工具返回的 ToolResult，并统一填写工具名称、attempts、验证和整体耗时。

Manager 默认持有内存 RunRecorder，也可注入共享 recorder，独立使用时可通过 manager.recorder.events 检查跳过原因。复用 RunRecorder 的 mcp_connection、mcp_discovery、mcp_execution 事件：server_id、remote_tool_name、discovery_count、connection_duration_ms、execution_duration_ms，以及 timeout/cancelled/transport_error/tool_error/permission_denied/unsupported_schema。运行前连接事件属于 recorder 的初始化上下文；运行中的执行事件使用当前 run。多个 agent 不应共享 recorder/manager。

不记录命令参数、env、工作目录或请求 payload；配置加载和协议错误只返回固定说明，不携带原始异常。配置字段隐藏输入值；参数校验隐藏原始 input；原始 Server stderr 丢弃；SDK 已知客户端/进程诊断 logger 的内容和异常栈统一隐藏，避免 JSON 解析错误反射 wire data（此过滤作用于进程中同名 SDK logger）。env 显式值在结果和事件字符串中脱敏。不要将凭据直接放入工具名或 Schema；任意业务输出中的未知凭据需要业务系统自身的数据治理，v1 不承诺识别所有敏感数据。

## 验证与 v2

本地 fixture 使用官方 SDK 的 Server/stdio_server，提供 echo、add、slow、fail、write_marker，并提供 crash 和不支持的 Schema 用于失败测试；真实分页 tools/list。测试不需要网络、LLM、Key、Docker 或 Redis。

```bash
python -m pytest tests/test_mcp_system.py -q
python -m pytest -m 'not redis and not docker' -q
```

Windows 使用 SDK 平台进程管理；本次验证在 Linux 上运行，尚未执行 Windows CI。测试进程检查在 Linux 使用信号 0，在 Windows 使用只读 GetExitCodeProcess；后续应增加原生 Windows CI 验证。

v2 可在此 provider 边界添加 HTTP/OAuth、审批回调、显式补偿、重连和热更新。v1 不实现 Resources/Prompts、通用 Plugin System、新调度器或大规模工具检索；没有改动 Skill、Workspace Recovery 或 Distributed Worker 的实现。
