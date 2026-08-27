# 变更记录

[English](CHANGELOG.en.md)

## 0.1.0-alpha.7

### 相比 0.1.0-alpha.1

- 将本地 SQLite 状态、checkpoint、event replay、claim/lease 和 evidence gate 的运行时边界补齐；
- 增加 Codex plugin 公开 bundle，包含 SessionStart、PreCompact、PostCompact、PreToolUse、PostToolUse hooks、MCP server 和连续性 Skill；
- 新 Session 自动发现项目根目录并显示一次性启动确认；压缩恢复继续使用有界 packet，不输出恢复旁白；
- 增加 Git common-dir 主目录/worktree 统一绑定，阻止旁路 Session 绕过同一份项目状态；
- 增加 source-stale owner heartbeat/reclaim 的原子自恢复：只在 actor、claim、checkpoint 和 scope 满足条件时重绑当前 canonical source；
- 增加 idle-to-delivery 原子激活，绑定 source、前序 Work、实现证据、基线 HEAD、未提交 worktree delta 和精确 effect scopes；
- 修复本地 `rsync` 被误判为远端副作用的问题，真实远端传输仍受 effect gate 控制；
- 公开发行编译器现在同时输出 Python wheel、Codex plugin 和公开 marketplace；
- 公开 benchmark、隐私扫描和跨平台安装验证继续保留，不能把缓存命中率当作 token 节省；
- `0.1.0-alpha.7` 是公开 alpha，完整 Docmost connector、Canvas/Bases 和 shared-strong 部署仍属于后续计划。

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

安装或升级 plugin 后请新建 Session。核心包可以单独使用；plugin 只是宿主接入层，不拥有
权威状态写权限。

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
- Linux、macOS 和 Windows 安装、verify、卸载通过，迁移与 rollback 仍在 M10-09。
