# Workspace Runtime V2 — Isolated Execution & Selective Recovery

V2 在 Workspace Recovery V1.1（PR #29）的 ShadowGitSnapshotStore、
ExecutionCheckpoint、WorkspaceRecoveryRuntime 和同步/异步 ToolExecutor 上扩展。
AgentLoop 的 Action/Wave observe/reflect/commit 边界和 Snapshot 引擎保持复用。
默认模式仍是 DIRECT；Apply 不注册为模型工具。

## 入口和生命周期

```python
from cortex.app.bootstrap import build_agent
from cortex.runtime.workspace_session import WorkspaceManager

manager = WorkspaceManager('/data/cortex-runtime')  # 必须在 main_root 外
agent = build_agent(
    client,
    execution_workspace='/data/project',
    workspace_mode='ISOLATED',
    workspace_manager=manager,
    workspace_owner_id='user',
    execution_backend='docker',  # 或支持 bubblewrap 的 Linux Local
)
agent.run('修改项目')
ws = agent.workspace_session
changes = manager.final_diff(ws)

# 宿主 UI / 用户调用；不由 LLM 决定是否执行。
result = manager.apply(
    ws, runtime=agent.workspace_recovery_runtime,
    state=agent.last_state, executor=agent.executor,
)
if result.conflicts:
    print(result.conflicts)
# 放弃：manager.discard(ws, executor=agent.executor)
```

WorkspaceSession 持久化 main_root、execution_root、BASE snapshot、session/run、
owner、generation、active checkpoint 和 lifecycle status。WorkspaceSession 是
工作区生命周期，Conversation Session 是逻辑会话；默认新建隔离 Agent 使用相同
session_id，也可显式传入已有逻辑 Session。owner 是身份标签，不是授权服务或调度器。

状态为 ACTIVE → APPLYING → APPLIED，或 ACTIVE → DISCARDED。
无法恢复的排除项修改使 Session 进入 INVALID，只允许审计和 Discard。
未完成 Apply 通过 journal 恢复；完成状态的 Session 不再接受 Tool 操作。
同步 Apply 获取 execution ownership，等待活跃工具提交，关闭传入的 executor，再获取
main ownership。异步调用应使用 `await manager.aapply(...)`，它不会阻塞 Tool 所在
event loop；取消后等待后台 Apply 恢复完成，不遗留后台写入。失败/冲突后关闭的
executor 应重建，不能继续使用。

DIRECT 默认仍直接运行于原工作区，并保留 Full Rollback 的逻辑+文件恢复语义。
DIRECT 也创建 WorkspaceSession，但保持 Snapshot 延迟创建；首次 mutation 前的
base_snapshot_id 为空，不在 build_agent 时提前扫描整个工作区。
Notes 工具现在按实例绑定工作区；读取不再隐式创建 notes 目录，删除工具纳入
workspace mutation boundary。DIRECT 的 Local Shell 沿用原有 Permission/Shell
策略，不承诺 OS 文件系统隔离。

## BASE 和 Provider

BASE 来自真实文件系统 Snapshot，不能用 Git HEAD 代替。Snapshot 包含 tracked
working-tree modifications、untracked 和 ignored 普通文件、executable bit 及安全
symlink，不包含任何层级的 `.git` / `.cortex`。Git 环境变量和全局 Git 配置不能重定向
Shadow Git/index，也不会继承用户 hooks。

- Clean Git：GitWorktreeProvider 在独立、无 alternates/硬链接依赖的 Git 副本中创建
  detached worktree，然后把独立 Git metadata 放入 execution_root。用户 repository
  不新增 worktree 登记，不执行 stash/reset/checkout；独立副本内部可执行这些初始化操作。
  最后逐文件验证 BASE manifest。ignored 文件也从真实 BASE 写入。
- Dirty / untracked Git 或非 Git：SnapshotWorkspaceProvider 物化 BASE，并初始化
  独立 synthetic BASE repository。Git 支持以物化文件作为基线，不能声称继承了原 Git
  HEAD/branch/index/staging。主目录的 HEAD/index/branch 完全保留。
- 隔离副本不复制 staged state。clean 副本的 HEAD 对应原 clean HEAD；dirty 副本的
  Git 历史是 synthetic BASE，tracked modifications 和 untracked 的文件内容都已
  包含在基线中。这是明确的支持边界，不能用副本的 staging 状态解释用户原 index。

隔离不是只在结束时 Snapshot。所有 workspace-reversible Action 仍有
before_mutation 和 committed/failed/cancelled mutation checkpoints；异步 Wave
保留既有逻辑 commit。Notes、Shell、Local/Docker、Recovery 都绑定 execution_root。
默认 trace/artifact/memory/session/skill snapshot/checkpoint 位于 manager 的外部
storage 下；显式注入的已知持久化 store 若位于 main_root 内，会拒绝构建。

## Sandbox 与路径边界

