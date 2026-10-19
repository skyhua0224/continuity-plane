# Alpha27 · 私有 Issue／PR 协作与可靠接手

Private Issue/PR collaboration and reliable recovery

包 `0.1.0a27`；tag／插件 `v0.1.0-alpha.27`／`0.1.0-alpha.27`。GitHub 普通 Release；alpha 兼容阶段保留。

## 这一版带来什么

- 私有 Gitea Issue／PR 作为协作后端。成员各跑本机 CLI／AI，使用自己的本机 tea 登录；无 Continuity 服务、共享数据库或永久主控。
- 现有 task CLI／MCP 路由共享任务，支持创建、查看、筛选、更新、认领、释放、阻塞、请求与接受交接、关联 PR 和真实请求评审。AI 身份与人类驱动者分离。
- Issue 与事件评论是任务权威；修改携带观察 revision，追加事件而不整段覆盖网页修改。并发显式报冲突；离线未发送和发送后未知结果分开记录，未知结果不盲目重试。
- sync 提供有界分页、持久 consumer、完整阅读后 ACK；读取真实 PR 评审，PR 关闭不能冒充已合入。认领明确为协调模式，不承诺全局强锁。
- 仓库 `.continuity`／用户数据根的[配置与准入合同](collaboration-storage.md)，首次 configure 创建安全 `.gitignore`，已有内容保持不变；无隐式全局 config.toml。
- 主控恢复区分声明计划与读时观察；对账终态 attempt、报告晚于卡片、未确认消息。新增自动交接草稿、持久变化游标与顶层 task 别名。
- 派发身份、模型和卡片快照重新校验；拒绝改派不能继续按旧正文派工。区分上游重放拒绝与普通限流，保留策略／认证拒绝的停止边界。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a27'
continuity --version
continuity help backend
```

已有 pipx/uv 环境使用[部署说明](deployment.md)的原解释器升级命令。插件选择相同 tag，升级后重连 MCP／重启宿主加载；安装包升级不会自动替换运行中的代码。Gitea 需要支持 api --include / --data 的本机 tea（实测 0.15.0）。

不用为 Gitea init State：register 后 backend configure／doctor／sync。仓库只提交声明与版本化规则，快照、游标、待发布日志留在成员本机。默认全局数据根为 ~/.local/share/continuity；使用 ~/.continuity 时显式设置 CONTINUITY_DATA_ROOT。不迁移旧本地卡、原生账号、Token 或完整会话，不改仓库保护／成员权限。

## 验证与限制

本机 tea 0.15.0 连接隔离 HTTPS Gitea 1.24.7，两个合成账号、两个数据目录和一个私有测试仓库，验证共享任务、认领、接受后交接、网页修改保留、真实 PR 请求评审与未合入保护。并发认领、模糊网络结果、重试去重和离线协调由确定性合成测试覆盖；完整本版回归／构建结果见 tag CI。未操作真实业务仓库，不宣称跨机器强锁、服务器 CAS、恰好一次或真实私有部署全部验收。

任务驱动者记录在 Issue 事件评论；与原生侧栏 assignees 不一致时明确冲突，不自动覆盖。全局规则／参考资料／本机恢复信息不自动同步给队友。共享项目不走本地 worker dispatch 或本地 merged 快捷路径；PR 创建、合并和提交评审仍使用原有 Forge 工具。无统一 token／费用节省承诺。

## English

Alpha27 adds native private Gitea Issue/PR collaboration through each member's own local tea CLI. Existing task CLI/MCP operations cover CRUD, human-account claims/releases, blockers, requested/accepted handoffs, linked PRs and real reviewer requests. No Continuity server, shared database or permanent controller is required.

Issue/event comments own task state; append-only application updates preserve web edits and expose concurrent conflicts. Claims are coordination-only, not server leases. Unsent operations and ambiguous sends are separate; unknown outcomes reconcile without blind resend. Bounded sync pages retain durable consumers and reject early ACK. Closed PRs are not delivered; completion verifies the merged fact and merge commit.

The [configuration contract](collaboration-storage.en.md) separates committed declarations/policy from member-local cache, journals, cursors and native login. Default global data root is ~/.local/share/continuity; ~/.continuity needs explicit CONTINUITY_DATA_ROOT, not an implicit config.toml. First configure creates a safe repository ignore file only when absent.

Controller recovery now reconciles live observations, finished attempts, orphan reports and unread messages separately from declared plans. Persistent change cursors, task aliases and dispatch identity/model/body fences prevent stale dispatch. Upstream replay refusal is distinct from ordinary rate limiting; policy/authentication stop boundaries remain.

Install the exact pin above and matching plugin tag, then reconnect/reload. Existing pipx/uv users upgrade through the original interpreter as documented. Gitea requires local tea api --include / --data (verified 0.15.0). No automatic account/session migration, repository permission changes or old-card upload occurs.

The real isolated fixture used HTTPS Gitea 1.24.7, two synthetic accounts/data roots and a private repository. Races, ambiguous writes and retry behavior use deterministic synthetic tests. See tag CI for this release's full regression/build. This is not proof of acceptance on a real business deployment. No strong-lock, CAS, cross-machine exactly-once, universal savings or remote AI wakeup claim is made. Accepted driver state lives in Issue comments; native sidebar discrepancies remain explicit conflicts. Local rules/recovery are not automatically shared. Create/merge PRs and submit reviews through existing Forge tools.

[Gitea 使用](gitea-collaboration.md) · [配置](collaboration-storage.md) · [Deployment](deployment.en.md) · [Changelog](../CHANGELOG.en.md)
