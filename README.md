# Continuity Plane

[![Managed with Continuity Plane](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

Continuity Plane 是面向长期 AI 辅助软件工作的 provider-neutral 控制面。它把任务、
决定、约束、证据、checkpoint、上下文组合和协作状态放在聊天窗口之外，支持压缩、
任务切换、进程崩溃和多人交接后的确定性恢复。

[English README](README.en.md)

## 安装

先安装一份 CLI：

```bash
python -m pip install continuity-plane==0.1.0a1
```

### 单项目

适合希望每个仓库独立保存状态和版本的个人项目。

```bash
continuity init --root . --project-id my-project --display-name "My Project"
```

### 一个 CLI 管理多个项目

适合在同一台机器上维护多个仓库；每个项目拥有独立的 `.continuity/` 和 SQLite。

```bash
continuity init --root /path/to/project-a --project-id project-a --display-name "Project A"
continuity init --root /path/to/project-b --project-id project-b --display-name "Project B"
```

### 协作项目个人使用

适合加入团队仓库但只想先管理自己的本地 Session，不要求团队部署服务。

```bash
continuity init --root /path/to/team-repo --project-id team-project --display-name "Team Project"
```

### 团队共同使用

适合需要共享 Work、claim、PR/CI 和部署状态的团队。先完成本地初始化，再按项目条件
启用 `forge-coordinated` 或 `shared-strong`；默认安装仍不要求 PostgreSQL 或 Docmost。

常用参数：`--root` 指向目标仓库，`--project-id` 是稳定的小写标识，
`--display-name` 是人类可读名称。详见[完整安装、使用与模式切换](USAGE.md)。

## 它解决哪些问题

| 场景 | 痛点 | 详情 |
|---|---|---|
| 压缩与长 Session | 刚刚还在修测试，压缩后却重答旧问题，甚至把做完的工作重新做一遍 | [场景详情](docs/use-cases.md#压缩后像换了一个人) |
| 多 Session 与部署竞态 | 两边都以为自己可以部署，直到 main、CI 和环境互相覆盖才发现冲突 | [场景详情](docs/use-cases.md#同一仓库的多个-session-会互相踩踏) |
| 多人和多 Agent | 别人已经在本地做完的东西不可见，协作者只能重复实现、重复查资料 | [场景详情](docs/use-cases.md#多人和多-agent-不知道别人已经做了什么) |
| Idea 与任务切换 | 一句临时想法让 Agent 离开主线，回来时找不到原任务的落点 | [场景详情](docs/use-cases.md#临时-idea-很容易把主线带跑) |
| 大型项目 | 几百个模块和跨仓依赖堆在一起，人和 AI 都不知道改动会影响哪里 | [场景详情](docs/use-cases.md#大型项目里人和-ai-都不知道哪里是哪里) |
| Memory、Skill、文档漂移 | 旧路径、旧决定和旧规则在压缩后重新冒出来 | [场景详情](docs/use-cases.md#memoryskill-和文档会漂移) |

## 已测结果

| 场景 | 结果 | 详情 |
|---|---|---|
| 压缩恢复 | input tokens `-40.25%`；近上限历史 `-95.06%`；quality `3/3` | [压缩实测](docs/benchmarks.md#压缩与恢复) |
| 代码检索 | input `-50.02%`；tool calls `-57.89%`；wall time `-27.41%`；quality `3/3` | [检索实测](docs/benchmarks.md#代码检索) |
| Skill 装载 | source bytes `-96.54%`；quality `3/3` | [Skill 实测](docs/benchmarks.md#skill-装载) |
| 多 Session 协调 | duplicate tool calls `-55.88%`；parallel wall time `-22.65%` | [协作实测](docs/benchmarks.md#多-session-协作) |
| 一致性 | E0-E9 `10/10`；双 Session `1000/1000`；authority violation `0` | [一致性实测](docs/benchmarks.md#一致性与限制) |
| 大型项目视图 | 2,000 nodes / 5,000 edges；scale p95 `187.459764 ms` | [图形视图](docs/project-views.md) |

这些是匹配任务和当前 fixture 的场景级结果，不能合成为所有用户的统一节省率。
用户 token、窗口有效利用率和两次压缩之间的有效工作量，按 accepted Work 归一化，
并在 host trace 可见时计量。[完整方法和限制](docs/benchmarks.md)。

## 架构概览

```text
Agent / IDE / CI / 人类控制台
              |
              v
       Execution Packet
              |
     +--------+---------+
     |                  |
 Typed State        Evidence index
 revision + CAS     hash + validity
     |                  |
     +--------+---------+
              |
      append-only events
              |
      checkpoint + replay canary
              |
          SQLite 默认
```

Memory、检索系统、代码图和 reviewer 只能提供候选信息；active task、完成状态和外部
副作用必须经过 State MCP 的 authorization、expected revision/CAS 和 validator。

## 快速开始

要求 Python 3.11 或更高版本。在已安装 CLI 的目标项目目录执行：

```bash
continuity verify --root .
continuity doctor --root .
continuity state show --root .
```

初始化会创建 `.continuity/`、SQLite 状态库以及项目自己的 `MASTER.md`、`STATUS.md`
和英文模板。项目应自行决定 `project_id` 与 `display_name`。

## 安装模式

| 模式 | 外部服务 | 适用场景 | 详情 |
|---|---|---|---|
| `local-embedded` | 无 | 个人项目、离线开发、本机多 Session | [配置](docs/configuration.md) |
| `forge-coordinated` | 已有 Git forge | 普通开源团队协作 | [配置](docs/configuration.md#profiles) |
| 个人 PostgreSQL | 本地或私有 PostgreSQL | SQL 检查、备份、本地 worker | [配置](docs/configuration.md#profiles) |
| 个人 Docmost | Docmost + connector | 图表、审批、历史观察 | [图形化产品](docs/visual-products.md) |
| `shared-strong` | 显式 State MCP 服务 | 跨设备唯一 claim、lease 和 CAS | [配置](docs/configuration.md#profiles) |

默认路径是 `local-embedded`。PostgreSQL、Docmost 和 shared-strong 都是可选增强。

## 权威边界

- `Typed State`：当前任务、owner、revision、决定、约束和门禁；
- `Event Log`：append-only 状态变化、supersedes 和 hash chain；
- `Checkpoint`：压缩、切换、交接和崩溃后的恢复点；
- `Evidence`：当前源码、标准、官方文档和测试的 provenance；
- `MASTER.md`：项目级治理意图；`STATUS.md`：当前恢复路由；
- Docmost：可选的人类控制台，动作受 State MCP 约束；
- Obsidian：生成的只读视图；
- SQLite：默认本地 authority；PostgreSQL：显式选择的 adapter。

## 文档

- [完整使用教程](USAGE.md)
- [架构说明](docs/architecture.md)
- [配置说明](docs/configuration.md)
- [Python API](docs/api.md)
- [实测方法](docs/benchmarks.md)
- [使用场景](docs/use-cases.md)
- [大型项目视图](docs/project-views.md)
- [Docmost 与 Obsidian 图形化产品计划](docs/visual-products.md)
- [English README](README.en.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)

## Release 与许可证

当前 alpha 已发布到 [PyPI](https://pypi.org/project/continuity-plane/0.1.0a1/) 和
[GitHub Releases](https://github.com/skyhua0224/continuity-plane/releases)。GitHub
Release 同时提供 wheel、source archive 和 SHA256SUMS；详见 [发布说明](CHANGELOG.md)。

Continuity Plane 使用 [Apache-2.0](LICENSE)。badge、README 署名、应用 UI 标签和
telemetry 都是可选的，法律归属以 LICENSE 和 NOTICE 为准。

## 当前状态

Linux x86_64、macOS arm64 和 Windows AMD64 已完成安装、verify 和卸载。跨平台
export/import/rollback、完整 Docmost connector、Obsidian Canvas/Bases 和
shared-strong 部署仍在后续计划中。