Worktree 只是工作目录隔离，不是安全边界。ISOLATED Local 使用 bubblewrap，只开放
系统运行时的只读目录和 execution_root 的可写挂载；主目录和 runtime storage
不在其命名空间中，网络命名空间独立。缺少 bubblewrap 或 kernel namespace 支持时
执行失败，不降级为普通 host Bash。Windows 的 ISOLATED Local 需要使用 Docker；
DIRECT Windows 的原 Bash 合约保留。

Docker 只从 execution_root 及其中的只读 protected metadata 子目录挂载，不挂载
主工作区、宿主 Git common dir 或 runtime storage。沿用只读 rootfs、非特权用户、
cap_drop ALL、no-new-privileges、资源限额与 network_mode=none。执行 UID 必须有
execution_root 的访问权限；无权限会失败，不自动放宽用户目录权限。ISOLATED
不接受无法验证 workspace 绑定的自定义 backend。

Git/runtime metadata 在隔离后端中只读，支持 `git status/diff/log` 等读取命令，
不支持写 index、commit、checkout 等修改被 Snapshot 排除状态的命令。若工具在
新的嵌套目录创建被排除 metadata，MutationBoundary 会检测其 fingerprint 变化，
报告失败并持久化 unrecoverable_mutation，禁用后续执行。该记录不可选择性恢复，
应 Discard。DIRECT 的 Git metadata 仍是既有的非快照域，Full Rollback 不会恢复它。

路径校验拒绝 traversal、绝对路径、Windows drive/UNC/backslash/ADS、保留设备名、
尾随点/空格、protected components、文件和目录的大小写别名、symlink parent、
逃逸或指向 protected metadata 的 symlink。FIFO/socket/device 等特殊文件 fail closed。
Session 除 dev/inode 外还检查隔离目录中的 session marker，阻止 stale generation。
所有目标在写入前先校验，不把不能恢复的路径隐式当成可逆路径。

## Durable Ledger 与 Selective Recovery

SQLite MutationLedger 为 append-only API；每行包含 sequence、action/wave/checkpoint、
逻辑 session、相对 path、CREATED/MODIFIED/DELETED、before/after blob 和 Git file mode。
Blob 内容保存在 Shadow Git，记录可按 action_id/path/checkpoint_id 查询。
生产默认 Ledger 和 ExecutionCheckpoint 都持久化；显式 InMemory store 仅用于测试。

```python
runtime = agent.workspace_recovery_runtime
preview = runtime.rollback_action(action_id, preview=True)
result = runtime.rollback_action(action_id, state=agent.last_state)
result = runtime.rollback_file('src/app.py', action_id_or_execution_checkpoint_id)
undo = runtime.undo_action(action_id)
redo = runtime.redo_action(undo.checkpoint.action_ids[0])
```

`rollback_file(path, target)` 撤销 target Action/checkpoint 对该文件的修改，即恢复其
before-state；不是“恢复整个 target snapshot”。文件模式和 symlink 类型参与比较。
任何 path 的当前状态不等于 after-state，或任何后续/夹在选择记录中的其他 mutation
涉及同一路径，即返回 conflicts，不执行任何逆操作。即使后续 Action 最终写回相同
字节，也会冲突。缺少 file mode 的旧记录不能假定可逆。

成功恢复仅改变涉及的文件，保留无关文件和当前 Agent 逻辑状态，追加 compensation
Action、Observation、pending_input 和新 ExecutionCheckpoint。调用者可继续使用
传入的 state 或恢复返回的 result.state。原 actions、records、checkpoints 不删除。
Undo/redo 通过撤销补偿 Action 实现，同样检查 after-state 和后续修改。

Ledger rows、ExecutionCheckpoint 和 active branch head 使用 SQLite attached database
事务一起提交。Shadow blobs 和 AgentCheckpoint 先保存，失败最多留下无害的未引用
对象，不能发布缺少 Ledger 的成功 mutation head。Selective 写入前有完整 checkpoint；
可捕获的文件写入失败尝试恢复原涉及文件。进程崩溃保留 before checkpoint，需显式
Full Rollback 恢复，不把残留文件自动认作已成功补偿。

Full ExecutionCheckpoint Rollback 仍恢复旧逻辑+文件状态，保留 pre_rollback 作为 undo
目标并产生 post_rollback 新节点。它是用户显式请求的完整恢复，不使用 Selective 的
用户编辑冲突策略。隔离模式中被排除 metadata 已改变时，Full Rollback 拒绝声称完整
恢复它。

## History 和 Ownership

Branch head 持久化于 execution_heads，key 为 workspace_id + conversation session_id。
新节点沿 active head 的 parent_execution_checkpoint_id 追加；`latest()` 仅是诊断兼容
接口，不作为新的 active-head 选择规则。旧无 branch_id 的数据首次使用时可迁移到
对应工作区；无法加载其 snapshot 时不会跨工作区采用它。

READ ownership 使用 shared lock，WRITE 使用 exclusive lock。工具无论 sync/async、
是否有逻辑 context，都经过 workspace ownership；非 READ 的其他 side effects 也
保守串行。POSIX 使用 flock，Windows 使用保守的独占文件锁（包括读操作）。等待
async lock 可以取消，不留下后台阻塞线程。不同 execution_root 使用不同锁，可并行。
Apply/Discard/Full/Selective Rollback 与目标 workspace 活跃操作不能交错。

