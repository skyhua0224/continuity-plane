# 部署、升级与排障

[English](deployment.en.md) · [文档索引](README.md) · [快速开始](../USAGE.md)

适用版本：CLI `0.1.0a26`，插件 tag `v0.1.0-alpha.26`。CLI、MCP 与宿主 hook 是三个需要分别验证的层次。

## 选择部署方式

| 场景 | 安装在哪 | 额外条件 |
|---|---|---|
| 个人／同机多个会话 | 本机一份 CLI，每项目单独注册 | 不需要后台服务 |
| Claude Code 或 Codex 自动接续 | CLI + 对应插件／hook | 宿主实际加载、信任并触发 hook |
| 控制端与状态端分离 | 状态端 CLI/MCP，控制端 SSH bridge | 明确 SSH alias、两端项目路径与目标 profile |
| 强一致 State 服务 | 显式部署相应 adapter/service | 不是默认一键能力，见 [State](state-workflows.md) |

不需要为普通任务卡先部署数据库、登录另一家 provider，或启用主控 fence。

## 安装与升级

需要 Python 3.11+；检索 Git tracked 文件需要 Git。Agent 客户端另行安装并在所属机器完成自己的登录。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a26'
.venv/bin/continuity --version
```

Windows PowerShell 对应 `py -3 -m venv .venv`、`.venv\Scripts\python.exe` 和 `.venv\Scripts\continuity.exe`，无需强制改变 PowerShell 执行策略。最新文档的验证矩阵不等于本轮重新完成了每个 OS 的现场安装。

希望全局使用时可选 pipx：

```bash
pipx install 'continuity-plane==0.1.0a26'
pipx upgrade continuity-plane --pip-args=--pre
```

升级后检查 `continuity --version` 与 `pipx list`。GitHub 的 Release 分类不改变 PyPI 的 alpha 排序；精确 pin 或 `--pre` 仍必要。

已有 pipx/uv 环境无法重建时，更新原解释器，不使用破坏性重装：

```bash
uv pip install --python /path/to/pipx/venvs/continuity-plane/bin/python --reinstall-package continuity-plane 'continuity-plane==0.1.0a26'
```

pipx 的显示元数据可能仍需其正常 upgrade 流程刷新。网络失败时可用已核对摘要的 wheel 离线安装；不要关闭 TLS 验证，也不要把临时离线参数永久留给后续升级。

## 数据分别放在哪里

| 数据 | 默认位置／选择方式 | 备份范围 |
|---|---|---|
| 项目 State 与治理模板 | 项目 `.continuity/` | SQLite、checkpoint 引用、模板及产物 |
| 项目注册、卡、消息、规则、worker 记录 | 用户数据根；通常 `~/.local/share/continuity` | 整个所选数据根及外部附件 |
| 可恢复工作目录 | 数据根下 work 或持久用户状态目录 | 自定义 `CONTINUITY_WORK_ROOT` 也要覆盖 |
| 代码索引 | 用户缓存，按仓库隔离 | 可重建，不代替源码备份 |
| 原生账号／会话 | provider 自己的 profile | 不属于 Continuity 的跨机同步范围 |

协作命令可用 `--data-root`，或统一设 `CONTINUITY_DATA_ROOT`；先查具体命令 help。不要把任意内存盘当作持久数据根：

```bash
continuity storage inspect --path /path/to/persistent/work
```

备份前协调写入者，使用 SQLite 认可的备份方式或一致性停写窗口；不要复制一半正在变化的数据集。`export/import/rollback` 的 State bundle 不覆盖整套外部协作注册表。

## Codex plugin 与 MCP

先安装 CLI。添加 marketplace，再选择插件：

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.26
codex plugin add continuity-plane@continuity-plane
```

需要检索或完整 State MCP 时，分别选择对应插件。支持 `plugin add` 的 CLI 可执行：

```bash
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
```

没有该子命令的宿主使用插件浏览器选择相同条目；不要为了安装而覆盖配置文件。

宿主也可从 `/plugins` 选择相同 marketplace。检索插件 `continuity-plane-search` 可独立启用；`continuity-plane-state` 才注册完整 State MCP。core 默认只有恢复／hook，不是所有 MCP 功能都自动出现。

只想显式连接协作工具时，通用 stdio server 命令是 `continuity-mcp`；检索是 `continuity-search-mcp`。不要再与已提供相同服务的插件重复注册。`continuity_plan`、`continuity_help`、`continuity_inbox` 等元数据操作无需先调用 State resume。

固定在旧 tag 的 marketplace 不会因为普通刷新自动换成新 tag；升级时先核对并按宿主支持的方式选择目标 ref。随后需要宿主刷新／新会话。通过 `/hooks` 审核当前 hook 定义，并检查：

```bash
continuity doctor --root . --codex-home /path/to/codex-profile
```

分清配置已安装、宿主已信任、原生事件已观测三个状态。不要通过整体替换 Codex 配置、迁移 auth 文件或更改模型路由来修复插件。

