# 使用指南

[English](USAGE.en.md)

## 安装

要求 Python 3.11 或更高版本。当前 alpha 已在 Linux x86_64、macOS arm64 和
Windows AMD64 完成安装、verify 和卸载。

### 从 PyPI 安装

```bash
python -m pip install continuity-plane==0.1.0a11
```

从源码 checkout 开发时：

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable .
```

### 从 GitHub Release 安装

从 [Continuity Plane Releases](https://github.com/skyhua0224/continuity-plane/releases)
下载 wheel 或 source archive：

```bash
python -m pip install /path/to/continuity_plane-0.1.0a11-py3-none-any.whl
```

### 全局安装，管理多个项目

一个用户可以安装一份 CLI，然后为每个项目单独初始化：

```bash
python3 -m venv ~/.local/share/continuity-plane/venv
~/.local/share/continuity-plane/venv/bin/python \
  -m pip install continuity-plane==0.1.0a11

~/.local/share/continuity-plane/venv/bin/continuity \
  init --root /path/to/project --project-id my-project
```

### 单项目隔离安装

```bash
cd /path/to/project
python3 -m venv .venv
.venv/bin/python -m pip install continuity-plane==0.1.0a11
.venv/bin/continuity init --root . --project-id my-project
```

开发 Continuity Plane 本身时，可以把 wheel 路径替换为源码 checkout 的
editable install。`my-project` 是占位示例，项目应自行选择 `project_id` 和
`display_name`。

## 初始化项目

```bash
continuity init \
  --root /path/to/project \
  --project-id my-project \
  --display-name "My Project"
```

初始化会创建：

```text
.continuity/
  project.yaml
  MASTER.md
  MASTER.en.md
  STATUS.md
  STATUS.en.md
  state.sqlite3
```

命令拒绝覆盖已有的控制面目录。项目状态与当前 Agent、模型和 Session 独立。

```bash
continuity verify --root /path/to/project
continuity doctor --root /path/to/project
continuity state show --root /path/to/project
```

## 接入已有 MASTER 和 STATUS

已有项目不要直接用模板覆盖原计划。先生成只读 proposal：

```bash
continuity attach plan \
  --root /path/to/project \
  --master /path/to/project/MASTER.md \
  --status /path/to/project/STATUS.md \
  --work-id work-current \
  --work-title "Continue current delivery" \
  --owner-ref agent-main \
  --scope capability:delivery
```

这一步只读取并记录 MASTER/STATUS 的 hash，不写 SQLite。检查
`.continuity/attach-proposal.json` 后，再显式批准：

```bash
continuity attach approve \
  --root /path/to/project \
  --actor-ref agent-main \
  --claim-id claim-demo
```

批准会通过 State MCP 产生 revisioned commit 和 claim：初始模板 Work 被标记为
rejected，现有 Work 进入 active，source evidence 绑定到 proposal hash。源 MASTER 或
STATUS 在两步之间发生变化会拒绝批准。重复批准返回 `already-attached`，不会产生重复 Event。

已有 canonical MASTER 的长期项目都应使用这个流程；原 MASTER 继续拥有治理权，
`.continuity/MASTER.md` 只做本地桥接。

`state show` 通过与集成相同的 authorization 和 validation 边界读取 typed snapshot。
初始 snapshot revision 为零，并包含一个名为 `work-initial` 的 proposed Work。

## 治理仓与交付仓分离

多仓项目可以把 `.continuity/` 保存在治理仓，同时把实现、提交和发布放在独立仓库。
先在治理根注册本地 delivery workspace：

```bash
continuity workspace register \
  --root /path/to/governance \
  --workspace-id service \
  --workspace-root /path/to/service \
  --allow-effect source-control.local \
  --allow-effect source-control.history-rewrite \
  --allow-effect source-control.push
```

注册表保存在被忽略的 `.continuity/local/delivery-workspaces.json`，绑定项目 profile、
Git repository 摘要、绝对 workspace 根和 effect 上限，不写 State。Delivery Work
仍须显式绑定前序 Work、实现证据和当前 Git HEAD：

```bash
continuity work activate \
  --root /path/to/governance \
  --work-id work-delivery \
  --work-title "Deliver verified service change" \
  --owner-ref agent-main \
  --claim-id claim-delivery \
  --scope capability:delivery \
  --execution-class delivery \
  --source-ref issue://project/123 \
  --predecessor-work-id work-implementation \
  --implementation-evidence-id evidence-test-verified \
  --workspace-id service \
  --workspace-root /path/to/service \
  --expected-head "$(git -C /path/to/service rev-parse HEAD)" \
  --expected-ref HEAD \
  --allow-effect source-control.local \
  --allow-effect source-control.push
