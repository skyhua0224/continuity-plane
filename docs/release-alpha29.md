# Alpha29 · 主控连续性与分域记忆

Controller continuity and scoped memory

包 `0.1.0a29`；tag／插件 `v0.1.0-alpha.29`／`0.1.0-alpha.29`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- `controller start` 用一个有界包恢复计划、下一步、待决事项、交流／报告／提问／工具 profile、获批相关记忆和仓库共享知识，不默认扫描整个项目。
- 记忆明确分为项目共享约定、成员私人 profile 与项目已批准知识。候选不会自动注入；撤销、过期、revision 冲突和项目锁定字段可见。
- Claude／Codex 原生笔记采用原机、profile、项目和具体文件绑定的一次授权；此后 `controller start` 自动增量刷新这些文件，但只导入候选。不递归发现、不跟随链接、不读取账号／配置／会话，默认也不开放私人 MCP。
- 主控交接的快速路径先保存声明，现场观察按需现算；SessionStart 给主控直接可执行的恢复命令。报告、提问和工具习惯成为可恢复的工作合同。
- inbox 新增已确认消息归档；额度／限流／容量按已观察的 origin／model 暂停，未知 reset 不伪造；预留错误区分运行中重复与陈旧／未观察到；新增顺序 JSON 批量更新。
- Gitea 默认 ignore 白名单增加 `controller-profile.json` 与经人工导出的 `knowledge/*.json`，缓存、游标、操作日志和原生配置仍留在成员本机。

## 使用入口

```bash
continuity controller start --project-id demo --member alice --format brief --max-output-bytes 4096
continuity profile show --project-id demo --member alice --format brief
continuity memory notes --project-id demo --member alice --format brief
```

详细权限、存储、原生接入和 ACK 语义见[主控连续性与分域记忆](controller-memory.md)。私人 MCP 只有所属本机明确设置 `CONTINUITY_PRIVATE_MEMORY_MCP=1` 才可访问；共享服务器只用 `--public-only`。

## 安装与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a29'
continuity --version
```

已有 pipx 环境使用 `pipx upgrade continuity-plane --pip-args=--pre`。插件选择同一 tag，升级后刷新插件并重连 MCP；正在运行的 provider 不需要为安装而被终止。此版不修改原生账号、Token、会话或现有仓库权限，也不自动迁移旧私人记忆。

## 验证与限制

本版验证覆盖：候选／批准／撤销／过期与跨成员／项目隔离；项目锁定 profile；原生来源 origin、symlink、控制文件与链接限制；有界恢复及 ACK；inbox 归档重试；quota 暂停；reservation 诊断；顺序批量更新；CLI／MCP 私人读取门。完整公开回归、构建和 tarball／wheel 结果见 tag CI 与本版验证文档。

原生互通当前是只读候选导入。原生写回、跨设备／成员迁移、内容自动共享、模型内部状态逐字复制和真实全量 token 净收益不在本版承诺范围。不同 OS 用户的私有数据依靠文件权限；同 OS 用户的多个 Agent 需要宿主 sandbox 才能形成进程隔离。`.gitignore` 是 Git 准入规则，不能替代访问控制。

## English

Alpha29 adds a bounded `controller start` packet that restores the declared plan, next action, pending decisions, communication/reporting/question/tool profile, approved relevant memory and reviewed repository knowledge without a full project scan.

Memory is scoped across shared project conventions, private member preferences and approved project knowledge. Candidates never enter ordinary recovery packets automatically; revocation, expiry, revision conflicts and locked project fields remain visible. Native Claude/Codex notes require one consent bound to the owning machine, profile, project and exact files. Imported notes remain candidates; Continuity does not recursively discover files, follow links, read account/configuration/session data or mutate native files. Private MCP access is disabled by default and shared servers use public-only context.

Fast handoff writes save declarations before observations. SessionStart exposes a concrete controller recovery command. Inbox archive preserves unread messages and suppresses duplicate resends; observed quota/rate/capacity failures pause the exact origin/model without inventing unknown resets; reservation errors distinguish duplicate-running from stale/unobserved attempts; bounded JSON batch updates stop on first failure. The Gitea default ignore file permits reviewed `controller-profile.json` and knowledge exports while runtime data stays member-local.

Install the exact pin above, refresh matching plugins and reconnect MCP. No native credentials, sessions, account data, repository permissions or old private memory are migrated automatically. This release claims read-only native candidate ingestion, not native writeback, cross-member memory transfer, literal model-state replication or universal token savings. Same-OS-user agent isolation requires host sandboxing; ignore rules are not access control.

[Guide](controller-memory.en.md) · [Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
