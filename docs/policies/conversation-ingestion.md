# Conversation Ingestion Policy

版本：1  
日期：2026-08-09

## Storage Classes

| 类型 | 示例 | 默认位置 | Git | 状态权限 |
|---|---|---|---|---|
| Raw transcript | Codex/Claude JSONL、rollout、隐藏 provider 字段 | 原供应商目录或加密对象存储 | 禁止 | 无 |
| Handoff | `state.md`、阶段交接、project memory | 派生 memory store | 默认不入；审查后可引用 | 候选 |
| Typed event | decision、work、evidence、effect 事件 | PostgreSQL | schema/migration 可入 | 权威，需 CAS |
| Artifact | 构建日志、diff、profile、测试输出 | content-addressed store | 只存小型 fixture | 证据 |
| Replay fixture | 脱敏最小输入、期望状态和故障 | `replay/fixtures/` | 允许 | 测试 oracle |
| MASTER | 人类主线意图与治理计划 | Git | 允许 | governance authority；变更走治理流程 |
| Docmost | 人类控制台、审批、纠偏、promotion 与审计入口 | Docmost DB + State MCP provider | 不适用 | 治理动作受 authorization、revision/CAS、validator 和 promotion gate 约束 |
| Obsidian | 生成的只读 vault | generated vault | 生成物按独立 policy 管理 | 无权威状态提交权 |
| 报告与目标态架构全表 | 治理与汇报投影 | Git/Docmost | 允许 | 承重断言需 current provenance；不能自行改变 active state |

## Admission Rules

聊天内容只有满足全部条件才能进入 typed state 或 replay fixture：

1. 已绑定稳定 `project_id` 和 opaque `source_thread_ref`。
2. 已移除访问 token、Cookie、私钥、凭据、个人信息和机器专有敏感数据。
3. 已删除与验证目标无关的模型措辞、重复对话和大工具输出。
4. 已标明 `candidate / verified / stale / rejected`，历史叙述默认是 `candidate`。
5. 承重事实已经对照当前代码、当前日志或版本化官方来源。
6. fixture 同时包含预期结果、失败条件和最小触发输入。
7. 保存来源 hash 和 extractor version，但不保存 provider 隐藏 reasoning。

## Migration Flow

```text
inventory metadata
-> classify project and thread
-> sanitize
-> extract material events
-> verify current validity
-> write typed candidate or replay fixture
-> independent validator
```

Extractor 使用流式读取。超过 100 MB 的档案先生成 metadata/handoff；fixture 所需内容按 byte range 提取。

Opaque source ref 使用项目批准的 HMAC namespace secret。生产/受控离线路径从 secret manager、受权限保护的文件或进程注入读取至少 256-bit 的 base64url secret；命令行参数、Git、fixture、receipt、日志和 registry document 禁止保存 key material。公开记录只保存小写 `opaque_key_id`，key rotation 通过新 key ID、migration 和 replay 处理。

## Retention

- Raw transcript 的保留、加密和删除由 archive policy 管理，与 Git 生命周期分离。
- Typed state 通过 append-only 事件和 supersedes 保留完整变更历史。
- Replay fixture 永久保留最小必要内容；来源被删除后仍保留不可逆 opaque hash。
- 用户请求删除时，删除 raw/derived content，并以 tombstone 保存审计所需的最小非内容元数据。
- 不在公共 issue、CI 日志或 PR 中输出原始对话片段。

## Current Migration Result

- 已完成现有来源的只读元数据盘点。
- 已迁移上下文可靠性评估文档。
- 未复制任何 Codex/Claude JSONL、SQLite memory、handoff 或原始聊天正文。
- replay fixtures 需要经过 sanitizer 与 current-evidence verification 后逐条导入。
