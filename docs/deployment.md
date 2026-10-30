# 部署、升级与排障

[English](deployment.en.md) · [文档索引](README.md) · [快速开始](../USAGE.md)

适用版本：CLI `0.1.0a37`，插件 tag `v0.1.0-alpha.37`。CLI、MCP 与宿主 hook 是三个需要分别验证的层次。

## 选择部署方式

| 场景 | 安装在哪 | 额外条件 |
|---|---|---|
| 个人／同机多个会话 | 本机一份 CLI，每项目单独注册 | 不需要后台服务 |
| 跨成员共享私有 Issue／PR | 各自本机 CLI/MCP + tea 登录，同一 Gitea 仓库 | 无 Continuity 服务／共享数据库；[配置边界](collaboration-storage.md) |
| Claude Code 或 Codex 自动接续 | CLI + 对应插件／hook | 宿主实际加载、信任并触发 hook |
| 控制端与状态端分离 | 状态端 CLI/MCP，控制端 SSH bridge | 明确 SSH alias、两端项目路径与目标 profile |
| 强一致 State 服务 | 显式部署相应 adapter/service | 不是默认一键能力，见 [State](state-workflows.md) |

不需要为普通任务卡先部署数据库、登录另一家 provider，或启用主控 fence。

## 安装与升级

需要 Python 3.11+；检索 Git tracked 文件需要 Git。Agent 客户端另行安装并在所属机器完成自己的登录。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a37'
.venv/bin/continuity --version
```

Windows PowerShell 对应 `py -3 -m venv .venv`、`.venv\Scripts\python.exe` 和 `.venv\Scripts\continuity.exe`，无需强制改变 PowerShell 执行策略。最新文档的验证矩阵不等于本轮重新完成了每个 OS 的现场安装。

希望全局使用时可选 pipx：

```bash
pipx install 'continuity-plane==0.1.0a37'
pipx upgrade continuity-plane --pip-args=--pre
```

升级后检查 `continuity --version` 与 `pipx list`。GitHub 的 Release 分类不改变 PyPI 的 alpha 排序；精确 pin 或 `--pre` 仍必要。

已有 pipx/uv 环境无法重建时，更新原解释器，不使用破坏性重装：

```bash
uv pip install --python /path/to/pipx/venvs/continuity-plane/bin/python --reinstall-package continuity-plane 'continuity-plane==0.1.0a37'
```

pipx 的显示元数据可能仍需其正常 upgrade 流程刷新。网络失败时可用已核对摘要的 wheel 离线安装；不要关闭 TLS 验证，也不要把临时离线参数永久留给后续升级。

### 更新提示与 `continuity self-update`

Continuity 在独立的后台进程中查询包索引是否有新版本，每 24 小时最多一次，结果写入数据根目录。命令只读取这份缓存，不会等待网络。请求只包含包名，不带项目、用户或使用数据。私有构建、源码安装和 editable 安装不会检查。设置 `CONTINUITY_UPDATE_CHECK=0` 可关闭检查发出的全部网络请求。

有新版本时，交互终端每天最多在 stderr 出现一行提示（stdout 与 JSON 输出不变）；`continuity_start` 返回 `update_available` 字段；SessionStart 每个版本、每个会话一行；`continuity doctor` 同时列出 CLI、MCP 启动器与插件版本及其不一致之处。

升级始终是显式命令：

```bash
continuity self-update --dry-run   # 打印本安装对应的命令
continuity self-update
```

它识别 pipx、uv 与普通虚拟环境安装并执行对应升级；editable、源码和系统解释器安装会被拒绝并说明做法。本机有 worker 作业运行时以状态码 3 退出且不改动任何内容，因为运行中的 supervisor 惰性导入模块，可能加载到两个版本的混合；`--force` 可覆盖。升级后 MCP 服务随之退出，宿主在下一次调用时重连；插件需在宿主的插件管理器中更新并重连。Agent 仅在用户允许时运行 `self-update`。`CONTINUITY_AUTO_UPDATE=1` 可选择在本机没有 worker 运行时于 SessionStart 后台升级，默认关闭。

## 数据分别放在哪里

Gitea 模式先读[仓库与全局配置规范](collaboration-storage.md)：Issue／PR 是共享权威，仓库仅提交声明和规则；本机 State／注册表不成为远端任务账本。`~/.continuity` 仅在显式选作数据根时生效，并非隐式全局配置文件。

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

### Alpha35 的可选本机策略

这些设置只作用于当前主机与当前数据根，不跨机器同步，也不证明 provider 账号额度：

```bash
# 当前 host 所有已注册项目共享的 operator 声明容量
continuity controller host-capacity --project-id demo \
  --owner controller --token "$CONTROLLER_TOKEN" \
  --host-client-limit total=8

# 只在同会话 transient retry 耗尽后尝试的已配置 adapter
continuity worker fallback-policy set --project-id demo \
  --model primary --candidate secondary