同机同一用户的跨进程互斥使用 canonical workspace path 派生的本地 lock file，
没有 distributed lease。锁是协作式的：外部编辑器不遵守 Cortex 锁。Apply 检查 BASE
和每次替换前的状态，并在校验/恢复时拒绝覆盖新用户修改；不声称能阻止任意外部
进程在 OS 检查与原子 rename 之间写入。恶意并发 filesystem 攻击不在工作目录隔离
的安全保证内。

## Apply Journal 与故障语义

Apply 比较 BASE / AGENT FINAL / MAIN CURRENT：仅检查 Agent 改动的路径；MAIN 对
任一路径偏离 BASE 就阻止整个 Apply，即使它恰好等于 FINAL。无关用户文件保留。
没有自动文本 merge 或 Partial Apply。

1. 持有 execution → main ownership，完成路径和 manifest preflight。
2. 创建 MAIN CURRENT backup Snapshot，并同步持久化 PREPARED journal。
3. journal 标记 WRITING，逐路径 atomic replace/delete，file fsync + POSIX directory
   fsync，随后验证合成的 main manifest。只修改 Agent 涉及的文件，不碰 Git metadata。
4. COMMITTED journal、Final ExecutionCheckpoint 和 branch head 在一个 SQLite 事务中
   提交。随后记录 APPLIED；此状态写入失败可由重启后的 load 按 journal 校正。
5. 异常或取消尝试恢复备份中的涉及文件；恢复成功标 RESTORED，失败标
   RECOVERY_REQUIRED。任何恢复都先检查当前涉及文件仍为 backup 或 FINAL 状态，
   不覆盖崩溃后用户的新内容。

```python
for journal_id, session_id, status, payload in manager.pending_applies():
    manager.recover_apply(journal_id)
```

重启不会自动 Apply 或自动覆盖用户文件。未完成 journal 会阻止同 main_root 的
新 Apply；显式 recover_apply 受两侧 ownership 保护，并在锁内重读 journal 状态。
COMMITTED 是成功的唯一 journal 标记，不会在写入中途出现。恢复后无关文件保留。

Discard 等待相关 Tool ownership，关闭 executor，保存最后 Snapshot，再删除 execution
目录；外部 Snapshot/Ledger/checkpoint/history 留存供审计。如果最终文件包含不能 Snapshot 的
路径，Discard 保存 audit_note 并保留此前的有效 Snapshot，仍可释放隔离资源。APPLYING Session 不能 Discard，
必须先完成 journal 恢复。APPLIED Session 当前保留执行目录供审计，资源由 executor.close
关闭；没有自动 TTL/GC。

## 明确的支持边界与验证

- Snapshot 是 Git file-mode 语义：普通文件、executable bit、安全 symlink；不保存
  空目录、ownership、ACL、xattr、完整 POSIX permission bits、hardlink 关系或 Git index。
  Directory/file 形状转换、目录碰撞等不能安全按文件写入的情况会拒绝 Apply/Selective，
  不猜测删除目录。Snapshots 的 `.git`/`.cortex` 排除规则在所有层级一致。
- Durable Apply 要求 SQLite AgentCheckpointStore 和 SQLiteExecutionCheckpointStore，
  本地可靠 filesystem 上的 SQLite rollback-journal/attached-database 事务。未支持
  WAL 跨库原子提交、网络 filesystem、distributed lease 或外部 side-effect 补偿。
- 安全 fail-closed 会收紧 V1 对非法/无法管理路径的容忍；合法 DIRECT API、Action/Wave、
  Full Rollback、sync/async 和现有测试保留兼容。没有重写 AgentLoop 或 Snapshot 引擎。
- `tests/test_workspace_runtime_v2.py` 包括真实 clean/dirty/staged/untracked/non-Git、
  Apply/Discard/conflict、Selective/undo/redo、SQLite restart、真实子进程 crash、提交故障
  注入、跨进程/async ownership、Local namespace、Docker 和 Windows 原生路径测试。
  Docker test 需要 daemon 与 `cortex-execution-runtime:v1` image，原生 Windows test
  在非 Windows 主机明确 skip；Windows 字符串路径/别名拒绝测试在 Linux 仍执行。
- 本版本没有 Sub-agent Router/Handoff/调度、自动 merge、Partial Apply、Kubernetes 或 MCP。

### 本次验证（2026-10-07）

基于当前 `main` / `3894721`，包含 PR #29（`7cfc733`）。执行环境为 Linux。

```sh
CORTEX_RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest -q -ra
.venv/bin/python eval/smoke_eval.py
```

结果：174 passed，1 skipped；Eval Gate PASSED。真实 Docker daemon 和 runtime image
已执行验证，Local bubblewrap namespace 测试已执行。唯一 skip 是原生 Windows
filesystem/Git 集成；未声称原生 Windows 验收通过。Windows traversal/drive/ADS/
reserved-name/case-alias 路径校验已在 Linux 执行。`git diff --check` 通过。
