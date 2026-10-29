<div align="center">

# Continuity Plane

**给 AI 编程助手的项目记录：换会话、压缩上下文、多个 AI 一起干活时，计划、分工和进度都不会丢。**

[![PyPI](https://img.shields.io/pypi/v/continuity-plane?include_prereleases)](https://pypi.org/project/continuity-plane/) [![Release](https://img.shields.io/github/v/release/skyhua0224/continuity-plane?include_prereleases)](https://github.com/skyhua0224/continuity-plane/releases/latest) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) [![由 Continuity Plane 管理](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

简体中文 | [English](README.en.md)

</div>

---

## 这是什么

用 Claude Code、Codex 这类 AI 编程助手时，计划、分工和进度只存在于聊天记录里。聊天一被压缩、关掉，或者交给另一个 AI，这些信息就丢了：新会话只能重读代码去猜做到哪了，派出去的几个 AI 互相不知道对方在干什么。

Continuity 是一个在你电脑上运行的小工具，给 AI 一份**不会随聊天消失的项目记录**：要做哪几件事（任务卡）、谁在做、做到哪、交了什么报告、还有什么没决定。AI 自己通过 MCP 工具读写这份记录，插件在会话开始和上下文压缩前后自动保存、恢复，你不需要敲命令，也不需要提醒它。

默认只在本机运行，不需要账号、数据库或常驻服务。

## 什么时候用得上

| 场景 | 不用 Continuity | 用 Continuity |
|---|---|---|
| 上下文压缩了，或者第二天新开会话 | 计划留在旧对话里，AI 重读代码猜进度 | 会话一开始就拿到当前任务、下一步、约束和待决事项 |
| 一个 AI 当主控，把活分给几个 AI | 你手动写提示词、盯进程、从日志里找报告 | 主控把活拆成任务卡派出去，谁做完了马上知道，报告挂在卡片上 |
| 让 AI 在后台并行跑子任务 | Claude Code 原生后台子代理每 30 秒用自己的模型重读整段记录，只为生成一行状态文字 | Continuity 派发的执行者是独立进程，没有这笔额外开销 |
| 换人或换模型接手 | 从头讲一遍 | 交接记录保留计划、分工和待决事项 |

## 📊 实测：用与不用

以下都是真实的 Claude Code 会话（Sonnet 5.5），在隔离目录里运行，给 AI 的提示里不提 Continuity。工具与方法见 `tools/run_value_eval.py`。

**接手昨天的工作**（前一个会话已经没了，新会话要说出：每件事的状态、谁负责、待决事项；共 5 项，每组 3 次）

| | 答对几项（3 次） | token（中位数） |
|---|---|---:|
| 只有代码和 git 记录 | 1、1、1 | 77,640 |
| 有一份开工时手写的 PLAN.md | 2、4、1 | 78,531 |
| **用 Continuity** | **4、4、5** | **64,333** |

**后台并行跑子任务**（Claude Code 2.1.312，一个子任务运行约 2.5 分钟）

| | 额外的"进度摘要"模型调用 |
|---|---:|
| 原生后台子代理 | 5 次（每 30 秒一次，每次带上子代理的完整记录） |
| **Continuity 派发的执行者** | **0 次** |

**上下文压缩之后**：恢复信息约 1.1 KB，当前任务、下一步、约束、待决事项、验证入口 5 项全部正确（模拟会话，3 次）。

**它不省钱的地方**：在一个 807 个文件的 C++ 仓库里找 3 个类型的定义，Claude 自带的 grep 一次就找全，用了 37k token；改用 Continuity 的代码检索也都找对了，但多了加载工具和查询的轮次，用了 85k token。所以简单的"按名字找定义"直接 grep 就好，Continuity 的检索适合 grep 结果太多、或者要找某个符号在大仓库里被哪些地方使用的时候。

版本之间的改进数据在各版本的 [Release 说明](docs/releases.md)里。

## ✨ 核心特性

- **任务卡与待办** — 把“先修 A、再测 B、最后写说明”拆成任务卡，分给不同的 AI 或人；每张卡写明目标、允许改的文件和验收条件，待办清单直接由卡片生成
- **派发与监督 AI 执行者** — 一个工具调用把卡片交给 Codex 或 Claude 后台执行；Continuity 记录启动、心跳、退出原因和最终报告，报告自动挂回卡片
- **结束即通知** — 执行者完成、失败、被限流或卡住时，主控马上在对话里看到，并且附带下一步该调用的工具；覆盖主控负责的所有项目
- **压缩和换会话不丢进度** — 插件在会话开始、压缩前后自动保存和恢复“接下来做什么”；换主控时，交接记录保留计划、待决事项和批准来源
- **Agent 默认就会用** — MCP 服务器自带使用说明，常用操作是独立的类型化工具；派发给执行者的提示词直接写明该调用哪个工具；在 shell 里绕路时会被提醒一次
- **grep 结果太多时少读代码** — 本地代码索引按符号、路径或反向依赖返回定义在前的几条位置，默认约 1 KB；简单按名字找定义时直接 grep 更省
- **共享规则和资料** — 规则、术语、参考文档、踩坑教训分开记录，派发时自动带上对应规则
- **用量看得见** — `continuity observe adoption` 统计每个 AI 会话通过 MCP 和 CLI 使用 Continuity 的次数，哪些操作还在走 shell 一目了然

<details>
<summary><strong>任务卡、待办与交接</strong></summary>

- 卡片字段：标题、正文、目标、下一步、验收条件、负责人、泳道、优先级、允许修改的文件、worktree、分支、提交、验证记录和最终报告
- 状态：queued、dispatched、active、blocked、ready-for-review、completed、merged，以及 rejected、superseded
- 依赖：`--dependency` 声明前置卡片，`task list --dependencies-ready` 只列可以开工的卡
- 待办：`todo from-cards` 按负责人把卡片投影成清单；也可以把一段多事项指令直接编译成个人待办
- 交接：`handoff write` 保存当前计划、下一步、待决事项；`handoff show` 优先展示待决事项、阻塞卡和失败的运行
- 共享后端：可选私有 Gitea Issue／PR 作为任务来源，各成员用本机 CLI 和 MCP 访问

</details>

<details>
<summary><strong>执行者派发、队列与额度</strong></summary>

- 执行者配置：Codex、Claude Code 或任意自定义命令；Continuity 不保存也不复制登录信息
- 派发前预览：组装好的提示词、规则、代码路标和检查结果，确认后再启动
- 后台监督：心跳、无输出时长、超时、退出码、实际工作目录和 Git HEAD；崩溃或重启后可列出并清理遗留记录
- 队列：容量或额度不够时排队，单实例 runner 依次启动；临时 429／额度错误自动退避重试
- 回退策略：项目级 fallback 只在同一模型的临时重试用完后启用；认证失败和内容策略拒绝不会自动换渠道
- 续跑：`worker resume` 继续原生会话；模型额度恢复后优先回到原线程

</details>

<details>
<summary><strong>Hooks 与 MCP</strong></summary>

- Hooks：SessionStart、PreCompact、PostCompact、UserPromptSubmit、PostToolUse、Stop、SessionEnd；Claude Code 另有 SubagentStart／SubagentStop，Codex 另有 PreToolUse
- MCP：`continuity` 服务器提供任务、派发、等待、报告、交接等工具；`continuity-search` 提供代码检索
- 远程：控制端和状态端可以分在两台机器，通过 SSH 转发 hooks 和命令
- 计量：区分实际返回字节、hook 注入、宿主 token 和缓存计数；采用率统计只记次数，不存会话内容

</details>

## 🧭 工作方式

```mermaid
flowchart LR
    U[你] -->|一段需求| C[主控 AI]
    C -->|continuity_task_add| K[(任务卡)]
    C -->|continuity_worker_dispatch| W1[执行者 A]
    C -->|continuity_worker_dispatch| W2[执行者 B]
    W1 -->|continuity_report_final| K
    W2 -->|continuity_report_final| K
    K -->|结束通知 + 下一步工具| C
    C -->|continuity_handoff_write| H[(交接记录)]
    H -->|压缩 / 换会话后自动恢复| C
```

一个典型的过程：

1. 你给主控一段需求。主控用 `continuity_task_add` 拆成几张卡，写清楚每张卡做什么、怎么验收。
2. `continuity_worker_dispatch` 先预览，再 `launch=true` 交给执行者后台运行。
3. 主控调用 `continuity_events_wait` 等待；任何项目的执行者结束，主控都会收到通知和下一步建议。
4. 执行者干完活，调用 `continuity_report_final`：报告、提交和检查结果写回卡片，卡片标为待评审，并通知主控。
5. 主控读报告、安排评审或合入。上下文快满时 `continuity_handoff_write`，下一个会话自动接上。

## 🚀 快速开始

需要 Python 3.11+ 和 Git。

```bash
pipx install 'continuity-plane==0.1.0a36'
continuity --version
cd your-repo
continuity collaboration register --project-id demo --control-root . --repository-root .
```

然后在 AI 助手里装上插件（见下一节），直接说需求就行，例如“把这三件事拆给两个助手做，明天有人接手”。不习惯让 AI 自己管时，也可以手动建一张卡看看：

```bash
continuity collaboration task add --project-id demo --task-id first-card --lane-id main --mode patch --priority p1 --title '试一张卡' --objective '熟悉流程' --next-action '读卡片' --exit-criterion '能在列表里看到' --body '随便写点要做的事'
continuity collaboration task list --project-id demo --format brief
```

已有环境、venv、Windows 路径、升级和回滚见[部署说明](docs/deployment.md)。

## 🤖 接入 AI 助手

| 宿主 | 安装 | 得到什么 |
|---|---|---|
| Claude Code | `continuity-plane` 插件 | 协作 skill、全部 hooks、`continuity` 与 `continuity-search` 两个 MCP 服务器 |
| Codex | Codex plugin `continuity-plane-state` | 任务、派发、等待、报告等 MCP 工具和协作 skill |
| Codex | Codex plugin `continuity-plane-search` | 代码检索 MCP 工具 |
| Codex | Codex plugin `continuity-plane` | 会话与压缩 hooks |
| 其他 stdio MCP 客户端 | 手动添加 `continuity-mcp`、`continuity-search-mcp` | 同样的 MCP 工具 |

```bash
claude plugin marketplace add skyhua0224/continuity-plane
claude plugin install continuity-plane@continuity-plane --scope user
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.36
codex plugin add continuity-plane-state@continuity-plane
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane@continuity-plane
```

升级后需要重连 MCP 或新开会话，宿主才会加载新工具。Codex 安装插件后要在 `/hooks` 里信任新的 hook 定义。

最常用的 MCP 工具：

| 什么时候 | 工具 |
|---|---|
| 会话开始、压缩之后、收到任务 | `continuity_start` |
| 拆分、修改、查看任务 | `continuity_task_add` · `continuity_task_update` · `continuity_task_list` · `continuity_task_show` |
| 派给执行者并等待 | `continuity_worker_dispatch` → `continuity_events_wait` → `continuity_worker_report` |
| 执行者汇报 | `continuity_progress` · `continuity_report_final` · `continuity_review_request` |
| 交接 | `continuity_handoff_write` · `continuity_handoff_show` |
| 找代码 | `continuity_context_lookup` |
| 其他操作（规则、资料、额度、队列、批次） | `continuity_plan(action=…)`，参数用 `continuity_help` 查 |

完整的命令、参数和 hooks 说明见[命令与工具参考](docs/commands.md)。

## 📚 文档

| 想做什么 | 看这里 |
|---|---|
| 安装、升级、插件、多台机器 | [部署说明](docs/deployment.md) |
| 一个人用、主控带执行者、团队协作 | [使用说明](USAGE.md) · [按角色的工作流](docs/workflows.md) |
| 给 AI 的开工与收尾约定 | [开工指南](docs/agent-start.md) |
| 所有命令、参数、hooks | [命令与工具参考](docs/commands.md) |
| 换主控时恢复计划和习惯 | [主控连续性](docs/controller-memory.md) |
| 典型使用场景 | [使用场景](docs/use-cases.md) |
| 项目视图与图形界面的进度 | [项目视图](docs/project-views.md) · [图形产品](docs/visual-products.md) |
| 已验证的能力和已知限制 | [验证矩阵](docs/validation.md) |
| 版本变化 | [变更记录](CHANGELOG.md) · [Releases](https://github.com/skyhua0224/continuity-plane/releases) |

## ⚠️ 已知限制

- 任务卡记录的是意图和事实，不授予合并、推送、部署、付费模型或账号权限。
- `allowed-file` 和仓库写保护用来防止误操作，不是安全沙箱。
- 只有通过 Continuity 派发的执行者才有完整的运行记录；宿主自己的子代理只记录开始和结束。
- 通知在下一次工具调用或提问时送达，不能唤醒已经关闭的会话。
- Windows 原生 hooks、真实多账号额度和双机长时间等待还缺少现场验证，见[验证矩阵](docs/validation.md)。

## 🐛 反馈

提交 [Issue](https://github.com/skyhua0224/continuity-plane/issues) 时请附上：`continuity --version`、宿主（Claude Code／Codex 及版本）、脱敏后的命令和输出、复现步骤。不要上传原始会话、密钥或私有项目资料。

## 📄 许可证

[Apache-2.0](LICENSE)，归属要求见 [NOTICE](NOTICE)。
