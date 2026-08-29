# 变更记录

[English](CHANGELOG.en.md)

## 0.1.0-alpha.9.1

### 修复与改进

- 修复 Codex 插件在缺少 `PLUGIN_DATA` 时无法持久化 Session 绑定、观测记录和恢复预算的问题，改用稳定的用户级插件数据目录作为回退。
- 支持 `continuity_resume` 的短工具名与完整 MCP 工具名，确保不同 Codex 宿主的恢复后绑定一致。
- 副作用命令优先按已注册 delivery workspace 解析治理根，并校验 repository digest、允许的 effect 和唯一匹配，避免多项目 Session 被陈旧 cwd 路由。
- 补充公开插件的 `PreToolUse` effect-scope 门：无 claim、scope 不匹配或工作区未注册时，在命令执行前拒绝 push、merge、部署和发布。

### 验证

- public smoke/contracts/benchmark `5/5` 通过，插件脚本编译和 JSON 合同校验通过。
- 本补丁继续保持 alpha.9 的公开脱敏边界；业务仓库、内部状态和原始会话不进入公开树。

### 安装

```bash
python -m pip install continuity-plane==0.1.0a9
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.9.1
codex plugin add continuity-plane@continuity-plane
```

## 0.1.0-alpha.9

### 相比 0.1.0-alpha.8

- 一个 Codex Session 可以显式绑定多个独立项目根。MCP 启动不再从 `cwd` 预绑定；每次
  成功的 `continuity_resume(root=...)` 加入项目集合并切换 active root。不同项目的
  claim、checkpoint、revision 和副作用权限保持隔离。
- 已建立 Session binding 后，未绑定 root、缺少 profile、profile digest 失配或损坏
  binding 会在 CLI/State 写入前 fail-closed，终端 `cwd` 不能覆盖显式项目身份。
- 治理根与外部 delivery workspace 可以在同一 Session 中交替使用；外部仓库仍由
  workspace registry、repository digest、expected HEAD/ref 和 `repo://` scope 约束。
- 修复 packaged MCP 与 Codex plugin 的多根路由合同，并将 server handshake 版本同步
  到 alpha.8 协议线；补充 legacy 单根 binding 迁移和多根 profile 完整性校验。

### 验证与边界

- MCP binding、plugin lifecycle、跨根切换、未绑定写拒绝和严格 schema 聚焦测试
  `46/46` 通过；ruff 检查通过。
- 两个独立治理根的显式双项目 resume 在无关 workspace cwd 下通过；项目身份按请求
  root 返回，未修改业务仓库或直接写入 SQLite。
- alpha.9 仍为预发行版；跨项目自动合并 Work、跨设备唯一 claim 和跨项目 token A/B
  仍按各项目 runtime profile 与后续验收门执行。

### 安装

核心包：

```bash
python -m pip install continuity-plane==0.1.0a9
```

Codex plugin：

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.9
codex plugin add continuity-plane@continuity-plane
```

安装或升级 plugin 后请新建 Session。跨项目 Session 在每个项目首次工作前显式调用
`continuity_resume(root=...)`。

## 0.1.0-alpha.8

### 相比 0.1.0-alpha.7

- 公开标签树相对 `v0.1.0-alpha.7` 的差异为 `22` 个文件（`2` 个新增、`20`
  个修改，`+2,081/-166` 行）；alpha.1 的完整能力清单仍见下方 alpha.7 审计与
  alpha.1 初始条目，以下条目只记录 alpha.8 新增的用户可见变化。

- 新增 `continuity autorun` 与 `continuity_autorun` MCP 工具：checkpoint 已验证且
  权限有效时，同一 Session 自动回到当前 Work；同一 checkpoint 使用本地幂等记录，
  不重复产生 State Event；lease 临近自动 heartbeat，过期按受控 reclaim 换发 claim；
- MCP/插件遇到短暂 transport closed、connection reset 或 timeout 时，在同一项目根
  重试并保持绑定；耗尽重试后返回明确 `failed_gate`；
- attach refresh 后的 successor activation 可以在同一 State Event 原子绑定新的
  canonical source evidence，不再因 evidence 尚未进入 State 而拒绝激活；
- effect intent 资源键现在区分 provider、host、repository、worktree 和 branch，
  同一资源的不同 Session 仍互斥，不同仓库不会互相阻塞；
- public wheel 现在同时安装 `continuity` 和 `continuity-mcp`，Codex plugin 的 MCP
  入口不再依赖用户手工放置宿主脚本；
- source-control 的 push、PR、merge 或 release 权限现在覆盖其必需的本地 commit
  前置动作，但不会扩张为其他外部副作用权限；
- `git tag --list`、`gh release view` 等只读查询不再进入副作用门；
- 同一 Session 的连续副作用操作不再互相冲突，其他 Session 仍受仓库级 intent
  fencing 约束。

### 验证与边界

- 控制面、MCP binding、plugin lifecycle、activation 和 autorun 聚焦测试 `61/61`；
- 脱敏 live snapshot 在同一 MCP Session 中 `continued → already-continued`，重复调用
  不产生 State Event；原项目状态与未通过的外部阻塞均保持不变；
- public smoke/contracts/benchmark `5/5`，wheel/sdist `twine check`、公开隐私扫描、
  Linux/macOS/Windows 安装矩阵和 repository verification 通过；
- alpha.8 仍是预发行版，Docmost connector、Obsidian Canvas/Bases、shared-strong
  一键部署和跨项目 matched token A/B 不构成完成声明。

### 安装

核心包：

```bash
python -m pip install continuity-plane==0.1.0a8
```

Codex plugin：

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.8
codex plugin add continuity-plane@continuity-plane
```

