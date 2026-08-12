# Documentation Lifecycle Policy

版本：1  
日期：2026-08-09  
状态：implemented policy / deterministic repository validator

## 目标

文档系统同时保存长期治理、动态路由、协议合同、研究证据和生成投影。每类文档具有单一职责、权威级别、更新触发和生命周期，避免动态事实滞留在长期文件，也避免主线文件吸收无界细节。

## 文档分类

| 类别 | 规范文件 | 权威内容 | 更新触发 | 过期处理 |
|---|---|---|---|---|
| Governance | `MASTER.md` | 使命、边界、DAG、任务合同、生产门 | 用户或治理 owner 批准的计划/边界变化 | revision + event；旧版本保留 provenance |
| Routing | `STATUS.md` | 当前 active leaf、blocker、next action、恢复入口 | active leaf、blocker、revision 或 next action 变化 | 覆盖当前值；历史进入 event/migration |
| Policy | `docs/policies/` | 准入、权限、生命周期、写保护和验收规则 | 协议或治理规则变化 | version、兼容窗口、migration/replay |
| Architecture | `docs/architecture/target-state.md` 与其他 `docs/architecture/` | 目标态组件、合同、接口和权限边界 | 架构决策、schema 或依赖方向变化 | superseded 标记，链接替代合同 |
| Research | `docs/research/` | 研究方法、来源、实测和采用边界 | 新实测、来源快照或结论变化 | 保留原 snapshot，新增 revision，不回写历史结论 |
| Migration/Acceptance | `docs/migrations/` | 批次、验收、迁移和回滚证据 | 一次迁移或验收完成 | immutable record；纠正用 supersedes |
| Projection | Docmost/Obsidian/reports | 人类观察和汇报视图 | 状态或报告生成 | 重新生成；报告和 Obsidian 无权威提交权限；Docmost 只通过 State MCP 受控入口操作 |

## 更新门

文档变更必须在 `profiles/document-control-config.yaml` 声明 `change_type`：`correction`、`decision`、`status`、`evidence`、`schema`、`projection` 或 `style`，并绑定：

```yaml
document_id: stable-id
document_revision: uint64
change_type: correction | decision | status | evidence | schema | projection | style
authority_ref: user | governance-event | state-revision | evidence-ref
supersedes: document-revision | null
affected_tasks: [task-id]
next_review: rfc3339 | null
```

规则：

- `MASTER.md` 的使命、边界、DAG 和完成门只接受治理授权；动态任务事实不写入其中；
- `STATUS.md` 只保留短路由字段，超过恢复所需范围的内容转移到对应 policy、architecture、research 或 migration 文件；
- `docs/architecture/target-state.md` 保存目标组件、权限、依赖和交付归属；当前 task/blocker/Session 事实不写入其中；
- research、report 和 projection 不得直接改变 active state；承重断言必须引用 current provenance；
- 历史、失败尝试和被 supersede 的决定保留在 event/migration/research 记录，当前文档只链接有效结论；
- 文档内容、引用、结构和链接变化都必须通过 style、schema、reference 和 admission checks；
- 发现冲突时先建立 correction/supersedes 事件，禁止静默覆盖。

## 容量与扩展

每个规范文档都维护可比较的 UTF-8 bytes、章节数、外链数和引用深度。达到以下任一条件时必须拆分或转移：

- `STATUS.md` 超过 12 KiB 或恢复入口需要读取超过两次；
- `MASTER.md` 的单个二级章节超过 24 KiB，或同一动态事实在三个以上文件重复维护；
- 章节包含超过 3 个独立实验、组件评估或迁移批次；
- Execution Packet 需要展开完整文档才能通过 canary；
- 引用 freshness、schema version 或权限边界无法在当前 revision 内证明。

拆分后的父文档保留稳定摘要、子文档 ID、revision/hash 和返回链接。禁止通过重复复制全文解决可发现性问题；使用 bounded lookup、artifact range、索引和 receipt 定位细节。

## 生成、归档与恢复

Docmost、Obsidian 和报告属于生成或受控操作视图。生成物记录源 state revision、MASTER digest、template/version 和 content hash；人工意见通过 State MCP、治理事件或 correction proposal 回流。旧文档保留为 immutable snapshot 或明确 `superseded`，不得让旧版本重新进入 active packet。

`context.document-control-manifest/v1alpha1` 由 config 和当前文件确定性生成。CI 离线重算 UTF-8 bytes、章节数、链接数、STATUS 恢复引用图深度、文档 hash 与 evidence hash；恢复图中的 report/projection 可继续展开，其他类别作为承重终点，循环和超过两跳的路径拒绝通过。普通验证不对外部 URL 发起请求。外部 freshness 使用 catalog snapshot、trusted time 和 ReferenceWatcher receipt。既有文档通过首次 manifest 按 path/hash 固定迁移，新增规范文档必须登记，禁止新增无 metadata 债务。

压缩、handoff、任务切换或中断恢复时，只读取 `STATUS.md`、对应 MASTER 章节、当前 policy/architecture 合同和 packet 引用。历史文档、报告和 memory 保持 candidate，须通过 current-evidence verification 后才能展开。

## 验收

- 文档分类、authority、revision、supersedes 和 change trigger 可机器解析；
- 未授权 MASTER 使命/DAG/门禁修改率为 0；
- 动态状态进入 `STATUS`/event/state 的比例为 100%；
- 发现的重复全文、过期链接和未绑定 provenance 均被检查拒绝；
- 文档拆分后 packet 的承重字段恢复率保持 100%，直接读取 bytes 与重复检索量按 E5/M6-07 计量；
- 至少三个相同 task/provider/budget 样本后才能声明文档读取或上下文成本趋势；
- correction、supersedes、rollback 和重新生成均可从 revision/hash 重放。
