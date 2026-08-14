# M5-01 Execution Packet Acceptance

版本：1  
日期：2026-08-14  
状态：verified local-embedded bounded composition

```yaml
document_id: context.m5-01-execution-packet-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-01-execution-packet-v1
affected_tasks: [M5-01, M5-02, M5-03, M5-05, M5-06, M5-08]
next_review: M5-02
```

## 范围

M5-01 将当前 Typed State、M4-09 resolved Skill decision、compiled Skill packet、当前 active Work、直接决定/约束/阻塞、已验证 evidence、相关 Idea refs、唯一 next action 和 continuation cursor 组装为 `context.execution-packet/v1alpha1`。Packet 保存 project revision、governance ref、canonical plan digest、state digest、Skill lock 和 packet digest。

Composer 只产生内存中的有界投影，不提交 Typed State、Event、Claim、Effect、Skill activation 或 provider action。候选 Idea 在 packet 中标记为 `candidate-only`；历史 transcript、memory、完整 manifest set、Skill 正文和无关 Work 不进入 packet。Skill request、decision、compiled packet 的 request/catalog/manifest/packet digest 必须一致，项目和 active Work ref 必须匹配。

## 验证

| 门 | 结果 |
|---|---|
| strict contract | `context.execution-packet/v1alpha1` 与 `context.execution-packet-benchmark/v1alpha1` 已注册；Draft 2020-12、runtime exact-field validation 通过 |
| focused behavior | M5-01 composer 与 benchmark 定向 `8/8` 通过；stale Skill、未验证 evidence、active leaf 缺失、换行/重复 next action、超界 packet 均 fail closed |
| replay | 相同 state/Skill/cursor/plan 输入 `1000/1000` 成功；canonical packet replay mismatch `0` |
| canary | packet validation failure `0/1000`；`state_write_authority=true` `0/1000` |
| capacity | min/p50/p95/max `4436/4436/4436/4436 B`；目标 `4 KiB <= packet <= 12 KiB`，capacity gate `1` |
| latency | p50 `0.509149 ms`；p95 `0.517655 ms`；max `0.651848 ms` |
| external services | `0` |
| replay fixture | [`m5-01-execution-packet-replay-v1alpha1.json`](../../experiments/skills/m5-01-execution-packet-replay-v1alpha1.json)；canonical fixture SHA-256 `29164261d9e822d55b2813c73df024e21d334aaf5d655654219dedf13690fb11` |
| implementation provenance | `execution_packet.py` SHA-256 `1a5a6c9c137730e1af28bc16be01b59ef11de1437bc003d17255a41d82a772fc` |

Receipt [`m5-01-execution-packet-results.json`](../../experiments/routing/m5-01-execution-packet-results.json) 通过 strict benchmark schema；validator 会重新计算实现和 fixture hash。运行时 token、provider tokenizer/cache、PreCompact/PostCompact hook 和真实压缩行为属于 M5-02 至 M5-05，未在本任务声明。

## 边界与后续

M5-01 不保存完整 Work DAG，不展开 artifact 内容，不授予 candidate Idea、Skill resolver、模型或 provider adapter 状态写权限。M5-02 将把 material event 滚动 checkpoint 与 provider compaction hook 接入同一 packet 输入；M5-03 增加 PostCompact canary 和 host 对照；M5-04 增加 artifact bounded expansion；M5-05 记录 provider/token/cache/retrieval accounting。

## 复现

```text
.venv/bin/python tools/run_execution_packet_benchmark.py --samples 1000 --generated-at 2026-08-14T18:00:00Z
.venv/bin/python -m unittest tests.test_m5_01_execution_packet tests.test_m5_01_execution_packet_benchmark -q
.venv/bin/python tools/verify_repository.py --root .
```