安装或升级 plugin 后请新建 Session。核心包可以单独使用；plugin 的状态变更只能通过
受控 State MCP 工具提交。

## 0.1.0-alpha.7

### 审计范围

- `v0.1.0-alpha.1` 与 `v0.1.0-alpha.7` 的公开标签树差异为 `28` 个文件：
  `16` 个新增、`12` 个修改、`+10,282/-94` 行；
- 对应开发来源从 alpha.1 候选边界到 alpha.7 标签来源包含 `27` 个提交，按用户可见
  结果归并为安装与发行、现有项目接入、状态与任务生命周期、宿主集成、交付门禁和
  文档治理；
- 公开仓库使用脱敏发行投影，标签历史可能不构成单一直线；功能差异以不可变标签树、
  发行 artifact 和测试结果为准，不以提交数量代替功能证据。

### 本地运行时与 CLI

- 默认 `local-embedded` 继续使用项目内 SQLite，不要求 PostgreSQL、容器、Docmost
  或网络服务；
- CLI 增加并公开 `status`、`attach`、`resume`、`checkpoint`、`work`、`export`、
  `import` 和 `rollback` 生命周期；
- `attach plan/refresh/approve` 可将已有 canonical MASTER/STATUS 以 source hash、
  evidence 和幂等批准接入，源变化会拒绝陈旧 proposal；
- 本地 state bundle 的 export/import/rollback 已实现并通过篡改拒绝、原子替换和
  无状态丢失回滚测试；跨 adapter 的一键 profile switch 仍未提供；
- current-only STATUS projection、bounded resume packet、immutable checkpoint 和
  sticky route apply 共同保存 active Work、首动作、return point、effect watermark
  和已确认输入；
- Work complete、dependency suspend/return transition、idle successor activation 和
  claim heartbeat/reclaim 都在单一 revisioned 边界中刷新并验证 checkpoint，失败时
  不留下半完成状态。

### 项目根、协作与交付门禁

- Git common-dir 将主目录与 sibling worktree 绑定到同一项目状态，避免旁路 Session
  使用另一份 `.continuity/`；
- canonical source 合法变化时，active owner 可在 actor、claim、lease、scope 和
  checkpoint 验证后原子重绑 evidence；
- delivery Work 激活绑定 source、前序 Work、实现 evidence、expected Git head/ref、
  未提交 worktree delta 和精确 effect 集合；
- push、PR、merge、deploy、远端安装和 package publish 在 shell 执行前检查 active
  Work、claim、lease、source、checkpoint 和 scope；
- 本地 `rsync` 不再被误判为远端操作，包含远端端点的传输仍受门禁约束。

### Codex plugin

- 新增公开 marketplace 和 plugin bundle，包含项目根发现、有界状态注入、生命周期
  checkpoint、状态工具和副作用预检；
- 新 Session 只显示一次项目与 authoritative revision，不输出完整 Work、packet
  或原始对话；
- plugin 不直接修改数据库。其写操作只能调用受 authorization、revision/CAS、
  validator、claim 和 checkpoint 约束的 State MCP 工具。

### 发行、文档与验证

- 补齐中英文 README、USAGE、架构、配置、API、场景、benchmark、大型项目视图、
  图形化产品、贡献、安全、品牌和第三方声明；
- 发行编译器输出 Python wheel、source archive、Codex plugin marketplace、
  `SHA256SUMS`、中性模板和隐私扫描后的公开历史；
- public smoke/contracts/benchmark `5/5`、wheel/sdist `twine check`、公开隐私扫描
  `0` 条违规；Linux、macOS 和 Windows 安装/verify/卸载矩阵保持 `18/18`；
- benchmark 仍是匹配场景和脱敏 fixture 的结论。跨项目真实 Session 的 token、
  窗口利用率和每次压缩完成 Work 数尚未形成通用节省率；
- 完整 Docmost connector、Obsidian Canvas/Bases、provider-neutral Context Health
  export 和 shared-strong 一键部署未包含在 alpha.7。

### 从 alpha.1 升级

1. 备份完整 `.continuity/`；
2. 安装 `continuity-plane==0.1.0a7`；
3. 运行 `continuity verify --root .` 和 `continuity doctor --root .`；
4. 已有 canonical MASTER/STATUS 的项目使用 `attach plan` 与 `attach approve`，不要
   用生成模板覆盖原文件；
5. Codex plugin 是 alpha.7 新增的可选安装，不影响只使用核心 CLI 的项目。

### 安装

核心包：

```bash
python -m pip install continuity-plane==0.1.0a7
```

Codex plugin：

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.7
codex plugin add continuity-plane@continuity-plane
```

安装或升级 plugin 后请新建 Session。核心包可以单独使用；plugin 的状态变更只能通过
受控 State MCP 工具提交。

## 0.1.0-alpha.1

- 使用 Continuity Plane 作为首个公开产品身份；
- 采用 Apache-2.0，并提供 NOTICE 和第三方声明；
- 提供可选品牌规范和静态 `Managed with Continuity Plane` badge；
- 增加 local-embedded 项目初始化和验证 CLI；
- 增加 revisioned state、append-only events、claims、checkpoints 和 replay gates；
- 增加 bounded Execution Packet、Skill 选择、证据 lineage 和检索 receipt；
- 增加协作 claim、通知、handoff 和本地 unattended workflow；
- 提供可复验 benchmark 和隐私门控的公开历史编译器；
- 发布 PyPI 包 `continuity-plane==0.1.0a1`；
- Linux、macOS 和 Windows 安装、verify、卸载通过；本地 state bundle
  export/import/rollback 在 alpha.1 中尚未提供。
