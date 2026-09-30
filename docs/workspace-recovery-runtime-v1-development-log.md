# Workspace Recovery Runtime V1 开发日志

## 本轮目标

本轮工作的目标是把 Agent 的逻辑状态与 workspace 的物理文件状态绑定为统一的恢复边界：

```text
ExecutionCheckpoint = AgentCheckpoint + WorkspaceSnapshot
```

恢复操作以 `ExecutionCheckpoint` 为单位，避免只恢复 Agent 状态或只恢复文件系统所造成的状态分叉。

## 完成内容

### 1. Checkpoint 模型拆分

- 将原有逻辑 checkpoint 明确命名为 `AgentCheckpoint`。
- 保留 `Checkpoint` 别名，兼容现有调用方和已存在的测试。
- 在已有 goal、plan、actions、pending actions、observations、provider input、context 和 skill 状态之外，显式保存 `step_count`。
- 新增 `ExecutionCheckpoint`，绑定以下信息：
  - `execution_checkpoint_id`
  - `agent_checkpoint_id`
  - `workspace_snapshot_id`
  - `session_id` / `run_id`
  - `wave_id` / `action_ids`
  - `parent_execution_checkpoint_id`
  - `reason` / `created_at`
- 为 Execution Checkpoint 提供内存与 SQLite 两种 store。

### 2. Shadow Git Snapshot Store

- 新增独立的 bare Git object database，默认位于用户目录下的 Cortex snapshot storage，而不是目标 workspace 内。
- workspace identity 使用 workspace 绝对路径的 SHA-256 计算，不复用或修改用户项目的 Git repository。
- 文件内容通过 `git hash-object` 写入 shadow repository；目录树通过临时 index 和 `git write-tree` 构造；snapshot 使用 `git commit-tree` 保存。
- 临时 index 位于 shadow store，不读取或写入用户项目的 index。
- snapshot manifest 保存相对路径、blob hash 和文件 mode，并额外计算 manifest hash。
- 支持普通文件、可执行文件与符号链接。
- 遍历时排除 `.git` 和 `.cortex`，避免污染用户 Git metadata，也避免递归捕获 Cortex 自身运行数据。
- 同一套实现可用于 Git workspace、dirty worktree、包含 untracked files 的 workspace，以及完全没有 `.git` 的 workspace。
- snapshot ID 使用 shadow commit ID，因此可在 Runtime 重启后从 shadow object database 重新加载 manifest。

### 3. Workspace Diff

V1 比较两个 manifest，输出以下操作：

- `CREATED`
- `MODIFIED`
- `DELETED`

每条变化记录 path、before content hash 和 after content hash。本轮未实现 rename detection。

### 4. SideEffectPolicy

Tool 新增四种 side-effect 分类：

- `NONE`
- `WORKSPACE_REVERSIBLE`
- `COMPENSATABLE`
- `IRREVERSIBLE`

Tool 的默认值为 `NONE`；`ShellTool` 采用保守策略，默认标记为 `WORKSPACE_REVERSIBLE`。本轮没有实现 external compensation。

### 5. Mutation lifecycle 与审计

`WorkspaceRecoveryRuntime.mutate()` 在同一 workspace exclusive lock 内执行：

1. 保存 mutation 前的 AgentCheckpoint 和 WorkspaceSnapshot。
2. 执行 workspace mutation。
3. 保存 mutation 后的 AgentCheckpoint 和 WorkspaceSnapshot。
4. 计算前后 WorkspaceDiff。
5. 创建 ExecutionCheckpoint。
6. 将变化写入 MutationLedger。

`MutationLedger` 当前记录 execution checkpoint、action、wave、path、operation 以及前后 content hash，为后续 partial rollback 提供审计基础；V1 不执行 Action-level inverse rollback。

### 6. Exclusive workspace ownership

- workspace 的 canonical absolute path 是 lock identity。
- mutation、snapshot 和 restore 共用 process-wide reentrant exclusive lock。
- 同一个 workspace 的上述操作不能并发进入临界区。
- 不同 workspace 拥有不同 lock，不会被全局串行化。

### 7. Full rollback

完整 rollback 当前执行以下流程：

