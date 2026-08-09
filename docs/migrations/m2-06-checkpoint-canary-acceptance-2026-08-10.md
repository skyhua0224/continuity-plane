# M2-06 Checkpoint Canary Acceptance

版本：1  
日期：2026-08-10  
状态：accepted immutable local checkpoint

```yaml
document_id: context.m2-06-checkpoint-canary-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-local-checkpoint-benchmark-2026-08-10
supersedes: null
affected_tasks: [M2-06, M5-02, M5-03, M5-08, M8-01]
next_review: M2-07
```

## 范围

M2-06 提供 `context.checkpoint-manifest/v1alpha1`、`publish_checkpoint` 和 `restore_checkpoint`。Publisher 只接受一次 State MCP read result；typed snapshot 和 strict manifest 分别发布到 M2-04 content-addressed artifact store，manifest ArtifactRef 是 checkpoint identity。相同输入产生相同 identity，不存在可覆盖 alias 或随机 checkpoint ID。

Manifest 绑定 project/revision、governance ref、canonical plan digest、schema registry digest、Event head、active/primary/terminal Work、accepted/terminal Decision、active Constraint、open Blocker、active Claim、in-flight/terminal Effect、effect watermark 和 snapshot timestamp。Restore 在读取前执行 manifest/snapshot size gate，验证两个 ArtifactRef、strict JSON、typed state、critical projection digest 和可信调用方提供的 current authority expectation。

## 验证

| 门 | 结果 |
|---|---|
| strict schema / registry | checkpoint manifest 已注册；root、Event head 和 ArtifactRef 禁止未知字段；schema hash 与 registry 一致 |
| 定向测试 | checkpoint contract 9/9；benchmark/receipt 4/4 |
| deterministic identity | 同一 State MCP read result 重复发布得到相同 manifest ArtifactRef |
| critical recovery | 18/18 字段恢复；recovery 100% |
| integrity faults | missing、bit flip、truncate、malformed JSON、unknown version/field、manifest/snapshot drift 全部拒绝 |
| stale authority | project、revision、governance ref、Event head、plan digest 和 registry digest 任一变化均拒绝 |
| bounded restore | manifest/snapshot size 在 artifact read 前检查 |
| default runtime | SQLite + local artifact store；external services 0 |
| full repository | 345 tests；0 failed；28 个 PostgreSQL live tests 在无 DSN 环境跳过 |

## 量化结果

[`m2-06-checkpoint-canary-results.yaml`](../../experiments/state/m2-06-checkpoint-canary-results.yaml) 使用 40 个 `restore_checkpoint` 样本。计时边界不包含 StateStore read 和 checkpoint publication。Manifest 为 1,137 bytes，snapshot 为 2,583 bytes；restore p50 为 `0.2040 ms`，p95 为 `0.2378 ms`，max 为 `0.3102 ms`。p95 低于 `2,000 ms` 完成门，18 个承重字段恢复率为 100%。该结果来自当前 Linux x86_64 主机。

复验命令：

```text
.venv/bin/python tools/run_checkpoint_benchmark.py \
  --samples 40 \
  --observed-at 2026-08-10T08:00:00+08:00
```

## 权限与后续

ArtifactRef 证明内容完整性；checkpoint ref 必须来自可信状态或已授权调用方。任意调用者自行生成的自洽 manifest/snapshot 不获得 active state 或恢复权限。Restore 同时要求受信调用方提供匹配的 project、revision、governance ref、Event head、plan digest 和 registry digest。M2-06 未实现 PreCompact 增量提交、Execution Packet、PostCompact 写权限门、跨进程 durable request dedupe、lease 或副作用 retry；这些职责保留在 M5 与 M8。

M2-06 保存 verified Event head，供后续增量恢复路径使用。SQLite Event commit 的完整历史重验证成本未在本叶优化。完成门要求的关键字段 100% 恢复、损坏检测、stale authority veto、bounded restore 和 p95 `<2s` 已满足，active queue 推进至 M2-07。
