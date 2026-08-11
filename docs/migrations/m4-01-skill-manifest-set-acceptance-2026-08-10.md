# M4-01 Skill Manifest Set Acceptance

版本：1  
日期：2026-08-10  
状态：accepted provider-neutral Skill manifest contract

```yaml
document_id: context.m4-01-skill-manifest-set-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-independent-review-2026-08-10
supersedes: null
affected_tasks: [M4-01, M4-02, M4-03, M4-04, M4-07, M4-09, M4-10]
next_review: M4-02
```

## 范围

M4-01 注册 `context.skill-manifest-set/v1alpha1`，为 Built-in、External、Project、User 和 Workflow Skill 提供统一的 strict manifest set。Manifest 固定 `skill_id`、SemVer、内容 SHA-256、来源类型、SPDX 或 content-addressed license ref、typed applicability、稳定 rule IDs、依赖、冲突、expiry、provider/schema compatibility、content-addressed provenance refs 和生命周期状态。

动态任务字段不进入 Skill manifest。`active_task_id`、`idea_id`、`owner`、`claim_id`、`revision`、`blocker`、`checkpoint_id`、当前 evidence 和 effect 由后续 State MCP 组装 Execution Packet 时注入。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | `context.skill-manifest-set/v1alpha1` 注册；未知字段、未类型化 ref 和动态状态字段 fail-closed |
| runtime validator | 依赖闭包、循环、重复 ID、选择状态、冲突范围、expiry、SemVer、静态 typed ref、provider contract identity 和 canonical ordering 通过 |
| SPDX provenance | SPDX license ID 固定到 SPDX 3.28.0 官方 snapshot；snapshot revision 和 SHA-256 保存在测试合同 |
| 定向测试 | 33/33 通过；其中 26/26 为原始 M4-01 合同，7 项覆盖边界修正 |
| fixture | 1/1 versioned fixture 通过 JSON Schema、runtime validator 和 canonical byte round-trip；provider applicability 与 provider contract identity 一致 |
| registry | schema artifact hash `929bdd6eee0ec4eadd0449c8926517819d4e8c60317235739f2424e6e53acf85` 与 `schemas/registry.yaml` 一致 |
| independent review | High 0；Medium 0 |

## 权限与后续边界

Manifest 只描述规则资产及其准入条件，不授予状态写入、任务切换、claim、effect、promotion 或 evidence gate 权限。外部来源 URL、实时 evidence 和动态任务状态由 catalog 或 State MCP 管理；缺失、漂移、过期、冲突或 provenance 不完整的条目进入 quarantine。

M4-02 负责稳定 rule IDs 和 compiled packet；M4-03 负责漂移 quarantine；M4-04 负责 S0-S3 分层加载。当前验收不宣称压缩恢复、provider token 或 Skill A/B 的 live 改善。
