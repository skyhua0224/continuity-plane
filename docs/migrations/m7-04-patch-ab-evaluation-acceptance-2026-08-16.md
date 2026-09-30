# M7-04 Patch A/B Evaluation Acceptance

版本：2  
日期：2026-08-16  
状态：verified local deterministic contract

```yaml
document_id: context.m7-04-patch-ab-evaluation-acceptance
document_revision: 2
change_type: evidence
authority_ref: repository-tests-and-deterministic-replay-2026-08-16
supersedes: context.m7-04-patch-ab-evaluation-acceptance@1
affected_tasks: [M7-04, M8-07, M10-00]
next_review: M8-07
```

## 范围

M7-04 提供 provider-neutral patch A/B experiment、blind set、opaque verifier packet、independent verifier score、result 和 benchmark strict wire。两个 candidate 绑定相同 provider、model、fixture、input/output token、tool call 和 wall-time budget。

Experiment 在候选 patch、run context、quality observation 与 verifier policy 冻结后计算 `frozen_experiment_sha256`。随机顺序由可解析 entropy artifact、randomizer provenance artifact 与 frozen digest 派生。Verifier packet 仅包含 fixture、rubric、opaque assignment token 和 patch ref；packet 不包含 candidate ID、producer、variant、quality、run context 或 blind ID。

## 信任门

| 门 | 裁决 |
|---|---|
| randomizer | entropy 与 provenance artifact 必须可解析；seed 必须绑定 frozen experiment |
| principal | producer、randomizer 和 verifier 必须解析到 canonical principal；alias self-review 拒绝 |
| artifact | patch、fixture、candidate evidence、score evidence 与 randomizer artifact 必须按 content digest 解析 |
| quality | quality 必须绑定 M7-03 verification decision/run refs 与 M7-02 claim admission |
| verifier | 至少 2 个且最多 16 个 canonical independent verifier；每个完整评分两个 assignment |
| score | rubric score 范围为 0..10,000 basis points；result 保存精确 total、observation count、delta numerator 和 denominator |
| build/test | control 与 treatment 均通过 |
| mutation | treatment basis points 大于或等于 control |
| verifier result | treatment score total 大于或等于 control |
| rework | treatment count 严格小于 control |
| authority | 所有 wire 的 State/completion authority 均为 `false` |

缺少 principal、artifact 或 quality resolver 时，满足质量门的结果为 `provisional`，不能产生 `admit`。Resolver 返回 digest 漂移、错误 quality binding 或 alias self-review 时 fail closed。

## 固定 Fixture

[`m7-04-patch-ab-fixture.json`](../../experiments/evidence/m7-04-patch-ab-fixture.json) 保存 synthetic experiment、blind set、verifier packet、4 份 score、result，以及 fixture-only artifact payload、principal binding 和 quality resolution。全部 artifact ref 可按 payload 重算。

| 指标 | control | treatment | delta |
|---|---:|---:|---:|
| build passed | true | true | maintained |
| test passed | true | true | maintained |
| mutation score | 8,200 bp | 8,400 bp | +200 bp |
| verifier score total / observations | 48,600 / 6 | 51,000 / 6 | +2,400 / 6 bp |
| rework count | 4 | 2 | -2 |

Fixture result 为 `admit`，`trust_status=trusted`，`quality_not_degraded=true`，`rework_reduced=true`。Fixture 只验证合同和 resolver binding，不构成真实 provider 质量增益证据。

## Decision-Branch Replay

[`m7-04-patch-ab-benchmark-results.json`](../../experiments/evidence/m7-04-patch-ab-benchmark-results.json) 汇总 1,000 个 synthetic decision-branch sample。场景循环覆盖 valid、build failure、test failure、mutation regression、rework non-reduction 和 verifier score regression。

[`m7-04-patch-ab-benchmark-outcomes.json`](../../experiments/evidence/m7-04-patch-ab-benchmark-outcomes.json) 保存全部 sample outcome。Benchmark 绑定 outcomes content digest、fixture digest、implementation digest 和自身 digest；`validate_patch_ab_benchmark` 从 outcomes 重算全部 aggregate。

| 指标 | 结果 |
|---|---:|
| successful replay | 1,000 / 1,000 |
| expected / actual admit | 167 / 167 |
| expected / actual reject | 833 / 833 |
| false admit / false reject | 0 / 0 |
| admitted quality non-degradation | 167 / 167 |
| admitted rework reduction | 167 / 167 |
| control-first / treatment-first | 486 / 514 |
| external calls | 0 |

该 replay 验证六条裁决分支、聚合重算和 tamper detection。真实 blind review 需要独立 producer/verifier 运行、current repository evidence 和 M10 pilot sample。

## 验证

| 验证 | 结果 |
|---|---|
| adversarial focused tests | 25 / 25 passed |
| alias isolation | canonical principal alias self-review fail closed |
| unresolved trust | 无 resolver 的正向质量结果为 provisional |
| runtime bounds | 17 verifier 与 129 artifact refs fail closed |
| strict schema | 9 份 Draft 2020-12 schema check 与实例校验通过；unknown field 禁止 |
| replay | committed fixture、1,000-sample benchmark 与 outcomes 精确重建 |
| authority | external calls 为 0；State/completion authority 为 false |

## 当前边界

固定 fixture 和 replay 使用 synthetic provider、model、artifact payload、principal mapping、quality observation 和 verifier score。真实质量增益结论需要 M10 pilot 在相同 provider/model/budget、current repository revision、M7-03 Verification Profile 和可解析 build/test/mutation evidence 下取得独立 A/B sample。

M7-04 不拥有 artifact store、State 或 completion 权限。State 变更继续经过 M7-02 claim-evidence gate、M7-03 Verification Profile 和 State MCP revision/CAS/authorization/validator。
