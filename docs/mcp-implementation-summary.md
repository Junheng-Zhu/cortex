# MCP v1 实现总结

实现已落在 `cortex/mcp/`，配置为 `configs/mcp.example.json`，官方 SDK 本地 stdio fixture 为 `tests/fixtures/mcp_server.py`，设计文档为 `docs/mcp-system-v1.md`。

- 执行：Adapter 注册进原有 ToolRegistry；ToolExecutor 完成权限与 Pydantic 参数校验，经 BACKEND_SUPERVISED 调用原生异步 hook。AgentLoop 只增加异步关闭接口，没有改写调用链。
- 生命周期：每个 Server 的专用 owner task 持有官方 SDK 的 ClientSession/stdio 上下文。SDK 管理进程和平台清理，manager 管理注册，agent/executor 接管异步关闭。
- 名称：server_id 加远程名称摘要形成稳定 namespace；manager.resolve 支持反向映射，重复名称拒绝覆盖。
- 超时/取消：连接含完整分页发现限时；调用采用工具和 executor 上限的较小值。超时或取消关闭该 Server 连接及进程，其他 Server 独立；同 Server 的并发调用会一并受影响。没有自动重连。
- Schema：Pydantic 模型封装原始 JSON Schema 严格校验，不进行类型强转。拒绝不支持关键词、递归/外部引用；Schema 披露保持原语义。没有使用 jsonschema 的私有 API。
- 权限：默认关闭；Server 和 Tool 精确 allowlist、本地 Permission 与副作用分类；不信任远程 annotations。外部风险操作需要本地显式预授权。注册、披露及执行均受本地政策约束，收紧执行器权限即时生效。
- 副作用：禁止 MCP 使用 WORKSPACE_REVERSIBLE，所有 MCP 工具禁用自动重试。远程状态不属于本地工作区快照，不会自动 Rollback 或补偿。
- 安全：复用 RunRecorder，记录来源、耗时、发现数量与错误类别；隐藏原始配置/参数校验输入/协议异常/SDK wire 诊断，丢弃 stderr，对显式 env 值脱敏。

验证在 Linux、Python 3.12、MCP SDK 1.30.0 上完成：新增 MCP 验收 14 项；完整本地回归 214 通过、1 项现有 Windows 原生测试跳过、45 项 Redis/Docker 测试按离线范围排除；配置和 Schema 最终边界校验另有 2 项通过。没有失败被改为跳过。`git diff --check` 和 compileall 通过。

已知限制：尚未在 Windows CI 运行；stdio 程序以当前用户权限执行，授权配置不是 OS 沙箱；未知业务敏感数据不保证自动识别。没有 HTTP/OAuth、交互审批、Resources/Prompts、热更新、重连或自动补偿。进程清理使用 SDK 宽限期，超时返回可能额外等待约两秒。

测试日志位于工作目录 `work/mcp-acceptance.txt`、`work/local-final.txt` 和 `work/final-edge-tests.txt`。
