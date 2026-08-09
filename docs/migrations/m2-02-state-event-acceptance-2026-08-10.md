# M2-02 State Event Acceptance

版本：1  
日期：2026-08-10  
状态：✅（strict Event schema、deterministic reducer 与 versioned replay fixture 已验证）

```yaml
document_id: context.m2-02-state-event-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-schema-registry-and-replay-fixture-2026-08-10
supersedes: null
affected_tasks: [M2-02, M2-03, M2-05, M2-06, M5-08]
next_review: M2-03
```

## 范围

本验收覆盖 `context.state-event/v1alpha1`、canonical event hash、连续 sequence/revision、前序事件 SHA-256 chain、correction supersedes、deterministic snapshot reducer 和 authority 对象终态转换。每个 replay step 复用 M2-01 semantic validator；输入 snapshot 与 Event 列表保持不可变。

## 结果

| 门 | 结果 |
|---|---|
| schema registry | `context.state-event/v1alpha1` 已注册，strict artifact SHA-256 匹配 |
| deterministic replay | 1/1 versioned Event fixture 与期望 snapshot byte-equivalent |
| append-only history | reducer 不提供物理删除操作；对象通过 status 与 supersedes 演进 |
| sequence / revision | 非连续 sequence、revision gap 和非单步 revision transition 被拒绝 |
| event integrity | payload tamper、断裂 hash chain、重复 event ID 和 unknown wire version 被拒绝 |
| correction provenance | correction 必须引用更早的 Event；未知 supersedes reference 被拒绝 |
| stale authority | terminal Work 与 reverted/rejected/superseded Decision 无法使用同一 ID 复活 |
| input immutability | replay 不修改调用方提供的 snapshot 或 Event 列表 |

验证命令：

```text
python3 -m unittest tests.test_m2_02_state_events -v
python3 -m unittest tests.test_schema_governance tests.test_repository_verification -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

当前结果：M2-02 定向测试 14/14 通过；schema governance 7/7 通过；仓库全量测试 153/153 通过；Python compile 和 repository verifier 通过。

## PostgreSQL 边界

M2-03 PostgreSQL 承载关系型 typed snapshot、append-only Event、revision/CAS 和事务提交。两个 writer 使用相同 expected revision 时仅一个 commit 可以成功，stale writer 必须获得显式 conflict。向量索引不参与 active Work、Decision、Constraint、Claim 或 Effect 的权威裁决；可选 `pgvector` 属于 M6 candidate recall/retrieval 范围。

State MCP authorization、immutable checkpoint、runtime claim/effect enforcement 和 recall provider 分别由 M2-05、M2-06、M8 与 M6 交付。M2-02 的离线完成状态不授予 production state 写权限。