# 可选：worker 对 canonical checkout 的 Git ref 写入防护
continuity collaboration context-set --project-id demo \
  --integration-root /workspace/demo --integration-base main \
  --worker-write-policy canonical-read-only \
  --canonical-repository-root /workspace/demo
```

host capacity 是本地运行门，不是账号总额度。Git wrapper 位于本机持久数据的 `worker-git-guard/`，只拦截通过 PATH 调用的普通 push／update-ref／receive-pack；不是 mount／namespace 沙箱。需要强隔离时使用独立 OS 用户、只读挂载或虚拟机。完成钩子用 `worker completion-hook set --argv-json ...` 配置本机 argv，日志与 receipt 写进 job 目录；它不是远程唤醒渠道。

## Codex plugin 与 MCP

先安装 CLI。添加 marketplace，再选择插件：

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.37
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

若重新 add 提示已登记不同来源，先备份本机 Codex 配置，再只重设这一项市场登记，不卸载插件或删除账号资料：

```bash
# 仅切换 Continuity 市场的固定版本
codex plugin marketplace remove continuity-plane
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.37
codex plugin marketplace upgrade continuity-plane
```

然后重新执行本节中已选用插件的 add 命令，核对真实安装缓存，不能只看 pip 版本：

```bash
codex plugin list --marketplace continuity-plane --json
```

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

本地插件使用安装包的 `continuity-host-hook`，包括 SessionStart、SessionEnd、PreCompact、PostCompact、UserPromptSubmit、PostToolUse、Stop、SubagentStart、SubagentStop。新开会话后核对事件记录。

Claude PostCompact 只验证，SessionStart(compact) 才注入上下文。原生子代理 start/stop 只是观察，不能据此推断测试成功、退出码或完整报告。安装检查通过不代表自然发生的子代理生命周期已经实测。

### 多个 Claude profile

每个实际使用的 profile 分别安装和更新；路径只作示例，不能把另一个 profile 的凭据、会话或完整配置复制过来：

```bash
# 在这个 profile 的所属机器执行；其他 profile 分别重复
CLAUDE_CONFIG_DIR=/path/to/profile-a claude plugin marketplace update continuity-plane
CLAUDE_CONFIG_DIR=/path/to/profile-a claude plugin update continuity-plane@continuity-plane --scope user
```

再用同一 CLAUDE_CONFIG_DIR 调用 claude plugin list，重连／新会话后确认加载。若采用下方 SSH bridge 模式，不要同时启用重复的本地插件 hook：每个 profile 保留自己的 bridge 配置、明确项目映射，分别执行 bridge install。该模式以 hook 的 runtime 路径和新 MCP serverInfo.version 验收，不要求 plugin list 出现重复插件。共享公开程序包不等于共享原生账号或会话状态。

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

### 稳定的 MCP over SSH

直接把宿主的 MCP 命令写成 `ssh STATEHOST continuity-mcp` 时，SSH 一断，宿主就看到工具列表变化或服务掉线；宿主会重建提示缓存，Agent 也常退回 CLI。改为在宿主本机启动代理，由代理保持工具列表稳定：

```json
{
  "mcpServers": {
    "continuity": {"command":"continuity-mcp","args":["--proxy","ssh://state-host"]},
    "continuity-search": {"command":"continuity-search-mcp","args":["--proxy","ssh://state-host"]}
  }
}
```

`state-host` 是 `~/.ssh/config` 里的 alias（端口与跳板写在那里）。远端程序不在 PATH 时，用 `ssh://state-host/opt/continuity/bin/continuity-mcp`；需要给远端进程设环境变量时，路径写成百分号编码的 `/env%20CONTINUITY_DATA_ROOT=/srv/data%20/opt/continuity/bin/continuity-mcp`，最后一个词必须仍是 `continuity-mcp`（检索代理为 `continuity-search-mcp`）。

- 代理首次连上远端时取回 `initialize` 与 `tools/list`，按「主机 + 远端 `serverInfo.version`」缓存到本机数据根的 `mcp-proxy-cache/`，整个会话内逐字节不变；远端暂时不可达时，新会话用磁盘缓存启动。
- `tools/call` 经 `ssh -T`（`BatchMode`、`ServerAliveInterval=15`、`ServerAliveCountMax=3`、ControlMaster）转发。连接断开后代理立即重连，失败则按 0.5 秒起翻倍、上限 10 秒退避，期间宿主的请求最多等待 30 秒；超时返回 `transport_unavailable`（调用未发出，可稍后重试）。
- 调用进行中连接断开：只读工具（`readOnlyHint`）或参数带 `operation_id` 的调用会重试一次；其他调用返回 `failed_gate: "transport_interrupted"`，说明它可能已执行，并指明用哪个只读工具核对，不会盲目重发。
- 重连后远端版本不同：本会话继续使用缓存列表，只向 stderr 写一次警告；重启宿主会话才采用新列表。

