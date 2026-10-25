# Continuity Plane

Continuity 是给人和 AI Agent 共用的任务清单、协作与上下文接续工具。用它把一段工作安排变成可追踪的 Todo／任务卡，让多个执行方按同一份规则工作，记录 worker 的启动、结束、报告和验证，并在压缩或换会话后取回下一步。

可以只用 CLI，也可以通过 MCP 让 Agent 直接操作；Codex／Claude Code 插件把这些能力接入宿主，hooks 在会话和压缩等时间点自动保存、恢复或提示变化。检索与输出预算减少重复读代码；实际字节、宿主 token 和缓存计数用于核对收益，不承诺统一节省率。

[English](README.en.md) · [下载 Alpha35](https://github.com/skyhua0224/continuity-plane/releases/latest) · [部署升级](docs/deployment.md) · [人和 AI 开工指南](docs/agent-start.md)

[![PyPI 版本](https://img.shields.io/pypi/v/continuity-plane?include_prereleases)](https://pypi.org/project/continuity-plane/)
[![Apache-2.0 许可](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![由 Continuity Plane 管理](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

## 它解决哪些问题

| 你想做的事 | 对应能力与入口 |
|---|---|
| 把“先改 A，再测 B”保存成清单，完成一项接下一项 | [Todo](#todo)：编译、查看、切换、阻塞、完成；也可直接投影任务卡 |
| 主控派卡，执行方认领、更新进度、交报告 | [collaboration](#collaboration)：任务正文、负责人、泳道、范围、收件箱、增量变更 |
| 不再手工拼提示词、查进程、从大日志里捞报告 | [Worker](#worker)：dispatch／supervisor、心跳、超时、续跑、报告挂卡 |
| worker 结束后立即续读，不在两次轮询之间漏事 | [事件与等待](#worker)：cursor 尾流、inbox wait、终态索引、无输出诊断 |
| 在 AI 对话中直接查待办、派卡、读规则和交接 | [MCP 与插件](#mcp)：工具名称、接入方式、CLI 对照和插件选择 |
| 压缩前保存，压缩后接续；worker 结束时提醒 | [Hooks](#hooks)：事件、宿主差异、保存／注入／观察的边界 |
| 找符号、相关文件、头文件或库的依赖者 | [代码检索](#context)：增量索引、brief、依赖查询、派卡代码路标 |
| 共用规则、术语、一手资料和踩坑教训 | [知识记录](#knowledge)：rules、memory、reference、lesson，各有用途 |
| 记住主控计划、待决事项、批次和批准来源 | [交接与证据](#handoff)：handoff、commitment、验证、batch、decision |
| 换 Claude／Codex 主控时恢复计划和交流习惯 | [主控连续性](docs/controller-memory.md)：有界 start、分域 profile、批准记忆、原生笔记候选导入 |
| 看工具是否真省上下文，给任务归属 usage | [计量与推荐](#usage)：字节、token、缓存、会话绑定、实际运行记录 |
| 多机使用，或需要主控互斥与可验证 State | [远端与高级能力](#advanced)：SSH bridge、controller、checkpoint／State |

默认本地运行，不需要云账号、外部数据库或常驻服务。原生模型客户端及其登录由你自行配置。普通 Todo、卡片、消息和检索不要求先建立 State claim。

<a id="start"></a>
## 安装后先跑通一件事

需要 Python 3.11+；代码索引还需要 Git。推荐用 pipx 安装，让宿主插件也能找到 CLI：

```bash
pipx install 'continuity-plane==0.1.0a35'
continuity --version
# 在目标仓库登记项目；demo 换成稳定的项目 ID
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
```

已有安装、venv、Windows 路径及 pipx/uv 升级见[部署说明](docs/deployment.md)。宿主进程的 PATH 必须能找到 CLI；使用 venv 时可在 MCP 配置中填写可执行文件的绝对路径。

登记项目即可用下方 Todo 和协作功能。需要项目内 `.continuity/`、治理模板与权威 State 时，再使用 `continuity init --root . --project-id demo`；已有目录不重复 init。`whoami` 说明本次身份，不自动获得主控租约。执行方使用独立、稳定的名字，不把临时 MCP connection ID 当 assignee。

<a id="todo"></a>
## Todo

**个人清单**：把明确的多事项指令编译成持久队列，查看当前项，记录结果后继续下一项。

```bash
# 一个人维护自己的执行清单
continuity todo compile --project-id demo --session-id controller --prompt '修复解析边界；补充回归测试；整理验证结果'
continuity todo list --project-id demo --format brief
continuity todo complete --project-id demo --item-id ITEM_ID --result '已完成该项并保留验证结果'
```

`ITEM_ID` 从 list 输出复制；`activate --item-id … --reason …` 显式切换，`block --item-id … --reason …` 记阻塞，`link` 把事项关联到已有卡片。complete 可按 ID 完成任意未关闭项，不要求工作按最初顺序结束。

**已有任务卡**：直接用卡片生成 Todo 视图，不再维护第二份计划。

```bash
continuity todo from-cards --project-id demo --assignee worker-a
continuity todo list --project-id demo --format brief
```

视图随卡片状态变化，默认只看未关闭状态；可用重复的 `--status` 或 `--all` 调整。brief 中的 `execution_state` 区分 queued、assigned、running、blocked 和 review：仅分给 worker 的卡显示 assigned，只有存在运行中的 execution binding 才显示 running；阻塞行同时给出短原因。旧队列留作历史。卡片视图的 activate 使用 `--assignee` 或 CONTINUITY_ASSIGNEE，session-id 只作恢复标签。参数查询：`continuity help todo.compile`、`continuity help todo.from-cards`。

<a id="collaboration"></a>
## collaboration

Alpha35 继续提供[私有 Gitea Issue／PR 后端](docs/gitea-collaboration.md)：成员各用本机 CLI／MCP 和 tea 登录，共享任务、认领、接受交接及真实 PR 评审请求，无需 Continuity 服务器。Issue 是任务权威，本地仅保留缓存；冲突显式报告，认领不承诺跨机器强锁。[仓库与全局配置规范](docs/collaboration-storage.md)说明哪些文件可以提交、哪些必须留在本机。下方本地卡片示例与 Gitea 模式分开使用。

### 创建、分配与更新任务卡

每张卡记录任务正文、目标、下一步、验收条件、负责人、泳道和优先级；还可记录 worktree、允许文件、分支、提交、验证和最终报告。下面在同一 demo 项目创建一张卡：

```bash
# 主控创建并分给独立的执行者实例
continuity collaboration task add --project-id demo --task-id parser-fix --lane-id parser --mode patch --priority p1 \
  --title '修复解析边界' --objective '只修改解析器及其回归测试' \
  --next-action '定位相关符号并复现边界输入' --exit-criterion '回归可复现且报告列出未验证项' \
  --assignee worker-a --allowed-file src/parser.py --allowed-file tests/ \
  --body '核对现有解析约束，补回归后修复；保留命令、结果和范围外发现。'
continuity collaboration task list --project-id demo --assignee worker-a --status queued --format brief
continuity collaboration packet --project-id demo --assignee worker-a
```

packet 返回派给你的主卡及有界协作入口；完整正文可用 task show，提示词由规则与卡片组装。手工执行时可认领和更新：

```bash
continuity collaboration task claim --project-id demo --task-id parser-fix --assignee worker-a
continuity collaboration task update --project-id demo --task-id parser-fix --body '补充已确认的任务范围与测试入口'
continuity collaboration task show --project-id demo --task-id parser-fix --include-report --format brief
```

update 只改正文时不用重复传 status。把状态改为 queued 会保留 assignee，只有显式 `--unassign` 才清空；活动 controller 租约持有者可在记录 `--reason` 后改派，不必冒充旧 owner。常见状态包括 queued、dispatched、active、blocked、ready-for-review、completed、merged，以及 rejected／superseded；本地完成与最终评审分开。列表支持状态、assignee、lane、关键词、更新时间和结果数筛选，`--all` 才包含已关闭卡，`--full` 展开字段。`task show` 默认预算为 64 KiB；更大列表使用返回的 `next_cursor` 分页。优先级为 p0／p1／p2；泳道用于区分并行工作。allowed-file 是范围声明，不是操作系统沙箱。

在 SSH、zsh 或其他容易发生引号拆分的环境，批量更新可把正文和字段放进 JSON 文件：

```bash
continuity collaboration task update --project-id demo --batch updates.json
cat updates.json | continuity collaboration task update --project-id demo --batch -
```

批次按文件顺序执行，最多 50 项；遇到第一项错误就停止并返回完成／失败索引，不宣称全有或全无事务。共享 Gitea 模式仍要求每项携带 `operation_id` 与 `expected_remote_revision`。

`--dependency ID`（可重复）在本地和 Gitea 卡上表达前置任务；completed、merged 与 superseded 都视为终态。`task list --dependencies-ready` 只返回满足这一条件的卡。未满足依赖会阻止 claim、转 active 和 dispatch。旧卡正文里的 “Depends on:” 不会被静默猜测成依赖；list 会给出迁移提示。两种后端的 `task list` 都返回同一 envelope：`items`、`total_items`、`next_cursor`、`has_more`，不再混用 list 和 object。

### 消息、增量变化与等待

收件箱保存带 ID 的消息，接收方可读后 ack；它不保证唤醒任意宿主。`send` 的 `--message-id` 可省略，Continuity 会生成并返回最终 ID；显式 ID 仍是幂等键。`ack` 可按单条 ID、时间截止点或 `--all` 分批确认，确认后再 archive。

```bash
continuity collaboration inbox send --project-id demo --sender worker-a --recipient controller --message-id parser-note-1 --task-id parser-fix --body '需要确认解析行为的兼容要求'
continuity collaboration inbox list --project-id demo --recipient controller
continuity collaboration inbox ack --project-id demo --recipient controller --message-id parser-note-1
continuity collaboration inbox ack --project-id demo --recipient controller --before 2026-10-20T00:00:00Z --limit 1000
continuity collaboration inbox ack --project-id demo --recipient controller --all --limit 1000
continuity collaboration inbox archive --project-id demo --recipient controller --limit 1000
continuity collaboration inbox unread --project-id demo --recipient controller --count
continuity collaboration inbox wait --project-id demo --recipient controller --follow
```

用 changes 回答“不在的时候发生了什么”，用 wait 阻塞等 worker 结束，减少反复读全账本与查进程：

```bash
# 0 用于首次读取；后续改用返回的 next_cursor
continuity collaboration changes --project-id demo --since 0 --max-output-bytes 2048
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --event dispatch.policy-stopped --timeout-seconds 30
```

changes 的 `truncated` 为真就按 next_cursor 继续读，不跳到 high-water 丢掉中间事件。多个 event 是“任一匹配”；wait 超时不表示 worker 失败。另有 `collaboration drift` 检查陈旧／矛盾记录，`collaboration doctor` 查看账本写入版本。

`inbox wait` 默认只看启动之后的新消息，避免把旧积压当成新通知；需要先处理已有未读时加 `--include-unread`。`--follow` 不设截止时间，Ctrl-C 结束；脚本重连使用返回的 `stream_cursor`。

<a id="worker"></a>
## Worker：派发、监督、续跑与报告

先在执行主机配置已获授权的原生客户端或自定义适配器。`YOUR_MODEL_ID`、worktree 和测试要求须换成实际值；配置适配器不会登录 provider。

```bash
continuity worker configure --model primary --provider codex --provider-model YOUR_MODEL_ID
continuity collaboration prompt --project-id demo --task-id parser-fix --assignee worker-a
# 预览不启动模型；确认计划后再实际派发
continuity dispatch --project-id demo --task-id parser-fix --model primary --effort high --dry-run
continuity dispatch --project-id demo --task-id parser-fix --model primary --effort high --background --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity report parser-fix --project-id demo
continuity worker events --project-id demo --since-cursor 0 --follow
continuity worker events --all-projects --since-cursor 'demo:120,other:80' --follow
continuity worker jobs --project-id demo --status ended
```

也可配置 `--provider claude` 或 custom。dispatch 从完整规则、任务正文、附件引用和有界代码路标组装提示词，保存运行 ID、日志、原生会话引用及报告。卡片必须先存在；并行修改前为每张卡准备隔离 worktree。后台运行后先用 wait 等对应任务的结束事件，再读 report。

需要排队而非立即启动时，用 Continuity 自己的持久队列，不再维护竞态的 filler 脚本：

```bash
continuity dispatch --project-id demo --task-id parser-fix --model primary --enqueue --request-id parser-fix-a --priority 10
continuity worker queue run --project-id demo --watch --controller controller
continuity worker queue status --project-id demo
continuity worker queue pause --reason 'maintenance'
continuity worker queue resume
```

同项目同一数据根只有一个 queue runner 能持有非阻塞单实例锁。runner 复用正常 dispatch 的卡片、控制器租约、client-family 容量和已观察 quota／cooldown 检查；容量或额度门未满足时请求留在队列，临时 429／quota／gateway 结果带退避重新入队。MCP 可安全调用 `worker.queue.enqueue/status/pause/resume/cancel/requeue`；真正启动 provider 进程的 `worker.queue.run` 保持 CLI-only。入队、等待、阻塞、退避和启动都写 change feed，可被 `changes`／`wait` 和 hook 消费。`pause` 是主机级开关，不会杀正在运行的 worker。参数见 `continuity help worker.queue.run`。

`worker events` 以新行分隔 JSON 流输出 started、progress、completed、failed、rate-limited、blocked，不靠 30 分钟监控窗口猜状态。单项目 cursor 是整数；`--all-projects` 使用 `project:cursor` 对，每个事件同时返回 `stream_cursor`。断线后从最后 cursor 重连即可。

supervisor 记录 attempt 的开始、心跳、无输出时长、结束原因、退出码与超时，并在启动后采样实际 cwd、Git HEAD 和 PID 身份。当前 attempt 成功时进入待评审，失败／中断进入阻塞；worker 已手工更新状态时保留该状态，仍挂最终报告。迟到的旧 attempt 不能覆盖新运行。`job.json` 与 `worker-jobs.json` 同步保存 running／终态和 exit_code，不再只能从 result 文件猜结束。

dispatch／resume 使用派发安全事件的派生索引：首次升级顺序回填一次，之后只读 change log 的新增尾部。索引损坏或日志替换时会从权威 JSONL 重建；不需要手工删除历史，也不会因为“索引不存在”跳过拒绝门。

主机崩溃或重启后先用 `continuity controller reservations --project-id demo --state stale` 查看遗留 job。确认重启后，在持有 controller lease 时执行 `controller reconcile-host --apply --reason …`；它只清理 host／boot 或主机身份变化证明为 stale 的 reservation。stale 不再占用容量。默认项目容量作用域是“当前项目 + host + profile + client-family”。需要跨项目限制同一 host 的运行数时，可显式执行 `controller host-capacity --host-client-limit total=8`；这是 operator 声明，不证明 provider 账号总额度。

```bash
continuity worker resume --project-id demo --task-id parser-fix --continuation '从已保存的进度继续并先核对已有测试结果' --background
```

执行方续跑使用 **worker resume**；顶层 resume 属于高级 State 恢复。原生续跑需要适配器支持及所属机器／profile 的会话引用；不把账号或线程目录搬到另一台机器尝试恢复。effort 原样透传，实际支持由适配器决定；新任务总时长／空闲默认 24 小时／90 分钟，可单独覆盖。usage limit 是 quota exhaustion，不是内容策略拒绝：若 provider 给出未来 reset，所属 official-subscription profile 会自动记录并暂停 origin／model，reset 后优先续跑原线程；旧版误标的 job 会在证据仍可读时追加分类更正。

HTTP 401／403、Cloudflare／代理拦截页和 gateway 错误不会被通用的 “policy” 字样升级成内容拒绝；只有明确的模型安全标记才算 `policy-review`。旧 job 仍误标时，主控复核后可执行 `continuity worker clear-policy-review --task-id … --job-id job-… --actor … --reason …`，留下单次可审计更正并允许原线程显式 resume。

已有启动脚本可用 `collaboration run --task-id … --assignee … -- <命令>` 或 supervisor run 只包装任意命令的记账。`supervisor daemon` 目前是执行主机上的单轮存活核对，不是自动部署的常驻服务；没有运行中 binding 时不会重放全部历史。临时错误可按有限 retry 和项目级显式 fallback 处理：`worker fallback-policy set --model primary --candidate secondary`。认证失败与内容策略拒绝不自动换渠道重试。`worker completion-hook` 可异步启动本机 argv，只记录 receipt，不阻塞 worker；它不是远程唤醒。

需要让 worker 对 shared canonical repository 只读时，可在项目 context 中显式设置 `--worker-write-policy canonical-read-only` 和重复的 `--canonical-repository-root`。Continuity 在 worker PATH 放置 Git wrapper，拦截 push／update-ref／receive-pack。它是防误操作，不是安全沙箱：绝对路径调用、其他程序或宿主权限仍可能绕过。

<a id="mcp"></a>
## MCP 与插件：让 Agent 直接使用

CLI 是实际能力入口，**MCP 是让模型调用这些能力的工具接口，插件负责把 MCP、skills 或 hooks 装进宿主**。安装哪个插件决定你会看到什么，不必全装，也不要与相同服务的手工 MCP 配置重复注册。

MCP 是宿主管理的本地 stdio 子进程；主机重启或宿主 kill 后，Continuity 状态库不会自行拉起它。重连新 MCP 前可继续走 CLI／SSH；`collaboration doctor` 会明确这一生命周期边界。

| 接入包 | 自动提供什么 | 适合谁 |
|---|---|---|
| Codex plugin `continuity-plane` | 会话／压缩等生命周期 hooks；不注册 MCP | 只需要自动接续与提醒 |
| Codex plugin `continuity-plane-search` | `continuity-search-mcp`，一个代码检索工具 | 只想让 AI 做有界 lookup |
| Codex plugin `continuity-plane-state` | `continuity-mcp`：Todo／协作工具与高级 State 工具 | 让 AI 直接管理卡片、规则、交接等；普通规划不需 State claim |
| Claude Code plugin `continuity-plane` | 协作 skill、生命周期 hooks，以及 continuity／continuity-search 两个 MCP | 同一插件接入规划、检索和自动接续 |
| 其他支持 stdio MCP 的客户端 | 手工接入下方两个 server | 不依赖某个插件市场或模型 |

### 安装插件

先安装 CLI，再选择宿主。以下是支持这些命令的 Codex CLI 的完整选择：

```bash
# 三项分别负责 hooks、检索、规划与 State；按需选择
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.35
codex plugin add continuity-plane@continuity-plane
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
```

也可在宿主插件浏览器选择同名组件。安装后检查 `/hooks` 并信任当前 hook 定义；安装本身不等于已获信任。[官方机制](https://developers.openai.com/plugins/build/plugins)与版本切换步骤见[部署说明](docs/deployment.md)。

Claude Code 在实际使用的 profile 下安装：

```bash
claude plugin marketplace add skyhua0224/continuity-plane
claude plugin install continuity-plane@continuity-plane --scope user
claude plugin list
```

多个 Claude profile 分别安装／验收。远端状态使用 bridge 模式时按[SSH 部署](docs/deployment.md#控制端与状态端分离)配置，不重复启用本地插件 hook。CLI 升级不会自动刷新插件缓存或已连接 MCP；检查包版本、插件版本和新 MCP 的 serverInfo.version，必要时重连或新开会话。

### MCP 具体能调用什么

| 工具 | 用途／例子 |
|---|---|
| `continuity_help` | 按 topic 查询真实参数，例如 todo.compile、task.add、rules.export |
| `continuity_plan` | action 选择 Todo、task、inbox、rules、reference、handoff、lesson、session、changes、wait、batch／decision、usage，以及 worker.prepare 预览等操作 |
| `continuity_collaboration`／`continuity_collaboration_packet` | 读取协作视图、派给自己的任务入口；不建立 State 写绑定 |
| `continuity_todo_compile`、`continuity_todo_list`、`continuity_todo_complete`、`continuity_todo_block`、`continuity_todo_link`、`continuity_todo_activate` | Todo 的直接工具入口；也可统一走 continuity_plan |
| `continuity_inbox` | 发送、读取、单条／批量确认和归档持久消息；send 可自动生成 ID，错误返回结构化 failed_gate |
| `continuity_context_lookup` | 另一个 search server 提供的有界符号／路径检索，默认 brief |
| `continuity_resume`、`continuity_inspect` | 绑定恢复入口／只读检查高级 State |
| `continuity_checkpoint`、`continuity_autorun` | 创建／验证 checkpoint，或从已验证现场继续当前 Work |
| `continuity_work_activate`、`continuity_work_complete`、`continuity_work_transition`、`continuity_claim_recover` | State Work 生命周期与租约；权威写入有 revision／授权／验证门 |

例如让 Agent 调 continuity_plan，参数如下；这与 CLI 的 todo from-cards 使用同一合同：

```json
{"action":"todo.from-cards","project_id":"demo","format":"brief","parameters":{"assignee":"worker-a"}}
```

查代码则调用 continuity_context_lookup，root 是代码所在主机上的绝对路径：

```json
{"root":"/workspace/project","query":"parse_message","format":"brief","max_results":5,"max_output_bytes":1024}
```

MCP 没有把所有 CLI 命令变成远程执行器：**dispatch／worker resume、适配器配置、supervisor 和 controller run 目前走 CLI**。模型可在已有 shell 权限和用户授权内运行它们，规划 MCP 自己不启动 provider，也不执行 Git 合入／部署。

不用插件时，在客户端的 MCP 配置处添加；这是宿主配置片段，不是执行一次就能完成的 shell 命令：

```json
{
  "mcpServers": {
    "continuity": {"command":"continuity-mcp","args":[]},
    "continuity-search": {"command":"continuity-search-mcp","args":[]}
  }
}
```

<a id="hooks"></a>
## Hooks：哪些是自动发生的

| 事件 | Codex | Claude Code | Continuity 做什么 |
|---|---|---|---|
| SessionStart | 支持 | 支持 | 读取有界恢复入口，提示有意义的变化；不要求先重读完整历史 |
| PreCompact | 支持 | 支持 | 保存下一步、约束、验证入口及主控工作现场；有 State 时按合同 checkpoint |
| PostCompact | 按宿主支持的恢复路径校验／恢复 | 校验；由 SessionStart(compact) 注入 | 同一代接续去重，避免每次都重复塞入上下文 |
| UserPromptSubmit | 支持 | 支持 | 有界规划提示、明确清单处理和变化提醒；卡片工作流不强建第二份 Todo |
| PreToolUse | 支持 | 本插件不提供 | Codex 的轻量边界提示，不是任意 shell／Git 的强制防护 |
| PostToolUse | 支持 | 支持 | 有界观测与已变化的结束／阻塞／规则提醒 |
| Stop | 支持 | 支持 | 收尾与接续检查；不把一次停止推断成验证或评审通过 |
| SubagentStart／SubagentStop | 本插件不提供 | 支持 | 记录原生子代理的观察事件；不凭此推断 PID、退出码、成功或完整报告 |

Alpha35 的插件与源码交接继续包含 SessionEnd，并增加可续读 worker events、inbox wait／unread、job 终态和实际 checkout 观测。[交接实现](docs/field-workflows.md#development-source-controller-recovery-and-change-consumers)包含交接读时观察、consumer／ack、派工快照校验与 task 命令别名。安装旧版本不会自动获得这些行为，需要升级并重连宿主。

Skill 给 Agent 可按需加载的协作方法；MCP 是可调用工具；hook 是在上述时间点运行的程序。它们作用不同。提醒只在有变化时给出，不能保证唤醒任何离线会话。只有在 supervisor 下运行的 worker 才有对应的受管运行记录。

自动接续恢复已记录的现场，不保证从未写入卡片／交接的想法也被完整捕获。

宿主必须实际加载并允许 hooks。`CONTINUITY_DISABLED=1` 可临时关闭；可用 doctor 检查版本与入口，再核对真实保存／恢复事件。配置安装成功、受控回调通过、自然发生的压缩／子代理生命周期，是不同证据。

<a id="context"></a>
## 代码检索与节省上下文

```bash
# 查询当前仓库的真实符号和路径
continuity context index --root .
continuity context lookup --root . --query parse_message --format brief --max-output-bytes 1024
continuity context search --root . --query 'parse_message' --path-prefix src --format brief --max-output-bytes 2048
continuity context dependents --root . --query include/parser.h --format brief
```

把符号／路径替换为仓库实际内容。索引针对 Git tracked 文件，返回位置与有界摘录；C/C++ 的包含关系、CMake 链接关系等可用于反向依赖查询和目录聚合。派卡时也可附代码路标。它不是完整编译器语义调用图；空命中后用窄范围搜索，不反复查询同一个空结果。

brief 不携带完整 JSON 哈希回执；需要机器处理时使用支持的 `--format json`。列表、结果数、输出字节各按命令 help 设置；packet 等旧子命令不接受所有通用 budget 参数。分页变化流必须消费 next_cursor，而不是把被裁剪内容当作没有发生。

<a id="knowledge"></a>
## 规则、记忆、资料与教训

| 记录 | 保存什么 | 何时用 |
|---|---|---|
| rules | 全局／项目约束、受众、版本和哈希长文附件 | 每个执行方／审查方需要一致的规则 |
| memory | 有作用域的术语、别名与释义 | 同一个词、缩写或中文简称需要一致理解；不是整个聊天记忆库 |
| reference | 标准、官方 OS／库文档、参考仓库的版本、章节和采用依据 | 实现前查一手来源，保留看过和采用过什么 |
| lesson | 项目经验、教训及更正来源 | 避免换会话或换模型后重踩同一个坑 |

```bash
# 规则正文与代码索引独立管理
continuity collaboration rule-add --project-id demo --rule-id focused-change --audience worker --text '修改限定在任务范围；报告实际验证及未验证项'
continuity rules export --project-id demo --audience worker --format text
continuity memory add --project-id demo --scope project --entity-id debug-build --alias '调试构建' --statement '本项目的 Debug 构建配置' --source '项目构建说明'
continuity reference add --project-id demo --reference-id python-json --title 'Python JSON 官方文档' --source https://docs.python.org/3/library/json.html --kind official-library
continuity lesson list --project-id demo
```

规则可按 worker／reviewer／controller 或泳道选择；`rules history/affected` 查修订和哪些在途 worker 仍持有旧版，`rules attachment` 按哈希读取长文。完整 rules export 不静默丢规则，超出预算会明确要求调整。`collaboration prompt` 统一组装规则、卡片正文和附件引用。

`rules import` 接入已有规则清单；`rules check-mirror` 检查生成的规则文件是否漂移。snapshot-export／snapshot-import 用于明确选择的规则快照往返，不自动同步整份原生 profile。

已有 source-index.tsv 可用 `reference import-tsv/export-tsv` 显式往返，不覆盖冲突或已有导出文件；`reference record` 另行登记真实阅读／采用。登记 URL 不证明读过、版本最新或方案正确。上述记录默认在外部数据根，不要求把账号数据或原始会话加入 Git。

<a id="handoff"></a>
## 主控交接与未了事项

主控计划不必散落在私有会话里。共享 handoff 保存当前批次、顺序、在途工作、待决事项和下一步；commitment 保存“答应了但还没做”的事项。

```bash
continuity handoff write --project-id demo --plan '先修解析器回归，再安排独立复核' --next-action '核对 worker 的报告与实际检查' --pending '兼容性取舍待负责人决定'
continuity handoff show --project-id demo
continuity collaboration commitment-add --project-id demo --commitment-id compatibility-decision --text '等待负责人确认兼容性要求'
continuity collaboration commitment-list --project-id demo
```

通过 `collaboration context-set --integration-base main` 设置项目级默认实现基线；它区别权威集成 HEAD 和某个 checkout 的 HEAD。仓库 `.continuity/controller-profile.json` 中锁定的 `integration_base` 也可作为项目默认值。组装 worker prompt 时会自动携带该 base 及可用的权威 HEAD，并明确它只是实现起点，不授予 merge、push、deploy 或所有权。`session bind` 把原生会话声明为主控或执行方，并关联 task／batch。压缩前和批次记录后的共享现场刷新遵守角色与租约；刷新失败会保留告警，不把旧文件当新现场。

`handoff show` 在预算不足时先保留待决事项、阻塞卡、失败／中断 attempt 和“报告已写但卡未更新”，普通 changes 后丢弃。快速 handoff write 会记录当前 change cursor，后续 show 只处理该边界后的新事件；旧交接没有 cursor 时写一次新交接即可。brief 的 unread 明确表示未确认 inbox 消息数，并给出批量 ack 与 archive 命令；确认只移出 unread，archive 才移到可恢复归档。

<a id="verification"></a>
## 验证、合入批次与决定

报告可作为附件保存到卡片，而不是只能在日志里查。手工执行也可提交：

```bash
# 这里填写该任务真实的最终报告文件
continuity collaboration task update --project-id demo --task-id parser-fix --status ready-for-review --report-file /persistent/path/report.md
continuity collaboration task show --project-id demo --task-id parser-fix --include-report --format brief
continuity help batch-add
continuity help decision-add
```

报告路径须是真实文件。branch、commit、landed_commit、released_in 分别保存相应 Git 事实；`--verification-json` 把 checks 与 unverified 分开，并把验证绑定到真实 commit。`batch-add` 记录一批卡、基线、结果提交、门结果和 metrics；`decision-add` 保存实际批准来源、例外与更正，status-render 可生成投影。

证据齐备时 task update 可一次提交元数据并标记 merged；`task merged … --batch …` 可使用已有批次记录。两者都不执行 Git 合并、快进或推送。缺证据会显露；override 需要记录 reason，不能靠元数据伪造用户批准。非代码 decision 可以不绑定 commit；一旦提供 commit，仍进行严格对象 ID 校验。

<a id="usage"></a>
## 计量、模型推荐与可用性

```bash
continuity observe report --project-id demo
continuity collaboration scenarios
continuity collaboration recommend --project-id demo --scenario quick-triage --busy-model grok
continuity quota report --project-id demo
continuity dispatch-health show --project-id demo --format brief
```

observe 区分实际返回字节、hook 注入、估计替代输出、索引缓存和 provider token／提示词缓存；净字节可以为负。实际 usage 需在所属机器／profile 原地从明确指定的 Claude／Codex 会话记录读入，保存计数而非原始会话文本。绑定 session 后才能从绑定基线计算 task／batch 增量；没有来源、价格或对照数据时保持未知，不把缓存命中率当作因果省 token。

窗口有效利用率、两次压缩之间的工作量等纵向收益，仍需真实会话对照验证，不能由这些计数直接推导。

下面仅在该会话的所属机器／profile 执行，替换成自己的真实会话 ID 和本地文件：

```bash
# 原地采集，不把别的机器的账号或原始会话复制过来
continuity session bind --project-id demo --provider claude --session-id OWN_SESSION_ID --task-id parser-fix --role worker
continuity observe ingest-usage --root . --provider claude --session-id OWN_SESSION_ID --transcript /path/on/owner/session.jsonl
```

recommend 用场景、任务适配、实际运行次数／时长与已记录 cooldown 提供建议；日常池与高成本专家池分开，可限定 eligible-model 或明确忙碌／不可用模型。它不自动派发，不保证所有模型刚好均分，也不能获知未记录的账号容量。quota 记录已知额度、可用性与明确批准的交接，不提供自动查询 Claude 原生额度的承诺。

`quota record` 缺少正式订阅声明时，错误会直接指出需要 `--official-subscription`，避免把 token 计数误当订阅额度。worker 观察到明确 usage limit 且有 reset 时自动写入该 origin／model 的 quota 记录；无可用 reset 时只暂停并保持 unknown，不伪造恢复时间。

`dispatch-health show` 现在解释暂停的 failure class、provider reason、task／attempt、观察时间和 reset；容量作用域也随 `controller status`／`reservations` 输出。

<a id="advanced"></a>
## 远端、互斥与其他高级命令

```bash
continuity --remote state-host --remote-root /srv/project collaboration task list --project-id demo --format brief
```

SSH alias、项目 root 和执行器均需在所属机器显式配置。控制端 hook 可用 `bridge install` 转发；MCP 检索可在状态端运行，或选择精确 root 映射的 bridge_config。各 profile 独立配置，不复制原生账号、会话或配额数据；不暴露任意远端文件／进程管理。可恢复数据放持久盘，tmp 只放可重建内容。

| 命令族 | 作用与边界 |
|---|---|
| controller acquire/renew/status/run | 显式主控租约、派发容量、协作客户端的串行集成；不阻止绕过工具的 Git。首次启用 guarded ledger v4 前须升级所有 dispatcher |
| collaboration effect request/list/update | 记录外部操作请求、批准与回执；不等于已经执行推送／部署 |
| collaboration outbox-replay/repair-events | 补写元数据故障时保留的执行事实、检查旧游标问题；修复先检查、备份并按显式参数应用 |
| init/verify/attach，state/status/workspace | 项目 Profile、既有治理文件接入、State 与投影视图；高级 State 是可选层 |
| resume/inspect/autorun/checkpoint/work | 有 revision、claim、授权与验证门的 Work 接续和状态提交；不作为普通 Todo/MCP 的先决条件 |
| decision/constraint，export/import/rollback | 权威规划元数据与经过验证的 State 备份／回滚；State bundle 不覆盖整个外部协作数据根 |
| plan inspect，worktree-inventory，skills-audit，storage inspect | 有界计划路由、worktree／skill 审计和存储持久性检查；不自动清理用户文件 |

## 验证范围与完整文档

当前版本为 Alpha35（包 `0.1.0a35`，插件 `0.1.0-alpha.35`）。[验证矩阵](docs/validation.md)把合成流程、受控 hook、真实使用反馈与尚未验证项分开；[历史实验](docs/benchmarks.md)的数字不代表每个项目或新版本的统一收益。核心故障不授予权限，也不要求为普通源码任务重置项目 State。

| 继续了解 | 入口 |
|---|---|
| 安装、升级、插件、不同 profile、SSH、回滚 | [部署](docs/deployment.md) |
| 个人／主控／团队的完整路径 | [使用说明](USAGE.md) · [角色工作流](docs/workflows.md) |
| 直接给 AI 的开场与收尾约定 | [开工指南](docs/agent-start.md) |
| 参数、监督与批次细节 | [命令参考](docs/field-workflows.md) · `continuity help` |
| 压缩、计量、State 与架构 | [生命周期](docs/lifecycle-parity.md) · [State](docs/state-workflows.md) · [文档索引](docs/README.md) |
| 可选项目视图、投影与图形产品的已实现／预览／计划边界 | [项目视图](docs/project-views.md) · [图形产品](docs/visual-products.md) |
| 六个使用痛点与能力边界 | [使用场景](docs/use-cases.md) |
| 版本、发布与兼容范围 | [变更记录](CHANGELOG.md) · [Release](https://github.com/skyhua0224/continuity-plane/releases/latest) |

反馈请附版本、脱敏命令和最小复现；不要上传原始会话、密钥、设备标识或私有项目资料。[提交 Issue](https://github.com/skyhua0224/continuity-plane/issues)。采用 [Apache-2.0](LICENSE)，归属要求见 [NOTICE](NOTICE)。