```

`source-control.local` 覆盖 `git add` 和普通 commit。`commit --amend`、rebase 和 reset
属于 `source-control.history-rewrite`，只有 Work 与 workspace 注册表都显式声明时才允许。
外部仓库没有匹配的 workspace ID、repository 摘要或 `repo://` claim scope 时保持拒绝。

## Profile 选择

### `local-embedded`：默认

状态、事件和本地 checkpoint 保存在：

```text
.continuity/state.sqlite3
```

不需要 PostgreSQL、Docker、Docmost、网络服务或 Agent plugin，适合个人项目、
离线开发和独立协作者。

### `forge-coordinated`：团队无需共享数据库

团队继续使用已有 Git forge：

- 需要共享治理时，提交项目的 `project.yaml` 和 canonical `MASTER.md`；
- 每个人的 SQLite、个人 STATUS overlay、artifact 和凭据留在本地；
- Issue、PR、branch ownership、review 和 CI 负责团队可见性；
- 发布 Work 或证据前运行项目自己的 build/test profile。

本模式不提供跨机器唯一 claim。需要唯一 claim 时，升级到 `shared-strong`。

### 个人 PostgreSQL：可选

个人想进行 SQL 检查、备份或让多个本地 worker 共用状态时，可以选择 PostgreSQL：

```bash
python -m pip install 'continuity-plane[postgres]==0.1.0a11'
```

当前 alpha CLI 仍默认 SQLite。PostgreSQL 通过显式 Python adapter 使用：

```python
from continuity_plane.postgres_state_store import PostgresStateStore

store = PostgresStateStore("postgresql://localhost/context")
store.initialize()
```

DSN 必须放在环境配置或 secret store，不得写进 Git。个人项目没有必要为了使用
控制面部署 PostgreSQL。

### 个人 Docmost：可选人类视图

Docmost 是人类控制台和投影界面，可以展示 Project Graph、决定、证据、上下文健康、
审批和 replay。它不能成为 typed-state authority，也不能绕过 State MCP。

当前 alpha 不包含一键 Docmost 部署。需要外部 State MCP provider 和 Docmost
connector；个人本地使用可以完全不安装 Docmost。

### `shared-strong`：团队共享权威

只有跨机器需要唯一 claim、lease、ownership、expected revision 和外部 effect
门控时才使用它。部署必须提供带授权、CAS、append-only events、durable receipts、
project isolation 和 audit retention 的 State MCP adapter。

PostgreSQL 是一种后端，不是唯一选择。当前 release 不会自动把本地项目切成
shared-strong。

## 模式切换

当前 alpha 只有 `local-embedded` 能通过 CLI 直接初始化。其他模式通过 adapter 或
团队流程启用，尚不存在 `continuity profile switch` 命令。

| 从默认模式切换到 | 适合场景 | 当前操作 | 是否移动权威状态 |
|---|---|---|---|
| `forge-coordinated` | 团队继续使用 GitHub/Gitea/GitLab，不想部署共享数据库 | 保留本地 SQLite；提交团队认可的治理模板；用 Issue/PR/CI adapter 提供共享可见性 | 否 |
| 个人 PostgreSQL | 一个人需要 SQL 检查、备份或多个本地 worker | 安装 `continuity-plane[postgres]`，由 Python/provider adapter 显式选择 PostgreSQL | 当前 CLI 不自动迁移 |
| 个人 Docmost | 需要图表、审批和历史浏览 | 保留 State MCP authority，连接只读 projection 和受控治理入口 | 否 |
| `shared-strong` | 多台机器需要唯一 claim、lease 和 CAS | 部署 conformed State MCP service，再执行 export/import/rollback 验证 | 是；当前 preview |

切换前执行：

1. 停止使用该项目状态的 worker 和 Agent Session；
2. 运行 `continuity verify --root /path/to/project`；
3. 备份完整 `.continuity/`，不要只复制运行中的 SQLite 文件；
4. 检查目标 adapter 的 capability manifest；
5. 只有 export/import 后 snapshot、event head 和 rollback hash 一致，才能切换 authority；
6. 失败时恢复备份并回到 `local-embedded`。

Linux、macOS、Windows 的安装、verify、卸载以及 local-embedded state bundle 的
export/import/rollback 已通过。跨 adapter 的一键 profile switch 仍未提供，因此
不要手工修改 `project.yaml` 的 `runtime_profile` 来冒充已切换。

### 迁移或回滚本地状态

以下命令适用于 `local-embedded`，用于可验证的备份迁移和原子回滚：

```bash
continuity export --root /path/to/project --output /safe/path/project-state.tar.gz
continuity import --root /path/to/target --bundle /safe/path/project-state.tar.gz
continuity rollback --root /path/to/target
```