代理只转发到 Continuity 自己的 MCP 入口，不提供任意远端命令；状态仍只在远端，本机不读写远端 SQLite。

### 远端检索

MCP 可以通过 SSH 在状态端启动 `continuity-search-mcp`，此时 `root` 是远端仓库。Alpha29 引入并由 Alpha35 保留本机 search MCP 的显式 `bridge_config`，或启动环境 `CONTINUITY_BRIDGE_CONFIG=/path/to/bridge.json`：仅把精确匹配的 local_root 映射到 remote_root，经 `continuity-remote-cli` 执行检索。配置可选 `remote_cli_endpoint`、`lookup_timeout_seconds` 和所属机器的 `remote_data_root`。未匹配、多重匹配或传输失败时明确报错，不回退本机；远端模式拒绝本机 cache_path/data_root。没有任意远端文件／进程管理接口。此路由有合成协议测试，尚未作为 Alpha35 在真实双机环境重新实测。

MCP 使用稳定执行者身份：在 server 环境设置 `CONTINUITY_ASSIGNEE=worker-a`，或每次 `continuity_plan` 传同一 `executor_id`。task add/update/claim 和 todo activate 自动沿用它；其他执行者的冲突仍被拒绝。session_id 只用于恢复，不再改写卡片 assignee。这是协作身份声明，不是原生账号认证；不要把所有 worker 配成同一身份。

主控设置 `CONTINUITY_ROLE=controller` 后，未显式传 executor_id/assignee 的 task update 只更新元数据，不把卡片改派给主控；显式 executor_id 仍执行归属检查。这个路由约定不增加外部操作权限。

## 验收、排障与回滚

| 表现 | 先检查 |
|---|---|
| CLI 新、MCP 旧／工具缺失 | 新建 MCP 连接，核对 serverInfo.version 与实际工具表，不重建项目 State |
| 主机 kill／重启后 MCP 不在 | MCP 由宿主管理且不跨重启存活；新开宿主 MCP，或在重连前走 CLI／显式 SSH remote。用 `collaboration doctor` 查看生命周期提示 |
| 压缩后没接续 | 当前机器、实际 profile、hook 信任、PreCompact/恢复成对记录；不能只查包版本 |
| 消息或结束事件没看到 | 收件人、task id、游标分页、wait 超时；不要跳到 high-water 跳过未交付页 |
| worker 结束但监控没醒 | 用 `worker events --since-cursor … --follow` 从原 cursor 续读；checkpoint 与 hook 都不替代持续事件流 |
| job.json 没有 status／exit_code | Alpha35 起新 job 会同步终态；旧 job 只用 result.json 对账，不补造缺失事实 |
| 规则正文缺失 | 用 rules export；长文检查附件 hash，不接受静默截断 |
| 更换 MCP 连接后自己的卡被拒 | 核对 CONTINUITY_ASSIGNEE/executor_id 与原 assignee 是否一致；不自动把旧连接身份转换为新身份 |
| worker 提前改卡后报告未挂卡 | Alpha29 起已修；核对是否是旧运行、旧 MCP、过期 attempt 或换过执行者，已有历史文件仍可显式挂载 |
| dispatch／resume 持续高 CPU | Alpha35 起先核对当前 execution bindings；无运行 attempt 不重放历史。再查 `dispatch-safety-index.json` 与 change log 尾部偏移；不要删除 change log |
| host 容量显示 paused／expired 混乱 | Alpha35 的 show 会现算 expired，派发在 reset 后不再被旧记录挡住；clear 可只给 model 并按 `--all-matching` 处理多个 provider 记录 |
| worker 误 push 到 canonical 仓库 | 先核对是否真的启用 canonical-read-only 与 root；绝对 git 路径或其他程序可绕过 wrapper，改用 OS 隔离而不是声称已有沙箱 |
| HTTP 403／Cloudflare 被标成 policy-review | 先核对原始 `output_tail`；若没有明确模型安全标记，resume 会按 HTTP/鉴权证据重分类。仍无法自动判断时，复核精确 job 后使用 `worker clear-policy-review --reason …` |
| hook 故障影响体验 | 临时 `CONTINUITY_DISABLED=1`，保留日志与原配置；不要删除账号数据 |

升级前保存软件版本、配置备份、注册表与自定义产物位置。回滚软件后恢复对应 hook/MCP 入口并重连。启用 guarded ledger v4 的项目不能指望旧 dispatcher 继续读取；保留升级前备份，先协调参与者，不擅自把新 schema 改回旧值。主机重启后的遗留 reservation 用 `controller reservations --state stale` 查看，并在持有租约时执行 `controller reconcile-host --apply --reason …`。

卸载插件或 CLI 不会自动清空项目记录。不要使用递归删除命令清理整个用户数据根或 provider profile。
