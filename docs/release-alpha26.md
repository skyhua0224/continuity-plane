# Alpha26 · 顺手协作与可靠报告

Smoother collaboration and reliable reports

包版本 `0.1.0a26`；tag／插件版本 `v0.1.0-alpha.26`／`0.1.0-alpha.26`。

## 这一版带来什么

- worker 提前把卡改为待评审、完成或 merged 后，当前 attempt 的最终报告仍自动挂卡；不回退手工状态。旧 attempt、重新指派后的迟到报告不能覆盖当前附件；写入失败保留可重放事件。
- MCP 可声明稳定 `executor_id`，或配置 server 环境 `CONTINUITY_ASSIGNEE`；重连不再用连接 ID 改卡片归属。卡片 Todo 激活有独立 `--assignee`；其他执行者抢占仍被拒绝。
- hook 给出已知上下文的完整命令，不捏造写入参数；超长清单提醒去重，已有卡片工作流不自动再维护第二份 Todo。
- `reference import-tsv/export-tsv` 连接仓库来源索引；冲突整批拒绝，导出只写新文件，不自动覆盖仓库文件或提交 Git。
- search MCP 可选择显式 SSH 仓库映射，避免本机与远端 root 混用。没有任意远端文件／进程接口，不转移原生账号、会话或额度数据。

## 来源索引往返

```bash
continuity reference import-tsv --root /workspace/project --file docs/authority/source-index.tsv
continuity reference export-tsv --root /workspace/project --file source-index.export.tsv
```

输入文件相对路径按命令工作目录解析；`--root` 选择资料库。UTF-8 TSV 的必要列是 `reference_id`、`title`、`source`；别名 `key/id`、`url` 可用。可选列 `kind`、`version`、`section`、`platforms`、`topics`、`applicability`；缺 kind 默认为 supplementary。列表用 JSON 数组或分号分隔，额外列保留。最多 1,000 条／4 MiB；相同键同内容幂等，内容冲突或无效行拒绝整批。导入是候选声明，不自动生成已读证据；本接口不自动同步文件。

## 升级与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a26'
continuity --version
```

pipx 已有安装使用[部署说明](deployment.md)的原环境升级命令。插件选择同一 tag，已有 MCP 连接需重连。本版本不主动迁移老卡的 session 风格 assignee：为旧身份设置相同稳定值，或显式记录一次重新指派理由。卡片投影的 Todo 激活需要 assignee，不再把 session_id 当执行者。未新增强制账本 schema 迁移；Alpha25 的 guarded v4 兼容限制仍适用。

远端 search MCP 启动环境可设 `CONTINUITY_BRIDGE_CONFIG=/path/to/bridge.json`，或工具传 `bridge_config`。配置精确映射 local_root/remote_root；未匹配、重复映射、SSH 失败时不回退本机。仅所属机器读取检索缓存与注册表，本机 cache_path/data_root 不被转发。详见[远端部署](deployment.md#远端检索)。

## 验证与限制

新增 13 项合成回归覆盖报告、重放、身份、TSV、提示和远端协议。公开 CLI 合成脚本要求提前改卡后的报告自动可读，不再接受手工挂载作为通过条件。完整回归、构建和发布结果以当前 tag CI 为准。未调用真实 provider，未在 Alpha26 重新做原生压缩、自然子代理生命周期或真实双机测试；无统一 token／费用节省承诺。

## English

Final reports now attach after early card status changes while preserving manual status and current-attempt/executor guards, including replay after interrupted metadata writes. Stable MCP executor_id/server CONTINUITY_ASSIGNEE survives reconnects; card-backed Todo activation takes assignee, not a connection ID. Other executors remain unable to take over without an audited override.

Hook guidance uses complete commands from known context, deduplicates oversized-checklist advice and avoids a second automatic Todo for card-based work. Explicit reference TSV interchange preserves keys/extra columns, rejects conflicts atomically and never overwrites an existing export destination. Imports are candidate declarations, not proof of reading. The same commands above apply: required reference_id/title/source (aliases key/id/url), optional kind/version/section/platforms/topics/applicability, JSON or semicolon lists, 1,000 rows / 4 MiB. Relative file paths use the command working directory. This is explicit interchange, not automatic file synchronization.

Mapped search uses tool bridge_config or server CONTINUITY_BRIDGE_CONFIG with an exact local_root/remote_root mapping. Missing/ambiguous routes and SSH failures never fall back locally. Local cache/data paths are rejected; native credentials, sessions and account counters are never transferred for querying. No arbitrary remote file/process tool is added.

Install the exact package pin above and matching plugin tag, then reconnect MCP clients. Existing session-style assignees are not silently migrated: retain that stable value or perform one explicit reasoned reassignment. No new forced ledger migration is introduced; Alpha25 guarded-v4 restrictions still apply. GitHub publishes this as a regular Release; the alpha compatibility stage remains.

Thirteen new synthetic regressions cover reports/replay, stable identities, TSV, notices and mapped protocol execution. The CLI fixture now requires automatic early-update report delivery. Complete test/build/publication results belong to tag CI. No new real-provider, native compaction/subagent or physical two-machine field trial is claimed; no universal token/billing saving is promised.

[部署 / Deployment](deployment.en.md) · [工作流 / Workflows](workflows.en.md) · [验证 / Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
