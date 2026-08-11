# M4-03 Skill Drift Quarantine Acceptance

版本：1  
日期：2026-08-11  
状态：accepted provider-neutral Skill drift admission contract

```yaml
document_id: context.m4-03-skill-drift-quarantine-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-independent-review-2026-08-11
supersedes: null
affected_tasks: [M4-03, M4-04, M4-05, M4-06, M4-09, M5-01, M5-02]
next_review: M4-04
```

## 范围

M4-03 注册 `context.skill-drift-assessment/v1alpha1`。评估器接收已通过 M4-02 严格验证的 compiled Skill packet、当前 manifest set、provider-neutral `asset_resolver(skill_id) -> bytes | None` 和受信任 `observed_at`。核心合同拒绝直接文件路径；resolver 只提供内容字节，内容 SHA-256 由核心计算。

评估结果是可重放的严格 receipt，包含 packet、manifest set 和每个已验证 Skill 的 observed content digest。`allow` 要求 manifest digest、所有 verified Skill 的 observed content digest、空 findings 和全部 verified 状态；任一漂移、缺失、过期、不可读、非法输入或来源不可选均产生 `quarantine`。resolver 不得改变已锁定 packet 的身份。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | Draft 2020-12 strict schema；未知字段、动态 task state、控制字符、非闰年 2 月 29 日、年份 0000、非法月份/日、非法时间和非法 offset fail-closed；schema hash `8ba0eddc0cef565fecc419af82414491f23cf197d44b174d2388c661017f7f43` |
| runtime validator | receipt 字段、gate/findings/Skill 状态关系、digest、RFC3339、reason code、runtime `skill_id` 唯一性和 allow evidence 完整性通过；结构校验与 expiry liveness 分层，无底层 KeyError/OSError/RuntimeError 泄漏 |
| drift and fault matrix | 33 个显式负变体全部被对应 schema/runtime/admission 门拒绝或 quarantine，拦截率 `33/33 = 100%`；包括 missing asset、content/manifest/version digest、expiry、source status、resolver failure、packet mutation、direct path、malformed manifest、非法时间、控制字符和 duplicate Skill ID |
| replay fixture | `experiments/skills/m4-03-skill-drift-assessment-v1alpha1.json` canonical round-trip；fixture SHA-256 `48bf6db2f531df3dff52d2e31805da2dfb41e6f8303ea67ac63a739d6c5c2750` |
| registry | `schemas/registry.yaml` 与 schema artifact hash 一致 |
| 定向测试 | 26/26 通过 |
| M4 组合回归 | M4-01/M4-02/M4-03：76/76 通过 |
| repository gate | 全库 474 passed、28 个无 DSN PostgreSQL live tests skipped；compile、repository verifier 和 `git diff --check` 通过 |
| independent review | High 0；Medium 0；resolver 重入、allow evidence、malformed manifest、direct-path boundary、控制字符、Gregorian date 和 runtime semantic uniqueness 复验通过 |
| provider/live boundary | provider tokenizer、真实压缩、Skill 正文遵循、恢复质量、token/cost 和跨 provider replay 未在 M4-03 宣称；由 M4-04/M4-05/M5 验收 |

## 权限与后续边界

M4-03 只判断版本化 Skill 资产能否进入下一阶段装载，不决定 active task、owner、claim、path ownership、revision、checkpoint、effect、promotion 或 provider 路由。`asset_resolver` 是受控 adapter 边界；M4-04 必须消费已经观测并校验的 bytes 或内容寻址 artifact，禁止重新从路径读取。

Draft 2020-12 `uniqueItems` 只能比较完整数组元素，不能表达 `skills[*].skill_id` 的键唯一性；该约束已作为 runtime semantic admission gate 固定，并在 schema `$comment` 与回归测试中声明。任何 schema consumer 仍必须执行 runtime validator。

M4-04 的下一执行叶为 S0-S3 分层 Skill 加载和有界 packet 指标，继续保留 M4-03 receipt 作为漂移准入证据。
