# M4-06 Skill Compatibility Acceptance

版本：1  
日期：2026-08-11  
状态：accepted active-task rule-set compatibility lock

```yaml
document_id: context.m4-06-skill-compatibility-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-compatibility-probe-2026-08-11
supersedes: null
affected_tasks: [M4-06, M4-07, M4-09, M5-01, M5-03, M8-06]
next_review: M4-07
```

## 范围

M4-06 注册 `context.skill-compatibility-lock/v1alpha1`、`context.skill-compatibility-decision/v1alpha1`、`context.skill-compatibility-migration/v1alpha1` 与 probe receipt schema。lock 绑定 task、operation、manifest-set digest、compiled packet digest、packet schema、provider contract、Skill version/content digest、selected manifest canonical digest 和 selected rule IDs。每个声明的 provider contract 必须同时满足全部 selected Skill 的 exact compatibility 与 provider applicability。

candidate manifest 的未选中元数据变化保持 `compatible`。选中 Skill 的 version、content digest、rule IDs、governance manifest、compiled packet 或 provider contract 变化进入 `migration_required`；selected Skill 缺失、不可选择或与请求 provider 不兼容时进入 `rejected`。受测 active-task gated composition entrypoint 根据 live lock、manifest、packet 和 provider contract 重算 decision，并校验 exact provider contract 到 adapter surface 的映射。未通过 decision 或 migration evidence verifier 时，该 entrypoint 不调用 M4-05 materializer。

M4-05 `ProviderSkillAdapter.compose` 是确定性本地 effect/payload materializer，provider process、network 和 state side effect 均为 0；它不是已门控的 provider dispatcher。M5/M8 的真实 dispatcher/effect emitter 必须携带 M4-06 lock、decision、provider contract digest 以及 authorization/State receipt，才能声明结构性强制门控。

migration 保存 old/new lock digest、task/operation binding、migration ID、reason、approval、replay proof、rollback proof 与 canonical old/new locks。migration replay 具有确定性，rollback 返回 canonical old lock。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | 四个 Draft 2020-12 strict schema 已注册；lock hash `31f27020405cf88956c84e6ea3927057b4040db516b6858cbcfdcab36153d841`；decision hash `1286bd3353558cce63f1bfcac4ff1929f91cf233259e60746ad54455edc9a94d`；migration hash `889ff8ce064e36d15538d6f072c263681d74b9f3be218600803cfad930f66b39`；probe hash `4c5088c90d1520aa8553f126c21e602d0e528479ccb2e65742cd9a43970fadb3` |
| immutable identity | lock canonical round-trip、selected rule coverage、packet/manifest binding、selected manifest digest、Skill version/content digest 和 exact provider applicability/contract 均通过 runtime validation |
| compatibility classification | 40 个变更样本：unselected metadata `8/8 compatible`；selected version/content/rules 与 provider contract `32/32 migration_required` |
| selected governance gate | selected provenance/schema compatibility 变化进入 migration；quarantined status 与 unsupported provider contract 拒绝；伪造 classification/reason 组合不授权 |
| pre-provider gate | breaking candidate 的 unauthorized gated composition `0/32`；blocked materializer calls `0`；compatible candidate materializer calls `8/8`；stale packet/contract 与不匹配 adapter 在 materializer 前拒绝 |
| explicit migration | 4 类 breaking change 经 synthetic evidence verifier `4/4` allowed；missing verifier/replay/rollback/approval evidence allowed `0/4`；wrong task binding allowed `0/4` |
| replay and rollback | migration replay mismatch `0/4`；rollback mismatch `0/4` |
| local latency | Linux x86_64、CPython 3.14.6；40 次 assessment p50 `0.6850 ms`、p95 `0.8320 ms`、max `0.8409 ms`；p95 `<10 ms` |
| authority boundary | provider process/network `0`；State commit `0`；state-write authority `0`；probe 使用 synthetic Skill source、synthetic evidence verifier 与 provider adapter test double |
| tests | M4-06 定向 `23/23`；repository gate `558 passed`、`28 skipped`；Python compile、repository verifier、`git diff --check` 与 M4-06 changed Python files Ruff 通过 |

版本化 receipt 位于 [`m4-06-skill-compatibility-results.yaml`](../../experiments/state/m4-06-skill-compatibility-results.yaml)。validator 重建固定 40 条 scenario 与 4 条 migration，重算 decision/migration digest、classification、provider compose calls、migration/rollback results、percentile 和 acceptance gate。provenance 绑定 fixture、compiler、manifest validator、M4-06 实现、probe、runner、测试、四个 schema 与四个 registry entry；字段篡改或 stale provenance 不通过验收。

## 权限与后续边界

本次验收建立本地 active-task Skill contract 与受测 gated composition entrypoint，未调用真实 Codex、Claude 或其他 provider 进程，未测量 provider token、prompt cache、规则遵循、context-window、压缩恢复、State MCP revision/CAS 或多人 claim/lease。M5 负责将 lock 与 Execution Packet、checkpoint 和 PostCompact canary 绑定；M8 负责使真实 provider dispatcher/effect emitter 强制消费 lock/decision/contract digest、authorization receipt、authoritative claim、effect 和 handoff。
