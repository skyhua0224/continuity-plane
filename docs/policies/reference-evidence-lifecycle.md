# Reference and Evidence Lifecycle Policy

版本：1  
日期：2026-08-09  
状态：implemented policy / state-service integration planned

## 适用范围

本策略管理行业标准、OS 官方文档、软件官方文档、当前源码、研究论文、供应商工程文章、社区资料和市场目录。它定义来源发现、快照、断言提取、采用、刷新、失效和 supersedes。原始聊天按 `conversation-ingestion.md` 管理，不进入本策略的外部参考目录。

## 对象与权限

| 对象 | 内容 | 权限 |
|---|---|---|
| `ReferenceSource` | canonical URL、publisher、authority class、license 和更新方式 | 发现元数据；不能直接改变治理或运行时状态 |
| `ReferenceSnapshot` | retrieval time、revision/ETag/Last-Modified、content hash 和 acquisition method | 固定某次读取证据；动态网页仍需 freshness 检查 |
| `Assertion` | 从来源提取的最小承重结论、适用版本、validity 和 evidence refs | 通过 M7 claim-evidence gate 后才能支撑完成门 |
| `AdoptionDecision` | adopt、adapt、reject、quarantine、supersede 及理由 | 由治理 owner 或受权 validator 提交 |
| `ReferenceWatcher` | 检测 revision/hash/页面状态变化并产生候选事件 | 写候选和 stale signal；无权自动改 MASTER、active task 或 Product |

来源权威顺序为：当前源码与运行证据、行业标准、OS 官方文档、软件官方文档、供应商工程材料、同行评审研究、组织资料、社区与市场信号。来源权威不能替代适用版本、当前 revision 和独立验证。

## 最小登记合同

```yaml
reference_id: stable-id
title: string
publisher: string
authority_class: current-source | standard | os-official | software-official | vendor-engineering | research | organization | community | marketplace
canonical_url: https-url
retrieval_url: https-url
retrieved_at: rfc3339
source_revision: commit | version | etag | last-modified | retrieval-id
content_sha256: sha256
acquisition: direct-official | git-snapshot | registry-api | proxy-render | manual
license_ref: SPDX-id | source-url | unknown
validity: candidate | verified-current | stale | superseded | rejected
adoption_status: candidate | adopted | adapted | quarantined | rejected
refresh_cadence_days: uint32
refresh_triggers: [before-bearing-use, upstream-release, hash-change, conflict, scheduled]
supports: [task-id | assertion-id | architecture-slot]
```

Git 保存来源元数据、hash、最小释义、采用决定和允许再分发的 fixture。受许可证限制的外部全文保留在 canonical source 或受控 artifact store；Git 不复制完整网页、手册或仓库。动态网页无法提供不可变 revision 时，`retrieved_at + content_sha256 + acquisition` 共同标识快照。

## 生命周期

```text
discover
-> classify authority and license
-> snapshot metadata and hash
-> extract minimal assertion
-> verify scope/version/current evidence
-> adopt, adapt, quarantine, or reject
-> bind to tasks and evidence gates
-> refresh on trigger
-> mark stale or superseded before replacement
```

发现器和模型只生成 candidate。承重断言必须记录来源、适用版本、检索时间和 current-evidence verification。来源发生 hash/revision 变化时，旧断言先进入 `stale` 并产生 `adoption_review_requested`，完成门不得继续引用它；review 产生新 assertion 或显式确认语义未变。来源互相冲突时，同时保留证据并按 authority、版本和当前源码/实测裁决，禁止覆盖冲突历史。

## 持续刷新

| 来源 | 默认刷新门 |
|---|---|
| 当前源码、构建、运行证据 | 每个承重使用点绑定当前 revision；完成前重新验证 |
| 行业标准 | 固定版本；新版本发布、适用实现变化或 90 天 freshness review 时复核 |
| OS/软件官方文档 | 承重使用前复核；provider release、hash 变化或最长 30 天时复核 |
| 供应商工程文章与研究 | 作为架构候选；采用前由官方合同、当前源码或实验补证 |
| 社区与市场 | 仅候选排序；采用前必须提升到可验证来源和本地实验 |

ReferenceWatcher 产生 `reference_discovered`、`reference_changed`、`assertion_stale` 和 `adoption_review_requested` 事件。重复发现按 canonical URL、publisher、scope 和 content hash 去重。定期扫描可以异步运行；扫描失败不得降低已有权威状态的可用性，也不得把旧快照默认为当前事实。

## 有界装载

Execution Packet 只携带当前 active leaf 所需的 assertion ID、单行结论、authority、validity 和 artifact range。完整研究文档与外部来源按 bounded lookup 展开。Reference catalog、历史快照和无关 candidate 不进入 S0/S2，也不授予 Skill、MCP 或工具权限。

## 验收门

- catalog 条目稳定 ID 和 canonical URL 唯一；snapshot 具有 revision 或 retrieval ID 及 SHA-256；
- 承重断言 provenance、适用版本和 current validity 覆盖率为 100%；
- hash/revision、license、publisher 或适用版本缺失时，采用率为 0；
- upstream change fixture 使相关 assertion 100% 进入 stale/quarantine，未复核 assertion 不能通过完成门；
- 社区热度、marketplace audit、memory 相似度和模型判断不能提升 authority；
- 参考刷新、冲突和 supersedes 可从事件与 checkpoint replay；
- bounded retrieval 相对 E0 的重复检索 token 下降至少 30%，承重引用保持 100%。
