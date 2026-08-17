# 使用指南

[English](USAGE.en.md)

## 安装

要求 Python 3.11 或更高版本。当前 alpha 在 Linux x86_64 完成验证，
Windows/macOS 原生矩阵仍在 M10-09。

### 从 GitHub Release 安装

从 [Continuity Plane Releases](https://github.com/skyhua0224/continuity-plane/releases)
下载 wheel 或 source archive：

```bash
python -m pip install /path/to/continuity_plane-0.1.0a1-py3-none-any.whl
```

### 全局安装，管理多个项目

一个用户可以安装一份 CLI，然后为每个项目单独初始化：

```bash
python3 -m venv ~/.local/share/continuity-plane/venv
~/.local/share/continuity-plane/venv/bin/python \
  -m pip install /path/to/continuity_plane-0.1.0a1-py3-none-any.whl

~/.local/share/continuity-plane/venv/bin/continuity \
  init --root /path/to/project --project-id my-project
```

### 单项目隔离安装

```bash
cd /path/to/project
python3 -m venv .venv
.venv/bin/python -m pip install /path/to/continuity_plane-0.1.0a1-py3-none-any.whl
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
python -m pip install '/path/to/continuity_plane-0.1.0a1-py3-none-any.whl[postgres]'
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

## Agent 接入

控制面安装在项目旁边，通过 CLI、Python API 或 provider adapter 使用。核心包不要求
安装任何 Agent plugin。未来 plugin 可以自动加载 packet、执行压缩前后 checkpoint
和恢复 canary，但不能拥有权威状态写权限。

## MASTER 与 STATUS

`MASTER.md` 保存稳定目标、约束、工作依赖和完成门；`STATUS.md` 保存 active work、
blocker、next action 和恢复入口。中英文版本同步提供，中文文件是默认入口。

不要把 Session 叙事和原始对话写入这两份文件。动态历史属于 typed state、append-only
events 和 checkpoint。

项目团队通常提交 canonical `MASTER.md`，个人 STATUS 和本地状态库保持本地；是否提交
项目级模板由团队治理决定。

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

## 协作与交接

所有权威写入或外部副作用都绑定：

```text
project + active Work + claim + lease epoch + scope owner + expected revision
```

交接必须携带 checkpoint、task revision、next action、return point、effect watermark、
claim 状态和 evidence refs。接收方确认第一个动作后，才允许副作用。

## 验证 Profile

项目自行决定需要哪些 gate：静态检查、测试、构建、contract fixture、mutation、loopback、
性能、故障恢复和 live-device。节省 token 或降低延迟不能跳过必要验证。

## 备份与移除

复制运行中的 SQLite 前先停止 worker。备份整个 `.continuity/`，包括 checkpoint 引用的
template 和 content-addressed artifact。所有 claim 关闭后，才能归档或删除本地目录。

## Release 与 PyPI

当前可用发行源是 GitHub Release：

<https://github.com/skyhua0224/continuity-plane/releases>

`continuity-plane` 的 PyPI 名称已检查为未占用，但当前版本尚未上传 PyPI。本地没有
可信发布凭据；后续应使用 PyPI Trusted Publishing 的 GitHub Actions workflow，而不是把
token 写入本机或仓库。文档不会在真正发布前声称 `pip install continuity-plane` 可用。

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