1. 解析目标 ExecutionCheckpoint。
2. 解析对应的 AgentCheckpoint 和 WorkspaceSnapshot。
3. 获取 workspace exclusive lock。
4. 为当前 workspace 创建 pre-rollback snapshot 和 ExecutionCheckpoint，保留撤销 rollback 的恢复点。
5. 直接按 shadow manifest 恢复文件；不调用用户仓库上的 `git reset --hard`、`git checkout .` 等命令。
6. 恢复目标 AgentCheckpoint。
7. 重新 snapshot 并校验 manifest hash。
8. 创建 post-rollback AgentCheckpoint 和 ExecutionCheckpoint。
9. 在配置 recorder 时写入 `workspace_rollback` observability event。

## 验证记录

新增的 workspace recovery 测试覆盖：

- 文件修改后恢复原内容。
- 文件删除后完整恢复。
- 新建文件在 rollback 后移除。
- 初始 dirty Git workspace 恢复到 Agent 执行前的 dirty 内容，而不是 `HEAD`。
- snapshot 和 restore 不改变用户 repository 的 `HEAD` 与 index bytes。
- `.cortex` 内容不进入 snapshot，restore 时也不会被删除。
- 非 Git workspace 的 snapshot、diff 与 restore。
- rollback 后 Agent goal 和 workspace 内容同时回到目标 ExecutionCheckpoint。
- MutationLedger 关联 action、wave 和实际文件变化。

本轮执行结果：

```text
python -m pytest -q
116 passed, 1 skipped

pytest -q tests/test_workspace_recovery.py
3 passed
```

同时执行了 `python -m compileall -q cortex` 和 `git diff --check`。

## 当前边界与后续工作

V1 首轮交付的是 recovery primitives 和协调器，当时的边界如下（其中 runtime integration 已在下方 V1.1 解决）：

- `WorkspaceRecoveryRuntime` 已提供完整的 checkpoint、mutation 和 rollback API，但尚未自动注入 `AgentLoop` / `ToolExecutor` 的每一次 ShellTool 调用；调用方必须显式通过 recovery runtime 建立 mutation boundary。
- `MutationLedger` 当前为内存实现；ExecutionCheckpoint 可以使用 SQLite 持久化，workspace 内容由 shadow Git 持久化。
- exclusive lock 是单进程范围，不是跨进程或分布式 lease。
- rollback 返回恢复后的 `AgentState`；上层 Runtime 仍需把该实例安装为当前 active state。
- pre-rollback checkpoint 会被保存，可用于 rollback-the-rollback，但当前没有更高层的 undo 命令或 checkpoint discovery UI。
- 本轮未实现 partial rollback、rename detection、手工修改 hash conflict、inverse operation、external compensation、exactly-once external operation、resource-scoped reader/writer scheduler、Git worktree 隔离或分布式 snapshot storage。

## V1.1 Runtime Integration & Enforcement

V1.1 已完成上述 recovery primitives 与真实执行链路的集成：

- `build_agent()` 默认按 `execution_workspace` 组合 Shadow Git store、Agent/Execution Checkpoint store、MutationLedger 和 WorkspaceRecoveryRuntime，同时允许调用方注入或显式禁用 recovery。
- `ToolExecutor` 根据 `Tool.side_effect_policy` 决定是否进入 mutation boundary，不包含 `ShellTool` 名称判断；未来的写文件工具只需声明 `WORKSPACE_REVERSIBLE` 即可复用。
- permission 和 validation 在 mutation boundary 之前完成，不执行 Tool 时不会创建 workspace snapshot。
- 同步 `AgentLoop` 在 Observe/Reflect 完成后提交 logical state；异步 ordered wave 在整轮 Observe/Reflect 后提交 `committed_wave` ExecutionCheckpoint。
- read-only parallel wave 保持并发且不创建 workspace snapshot；reversible tool 会形成 serial barrier。
- sync 与 async mutation 共用按 workspace identity 建立的 exclusive ownership。异步获取采用可取消的 non-blocking polling，不在持有 blocking wait 时阻塞 event loop，也不会留下后台 orphan waiter。
- reversible tool 禁止自动 retry，避免在前一次 attempt 可能已经修改 workspace 的未知状态上重复执行。
- failure、timeout 和 cancellation 均会保存执行后的 physical snapshot；cancellation 创建 `cancelled_mutation` checkpoint 后继续传播 `CancelledError`。
- mutation observability event 包含 action、wave、tool、policy、前后 checkpoint、snapshot、diff operation counts、duration 和 recovery outcome。

V1.1 的集成测试进一步覆盖同步 Shell create/modify/delete、真实 async AgentLoop、metadata-driven fake Tool、read-only 并发、同 workspace 串行化、不同 workspace 并发、失败、取消、permission/validation short-circuit，以及 logical/physical rollback 一致性。
