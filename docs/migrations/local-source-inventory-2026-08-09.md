# Local Conversation Source Inventory

日期：2026-08-09  
范围：只读文件元数据；未读取原始聊天正文

| 来源 | 观察规模 | 处理决定 |
|---|---:|---|
| `~/.codex/sessions` | `du` 约 33 GB | 受控原地归档 |
| `~/.claude/projects` | `du` 约 3.4 GB | 受控原地归档 |
| 两处 JSONL 合计 | 2,393 文件；37,982,681,786 bytes | 仅由流式 extractor 按需读取 |
| 超过 100 MB 的 JSONL | 167 文件 | 先 handoff/index，再做 byte-range 提取 |
| `~/.codex/session-memory` | `du` 约 27 MB | 作为派生候选，不作为当前事实 |
| `memory.sqlite` | 28,069,888 bytes | 未来通过只读 importer 消融评估 |

已迁移：

- `docs/research/context-reliability-assessment-2026-08-09.md`
  - 来源快照 SHA-256：`bb5a578272eb06a25eea062cda7f8ce9d82b02844355ac44788bbcc821b99182`
  - 正式文档修订 SHA-256：`5f610c8f4d05a86bc2e0a08e4fe5364b7249caf9a9479262d74409043bef3e9a`
  - 修订范围：语言规范化；研究数据与采用结论保持不变
- 本 inventory 的非内容元数据

未迁移：

- raw rollout/JSONL；
- session-memory SQLite；
- handoff 正文；
- project memory 正文；
- provider 隐藏字段和模型 reasoning；
- 未脱敏日志、diff、凭据和个人数据。

下一批迁移范围：40 个带来源 hash、预期状态和 current-evidence 验证的 replay fixtures。