`import --replace` 只应在目标已备份且确实需要替换现有本地 authority 时使用。bundle
成员、hash、project identity 或 checkpoint 绑定不一致时，导入会在替换前失败。

## Agent 接入

控制面安装在项目旁边，通过 CLI、Python API 或 provider adapter 使用。核心包不要求
Agent plugin。默认 Codex plugin 只提供有界 packet 和 checkpoint lifecycle，不注册
MCP 写工具，也不阻断普通开发。需要显式 State 操作时再安装 advanced State plugin；
写入仍经过 authorization、revision/CAS、validator、claim 和 checkpoint。

### 安装公开 Codex plugin

先安装核心包，再把本项目 GitHub 仓库作为 marketplace：

```bash
python -m pip install continuity-plane==0.1.0a11
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.11
codex plugin add continuity-plane@continuity-plane
```

安装后新建 Session。core plugin 在 `SessionStart` 加载有界 packet，并在
`PreCompact`/`PostCompact` 执行 checkpoint 生命周期。普通问题不会推进保存的 Work，
也不会输出恢复旁白。

大型仓库希望显式启用有界 current-worktree 检索时安装：

```bash
codex plugin add continuity-plane-search@continuity-plane
continuity context search --root . --query "Runtime" --max-results 40 --max-output-bytes 8192
```

该命令只读取 Git tracked current worktree，完整 JSON receipt 不超过指定字节预算；
search plugin 只注册一个 lookup MCP 工具，不加载额外 Skill。

#### 增量代码索引

适合大型仓库、多个 Session 或需要让其他 AI 快速定位符号的场景：

```bash
continuity context index --root .
continuity context lookup --root . --query "Runtime" --max-results 20 --max-output-bytes 8192
```

`index` 只读取 Git tracked 文件，把文件 hash、语言和符号位置写入用户缓存目录；默认不写
项目目录。第二次执行会复用未变化文件，修改一个文件只重新解析一个文件。`lookup` 返回
带 `repository_revision`、`index_revision` 和 `file_sha256` 的短引用，不返回源码正文。
任何 Agent 都可以通过 CLI 或 Python API 使用同一合同：

```python
from continuity_plane.code_index import lookup_code_index

receipt = lookup_code_index(".", query="Runtime")
```

索引是候选定位，不授予 State、memory 或副作用权限；展开源码前仍需按返回的 hash 验证
当前工作树。缓存损坏时会被当作未命中并重建，不会阻断项目工作。

需要在 Codex 内调用 State 工具时额外安装：

```bash
codex plugin add continuity-plane-state@continuity-plane
```

search plugin 提供 `continuity_context_lookup` MCP 工具。它只返回有界的 symbol/path/hash
引用，刷新用户缓存，不建立 State binding，也不拦截 shell。其他 AI 通过 CLI 使用同一合同。

`cache_status` 与 `returned_bytes` 由 lookup receipt 直接计量；模型 input/output token 由
host trace 计量。缓存命中不能单独证明 token 节省，必须和同任务的上下文输入输出 A/B 对账。

安装或恢复 Session 后验证实际采用状态：

```bash
continuity doctor --root . --codex-home ~/.codex
```

doctor 只读取插件配置、MCP policy、hook trust 和脱敏 lifecycle observation，不读取聊天
正文。`active` 表示已出现真实 SessionStart；`configured` 表示配置完成但尚无运行事件；
`misconfigured` 表示至少一项安装门未通过。

CLI v1 packet 中的 `read_only` 仅指 Continuity State 写入。State MCP 的 inspect/resume
外层返回 `read_only_scope=continuity-state` 与 `ordinary_project_work_allowed=true`；生成的
STATUS 将项目动作显示为 `continue-project-work-state-sync-pending`。代码编辑、编译、测试
和读取继续进行，不等待外部 Session。

### 一个 Session 管理多个项目

安装 advanced State plugin 后，同一个 Session 同时处理治理仓、实现仓和另一个项目时，
只有显式诊断 State 才对治理根执行一次 inspect；需要 State 写入时执行一次 resume：

```text
continuity_inspect(root=/path/to/project-a)
continuity_resume(root=/path/to/project-a)  # 仅在显式 State 写入前
```

inspect 不写投影、不建立写绑定；同一 turn 复用其结果，不得重复调用。resume 会把该 root
加入当前 Session 的受校验项目集合，并把它设为 active root。
之后该项目的 Work、claim、checkpoint 和 effect 请求必须使用相同 root；切换项目时
再次显式调用 `continuity_resume`。相对 root 只相对上一次成功的 active root 解析。
Session 已建立 binding 后，终端 `cwd` 不能替换 active root；未绑定、profile 缺失、
digest 失配或损坏 binding 会在 CLI/State 写入前拒绝，而不会静默改用 cwd。治理根与
独立 delivery workspace 的多仓关系仍按上一节的 registry 注册。

