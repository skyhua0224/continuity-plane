# M4-09 Skill Resolver Acceptance

版本：1  
日期：2026-08-14  
状态：verified local-embedded role/operation/provider resolver  

```yaml
document_id: context.m4-09-skill-resolver-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m4-09-skill-resolver-v1
affected_tasks: [M4-09, M5-01, M7-03, M8-06]
next_review: M5-01
```

## 范围

M4-09 消费已通过 M4-07 admission 的 catalog universe，产生有界、无副作用的 role/operation/provider Skill decision。request 绑定 catalog digest、project/repo/path/task/role/operation、provider contract、required schema 和显式 binding provenance。Skill manifest 不保存 active task、claim、revision、owner、checkpoint 或当前 evidence。

同一 applicability kind 使用 OR，不同 kind 使用 AND。path 使用 segment-prefix 匹配；`src/core` 匹配 `src/core/...`，不匹配 `src/core2/...`。provider contract 使用精确版本；provider alias 不隐式转换。required Skill 不能绕过 applicability、provider/schema compatibility、expiry、approval、provenance 或权限门。

resolver 按上下文 specificity 计算确定性 priority。低优先级 conflict 可审计抑制；同级 conflict、required conflict、dependency conflict、expired applicable Skill、dependency 不适用、provider/schema 不兼容和空匹配均 quarantine。依赖闭包通过后才进入 M4-02 compiled packet；decision 的 `state_write_authority` 固定为 `false`。

## 验证

| 门 | 结果 |
|---|---|
| strict contract | request、decision、benchmark 三份 schema 注册于 `schemas/registry.yaml`；runtime 与 Draft 2020-12 strict validation 通过 |
| focused behavior | `11/11` resolver tests 与 `5/5` benchmark tests 通过；M4-01 至 M4-10 回归 `225/225` 通过 |
| applicability | role/operation/provider/context isolation、same-kind OR、cross-kind AND、segment-prefix path `0` leakage |
| conflict and expiry | higher-priority suppression 可回放；equal-priority conflict、dependency conflict、expiry boundary `100%` quarantine |
| replay and mutation | `1000/1000` canonical decision replay；catalog/request input mutation `0`；replay mismatch `0` |
| negative fixtures | `125/125` provider contract drift fixtures quarantine；untyped selection、role leakage、provider leakage、authority true `0` |
| capacity and latency | approved/active catalog universe `6000`，selected `4000`，reduction `33.3333%`；p50 `1.085698 ms`，p95 `1.109641 ms`，max `2.143002 ms` |
| external services | `0` |
| replay fixture | [`m4-09-skill-resolution-replay-v1alpha1.json`](../../experiments/skills/m4-09-skill-resolution-replay-v1alpha1.json)，canonical fixture SHA-256 `cba7e1d7e9ffa3c3df1edf6acfa72e7537dc6b0fa37b85b119abf863372fb2df` |

## Boundaries

M4-09 不提交 Typed State、claim/lease、State revision、effect 或 provider runtime action。M5-01 将把该 decision、compiled Skill packet、当前 Work、evidence refs、Idea refs 和 continuation cursor 组装为 Execution Packet；M8 将绑定多协作者 lease、forge projection 和 durable execution。M4-06 compatibility lock 仍需在任务开始时结合 M4-09 request digest 和 provider contract 复核。
