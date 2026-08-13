# M4-05 Provider Skill Adapter Acceptance

版本：1  
日期：2026-08-11  
状态：accepted local provider delivery-plan replay

```yaml
document_id: context.m4-05-provider-skill-adapter-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-probe-official-references-and-independent-review-2026-08-11
supersedes: null
affected_tasks: [M4-05, M4-06, M4-09, M4-10, M5-01, M5-03, M5-05, M8-06]
next_review: M4-06
```

## 范围

M4-05 注册 `context.provider-skill-adapter-effect/v1alpha1` 与 `context.provider-skill-effect-probe/v1alpha1`。neutral effect 保留 packet、layer plan、load receipt、Skill、rule、composition 和 provider payload identity；Codex 与 Claude 通过独立 materializer 生成本地 delivery plan，并可严格恢复相同 neutral sections。

Codex materializer 生成 Agent Skills filesystem assets 和 `$alias`/Skill input；Claude materializer 生成 filesystem Skills、`skills`、`setting_sources` 和 Skill tool 初始化参数。两条路径均生成合法 `SKILL.md` frontmatter，provider alias 与原始 `skill_id` 保持可验证映射。输入 Skill bytes 的合同是 provider-neutral body；已包含 YAML frontmatter 的 source asset 被拒绝，避免 materialization 产生双 frontmatter。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | 两个 Draft 2020-12 strict schema 已注册；effect schema hash `7687a0b85a63d7e128918d18fdf12cf10b4b83b649b510efce8471fd856d53d1`；probe schema hash `fbf86d482cff35c505ab62cd45f2adef530c0c6f93b11583d3e07d2c9fd79107` |
| identity chain | packet、M4-03 drift assessment、M4-04 plan/authorization/load receipt、requested layers、Skill/rule IDs、content bytes 和 provider payload 全链校验；伪造或越界输入 fail-closed |
| provider surface | 2026-08-11 Codex Manual、Claude documentation index 与 Claude Agent SDK Skills 官方快照 hash 分别为 `633d406edbe14526cb7d1e113db188a7ec3bee188e2b28e4290912d15d214989`、`d8174cb72c55130d0200c4448c96d8dcbcd008b93e63ca9655de9206a3471d29`、`81dd0e86f05cca1d91e758ccf0d5b26b1a72efd5eb6646c9a9f163e60723dcb7` |
| bounded payload | composition 上限 8 MiB、provider payload 上限 16 MiB、Skill 上限 1,024、aggregate rule ID 上限 65,536；unknown provider fields、非法 frontmatter/name/path 和非 canonical payload 被拒绝 |
| deterministic replay | 40 组 byte-distinct synthetic compositions、Codex/Claude 各 40 个 effect；neutral mismatch `0/40`，provider replay mismatch `0/80` |
| authority boundary | 80/80 effect 通过 strict validation；runtime-state write declaration `0/80`；`provider_process_invoked: false`、`state_revision_authorization_measured: false` |
| measured calls | 80 warmup、80 compose-and-validate、80 replay，共 240 次 compose；80 个 measured effect |
| local latency | Linux x86_64、CPython 3.14.6；Codex p50 `0.7601 ms`、p95 `0.7937 ms`；Claude p50 `0.7443 ms`、p95 `0.7750 ms`；两者 p95 `<10 ms` |
| 定向测试 | adapter 16/16、probe 8/8，共 24/24 通过 |
| repository gate | 全库 535 tests 通过；28 个无 DSN PostgreSQL live tests skipped；Python compile、repository verifier 和 `git diff --check` 通过 |
| independent review | 三路复核 High 0、Medium 0；权限合同、计量语义、schema/runtime parity、registry provenance 与官方 surface 已复验 |

版本化原始结果位于 [`m4-05-provider-skill-effect-results.yaml`](../../experiments/state/m4-05-provider-skill-effect-results.yaml)。validator 会重新构造全部 80 个 effect，重算 provider/neutral digest、bytes、percentile、schema/registry/source provenance 与 acceptance gate；stale 或越界 receipt 不通过验收。

## 权限与后续边界

本次验收只证明本地 delivery-plan 构造、严格 effect validation 和 deterministic replay。40 个 workload label 只标识 byte-distinct synthetic compositions，不表示对应 checkpoint、CAS、lease 或 workflow 行为已在该 probe 中执行。synthetic plan authorizer 和 frozen clock 不证明 actor、State revision 或 project policy authorization。

真实 Codex/Claude 进程调用、provider input/billable token、prompt cache、规则遵循、context-window 使用和压缩恢复均未测量。M4-06 负责进行中任务的 rule-set 兼容锁与变更 replay；M5/M8 负责 Execution Packet、真实压缩、token/cache accounting、provider Harness Run 和 State revision authorization。