升级 plugin 时刷新 marketplace 后重新安装，并新建 Session：

```bash
codex plugin marketplace upgrade continuity-plane
codex plugin add continuity-plane@continuity-plane
codex plugin add continuity-plane-search@continuity-plane  # 仅在需要有界检索时
codex plugin add continuity-plane-state@continuity-plane  # 仅在需要 State MCP 时
```

如果某个项目暂时不需要宿主 hook，保留核心 CLI 即可；停用 plugin 不会删除项目的
`.continuity/` 状态。

## MASTER 与 STATUS

`MASTER.md` 保存稳定目标、约束、工作依赖和完成门；`STATUS.md` 保存 active work、
blocker、next action 和恢复入口。中英文版本同步提供，中文文件是默认入口。

不要把 Session 叙事和原始对话写入这两份文件。动态历史属于 typed state、append-only
events 和 checkpoint。

项目团队通常提交 canonical `MASTER.md` 和明确属于团队的 project profile；个人
STATUS overlay、本地状态库和 provider archive 保持本地。项目级 STATUS 是否提交由
团队治理决定。

## 日常流程

1. 读取 `STATUS.md`；
2. 在当前 revision claim active Work；
3. 组合 bounded Execution Packet；
4. 只加载适用 Skill 和当前 evidence；
5. 在 claim scope 内执行动作；
6. 运行项目 Verification Profile；
7. 提交 evidence，完成或释放 claim。

新想法默认 `capture-and-continue`，不会自动替换 active Work。

## Context Composition

默认 packet 只包含：

- active Work 和 revision；
- 当前决定、约束和 blocker；
- claim scope、owner 和 return point；
- 选中的 Skill rule ID 和 hash；
- evidence、artifact ref 和 continuation cursor；
- effect watermark。

大型日志、diff、报告和源码片段留在 content-addressed artifact，只有当前动作需要
时才有界展开。

## Skills

Skill 是带版本、hash、适用范围、依赖、冲突和 expiry 的规则包，不是动态任务记忆。
缺失或漂移的 Skill 会先 quarantine。始终加载集合应保持很小；实测选中 Skill source
bytes 降低超过 96%。

## 协作

### 本地与 Forge-Coordinated

多数团队继续使用现有 Git forge 展示 Work、branch、review 和 CI。控制面增加本地
claim、checkpoint、evidence receipt 和冲突警告；贡献者无需连接共享数据库。

### Shared-Strong

所有权威写入或外部副作用都绑定：

```text
project + active Work + claim + lease epoch + scope owner + expected revision
```

lease 或 revision 过期时操作 fail closed。presence、聊天消息、branch 和 UI 状态都
不能授予执行权限。

### 交接

交接必须携带 checkpoint、task revision、next action、return point、effect watermark、
claim 状态和 evidence refs。接收方确认第一个动作后，才允许副作用。

## 验证 Profile

项目自行决定需要哪些 gate：静态检查、测试、构建、contract fixture、mutation、loopback、
性能、故障恢复和 live-device。节省 token 或降低延迟不能跳过必要验证。

## 存储

默认 SQLite 文件为：

```text
.continuity/state.sqlite3
```

初始化时创建该文件。文件缺失、属于其他应用、schema 版本高于当前支持范围或完整性
检查失败时，`verify` 与 `doctor` 会拒绝继续。

PostgreSQL 仅是可选 adapter。artifact 可以保存在本地文件系统或外部对象存储；
profile 记录可用 capability，不可用增强必须显式降级。

## 备份与移除

复制运行中的 SQLite 前先停止 worker。备份整个 `.continuity/`，包括 checkpoint 引用的
template 和 content-addressed artifact。所有 claim 关闭后，才能归档或删除本地目录。

## Release 与 PyPI

当前版本同时发布到 PyPI 和 GitHub Release：

<https://pypi.org/project/continuity-plane/0.1.0a11/>  
<https://github.com/skyhua0224/continuity-plane/releases>

当前公开版本使用受控 PyPI token 发布。GitHub Actions OIDC workflow 和 `pypi`
environment 已准备，但 PyPI 项目侧仍需绑定 Trusted Publisher；token 不进入仓库或
GitHub secret。

## 公开仓库

项目团队可以提交项目拥有的 governance template。个人 STATUS overlay、provider archive、
secret、raw transcript、本地 database 和机器路径应写入 `.gitignore` 或外部存储。

## 验证发行包

```bash
python -m unittest discover -s tests -p 'test_*.py'
python benchmarks/run_local_state.py --iterations 1000
python -m build
```

公开 benchmark 使用临时项目，不需要网络服务。完整 receipt、样本数、限制和 content digest
位于 `benchmarks/reference-results.json`。
