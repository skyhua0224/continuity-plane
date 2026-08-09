# PostgreSQL State Store Assessment

版本：1  
日期：2026-08-10  
状态：implemented shadow evidence

```yaml
document_id: context.postgresql-state-store-assessment
document_revision: 1
change_type: evidence
authority_ref: postgresql-18-psycopg-3-official-docs-and-local-fault-injection
supersedes: null
affected_tasks: [M2-03, M2-05, M2-08, M2-09, M6-03, M8-02]
next_review: M2-08
```

## 采用范围

M2-03 使用 PostgreSQL 保存 `shared-strong` profile 的关系型权威状态：`context_control.projects` 保存当前 typed snapshot、revision 和 Event head，`context_control.state_events` 保存完整 canonical Event envelope。Event 表的 project/sequence、project/event ID、project/revision 和 project/event hash 具有唯一约束；trigger 拒绝 UPDATE 与 DELETE。默认 `local-embedded` profile 由 M2-08/M2-09 的 StateStore SPI 与 SQLite backend 交付，不依赖 PostgreSQL。

每次提交在一个事务中执行以下顺序：

1. `SELECT ... FOR UPDATE` 锁定 project 行；
2. 对比 expected revision、sequence 和 previous event hash；
3. 使用 M2-02 reducer 从当前 snapshot 验证候选 Event；
4. 插入 append-only Event；
5. 使用 `WHERE revision = expected_revision` 更新 snapshot 与 Event head；
6. commit 后向调用方返回成功，任一异常回滚 Event 与 snapshot。

该路径使用 PostgreSQL 默认 `READ COMMITTED` 加 project row lock。不同 project 可以独立推进；同一 project 的并发 writer 在 row lock 后读取 current revision，stale writer 获得 `PostgresStateConflict`。State MCP authorization、claim/effect permission 和 tenant isolation 继续由 M2-05/M8 交付。

## 实测

结构化结果位于 [`m2-03-postgres-cas-results.yaml`](../../experiments/state/m2-03-postgres-cas-results.yaml)。PostgreSQL 18.4 容器上的两 writer fault injection 为 8/8 显式 conflict，silent overwrite 为 0。连续两次 Event 提交保持 revision、sequence 和 hash chain；重复 Event identity 回滚；Event UPDATE/DELETE、snapshot row revision 漂移和 replay mismatch 均被检测。

40 个 localhost 样本中，commit p50/p95 为 19.5525/22.0199 ms，read p50/p95 为 9.8665/12.7360 ms。每个样本包含新建数据库连接、Python validator、Event replay、row lock、JSONB 写入和 transaction commit。该结果用于 shadow baseline，不代表远程、复制、故障切换或 production durability 性能。

## 向量边界

M2-03 未安装 `pgvector`。active Work、Decision、Constraint、Claim、Effect、revision 和 Event head 只由关系型约束、transaction、CAS 和 validator 裁决。M6 可以评估 `pgvector` 作为 candidate recall/retrieval index；其结果需要 provenance 与 current-evidence gate，权威状态提交权限保持 0。

## 当前证据

| 来源 | Snapshot SHA-256 | 采用结论 |
|---|---|---|
| PostgreSQL 18 Explicit Locking | `75f5cf9dc6a8e2da05a5df1f29d8808427e5018ff6dc1bca97f04a3436f05d48` | project row lock |
| PostgreSQL 18 Transaction Isolation | `9368fe2c416b7994b6a05beb3ba6b20abc988f9106a94132b11af14f44a7534d` | default isolation 与显式 CAS |
| PostgreSQL 18 JSON Types | `159c813c024813dd77d04189b620a80264e492d64e3c657dcc1c7771f76b79fb` | canonical envelope/snapshot JSONB storage |
| Psycopg 3 Transaction Management | `4a74b74057a58b964d6e0c396a5ad3689a1785d932ebd435e34b74b8e89be772` | connection context commit/rollback |
| PostgreSQL 18.4 Alpine OCI image | `sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15` | local 与 CI integration fixture |