官方参考：[Codex 插件与 marketplace](https://developers.openai.com/plugins/build/plugins)。

## Claude Code

在你实际运行的 profile 下安装，而不是只装在另一台机器的默认目录：

```bash
claude plugin marketplace add skyhua0224/continuity-plane
claude plugin install continuity-plane@continuity-plane --scope user
claude plugin list
```

本地插件使用安装包的 `continuity-host-hook`，包括 SessionStart、PreCompact、PostCompact、UserPromptSubmit、PostToolUse、Stop、SubagentStart、SubagentStop。新开会话后核对事件记录。

Claude PostCompact 只验证，SessionStart(compact) 才注入上下文。原生子代理 start/stop 只是观察，不能据此推断测试成功、退出码或完整报告。安装检查通过不代表自然发生的子代理生命周期已经实测。

不要同时启用同一套插件 hook 与手写重复 hook。自动 hook 需由宿主实际触发，CLI 可执行不构成采用证据。官方参考：[Claude Code plugins](https://code.claude.com/docs/en/plugins)。

## 控制端与状态端分离

在状态端安装 CLI，确认已注册项目；在控制端配置现有 SSH alias。先做只读检查：

```bash
continuity --remote state-host --remote-root /srv/project --remote-endpoint /opt/continuity/bin/continuity-remote-cli --version
continuity --remote state-host --remote-root /srv/project --remote-endpoint /opt/continuity/bin/continuity-remote-cli collaboration task list --project-id demo --format brief
```

路径与 alias 是占位符。远端数据根属于远端进程环境；不要把 `--data-root` 当作 SSH URL 或依赖跨机直接读写 SQLite。卡片 worktree、适配器和原生会话必须属于实际执行的主机／profile。

### Claude hook bridge

控制端写一个私有 bridge JSON；不把它提交到公共仓库：

```json
{
  "host": "state-host",
  "remote_endpoint": "/opt/continuity/bin/continuity-remote-receive",
  "mcp_endpoints": {
    "continuity": "/opt/continuity/bin/continuity-mcp",
    "continuity-search": "/opt/continuity/bin/continuity-search-mcp"
  },
  "forward_checklists": false,
  "mappings": [
    {"local_root": "/workspace/project", "remote_root": "/srv/project"}
  ]
}
```

安装时只指定你拥有的目标 profile，使用规范的 remote-hook 入口：

```bash
continuity bridge install --profile /path/to/claude-profile --config-file /path/to/bridge.json --executable /opt/continuity/bin/continuity-remote-hook
```

该命令保留原字节备份、只管理指定 bridge 的 hook，重复执行应无变更。检查 8 类 hook 各一份，并验证实际配置的 SSH MCP 握手。切换旧 bridge 配置路径时，通过 `supersedes_config_files` 明确列出要替换的路径。

bridge 默认可转发有界的当前显式清单指令；上例关闭了这一项。转发内容不是整个历史 transcript。私有凭据、原生 session 库与 transcript 路径不应跨机器搬运。

### 远端检索

MCP 可以通过 SSH 在状态端启动 `continuity-search-mcp`，此时 `root` 是远端仓库。Alpha26 也支持本机 search MCP 显式指定工具参数 `bridge_config`，或启动环境 `CONTINUITY_BRIDGE_CONFIG=/path/to/bridge.json`：仅把精确匹配的 local_root 映射到 remote_root，经 `continuity-remote-cli` 执行检索。配置可选 `remote_cli_endpoint`、`lookup_timeout_seconds` 和所属机器的 `remote_data_root`。未匹配、多重匹配或传输失败时明确报错，不回退本机；远端模式拒绝本机 cache_path/data_root。没有任意远端文件／进程管理接口。此路由有合成协议测试，尚未作为 Alpha26 在真实双机环境重新实测。

MCP 使用稳定执行者身份：在 server 环境设置 `CONTINUITY_ASSIGNEE=worker-a`，或每次 `continuity_plan` 传同一 `executor_id`。task add/update/claim 和 todo activate 自动沿用它；其他执行者的冲突仍被拒绝。session_id 只用于恢复，不再改写卡片 assignee。这是协作身份声明，不是原生账号认证；不要把所有 worker 配成同一身份。

主控设置 `CONTINUITY_ROLE=controller` 后，未显式传 executor_id/assignee 的 task update 只更新元数据，不把卡片改派给主控；显式 executor_id 仍执行归属检查。这个路由约定不增加外部操作权限。

## 验收、排障与回滚

| 表现 | 先检查 |
|---|---|
| CLI 新、MCP 旧／工具缺失 | 新建 MCP 连接，核对 serverInfo.version 与实际工具表，不重建项目 State |
| 压缩后没接续 | 当前机器、实际 profile、hook 信任、PreCompact/恢复成对记录；不能只查包版本 |
| 消息或结束事件没看到 | 收件人、task id、游标分页、wait 超时；不要跳到 high-water 跳过未交付页 |
| 规则正文缺失 | 用 rules export；长文检查附件 hash，不接受静默截断 |
| 更换 MCP 连接后自己的卡被拒 | 核对 CONTINUITY_ASSIGNEE/executor_id 与原 assignee 是否一致；不自动把旧连接身份转换为新身份 |
| worker 提前改卡后报告未挂卡 | Alpha26 已修；核对是否是旧运行、旧 MCP、过期 attempt 或换过执行者，已有历史文件仍可显式挂载 |
| hook 故障影响体验 | 临时 `CONTINUITY_DISABLED=1`，保留日志与原配置；不要删除账号数据 |

升级前保存软件版本、配置备份、注册表与自定义产物位置。回滚软件后恢复对应 hook/MCP 入口并重连。启用 guarded ledger v4 的项目不能指望旧 dispatcher 继续读取；保留升级前备份，先协调参与者，不擅自把新 schema 改回旧值。

卸载插件或 CLI 不会自动清空项目记录。不要使用递归删除命令清理整个用户数据根或 provider profile。
