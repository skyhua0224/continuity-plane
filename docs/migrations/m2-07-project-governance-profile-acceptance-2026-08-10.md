# M2-07 Project Governance Profile Acceptance

版本：1  
日期：2026-08-10  
状态：accepted provider-neutral governance profile

```yaml
document_id: context.m2-07-project-governance-profile-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-independent-review-2026-08-10
supersedes: null
affected_tasks: [M2-07, M3-01, M4-01, M8-07, M8-08, M10-07, M10-09]
next_review: M4-01
```

## 范围

M2-07 提供 `context.project-governance-profile/v1alpha1` strict wire、semantic validator、deterministic canonical form 和 provider-neutral fixture。现有 `context.project/v1alpha1` integration profile 保持原 wire version 与 registry identity。

Project Profile 声明 direction、governance owner、execution worker、repository topology、requested runtime profile 和 task source。Project Charter 保存 discovery 范围、约束、证据、未知项、预算、expiry、return point 和 promotion authority。WorkSource 将 MASTER、Issue、State 或外部 PM 任务映射到 opaque source revision。Work obligation 绑定 `required/conditional/optional`、authority、automation class、verification profile、evidence 和 expiry。

ProjectAdaptation 只保存有界检索、typed ref、Skill applicability 和展示偏好候选。内容 hash 包含 `proposal_revision`；version 符合 SemVer 2.0.0，path/repo/operation/user/provider applicability 使用对应 typed ref。activation provenance 满足 `proposal_revision < activation_revision <= profile.revision`，并可在激活后的 profile revision 中确定性 replay。active 或 superseded rollback target 必须来自同一 normalized lane 的更早 SemVer，并保存完整 approval、activation 和 replay provenance。`rollback_to` 形成新的 candidate；current replay、safety veto、approval、expiry 和 activation gate 仍须重新通过。

Project Profile、Project Charter 和 Work obligation 的 revision 是独立对象 revision domain。跨对象提交一致性由 State MCP expected revision/CAS、Event 和 validator 建立。Work obligation 的 satisfied、waived 和 expired 状态分别要求 evidence、approval 或 elapsed expiry 依据。

## 权限

| 对象 | 权限 |
|---|---|
| Project Profile / Charter / WorkSource / obligation | typed candidate；权威提交仍需 State MCP authorization、expected revision/CAS 和 validator |
| ProjectAdaptation | observe/propose/shadow/approve/rollback 数据合同；active task、claim、authority、gate、retention 和 effect 写权限为 0 |
| `observed_at` | 由 State MCP 或授权 host clock 注入时可用于 liveness gate；provider、Skill 或聊天输入自报时间不构成 current evidence |
| `requested_runtime_profile` | 只声明部署意图；实际保证仅来自已验证 StateStore capability manifest 和 State MCP receipt |
| Repository topology | 选择 scope resolver；modular、monolith 和 mixed 共享同一治理合同 |

## 验证

| 门 | 结果 |
|---|---|
| strict schema / registry | 新 wire 独立注册；schema hash 与 registry 一致；所有对象拒绝未知字段 |
| M2-07 定向测试 | 35/35 通过 |
| 配置组合 | direction、owner、worker 与 topology 的 36 个组合 canonical round-trip 通过 |
| authority / obligation | governance 与 task-source authority、condition、automation class 和完整 Work coverage 通过 |
| obligation lifecycle | satisfied evidence、waived approval 和 elapsed expiry 依据门通过 |
| adaptation safety | typed applicability、SemVer 2.0.0、allowlist、deterministic sets、content hash、trusted-time boundary、expiry、proposal/activation revision、approval 和 veto 通过 |
| rollback | missing/self/forward/cycle/cross-lane/unactivated target 全部拒绝；active snapshot 在 activation revision 和后续 revision replay 通过 |
| fixture | 1/1 versioned provider-neutral fixture canonical round-trip 通过 |
| M2-06 evidence correction | 原 receipt SHA-256 `0a793a1088bd38d77c083b745a6ec3444bf4354a9030144d3d1d705d2ec97224` 字节不变；历史验证绑定 pinned digest；current provenance 使用独立 40-sample revalidation receipt |
| schema governance | 9/9；非法 prerelease 拒绝，alpha/beta/release precedence transition 通过 |
| repository | 394 tests；0 failed；28 个 PostgreSQL live tests 在无 DSN 环境跳过；compile、repository verifier 和 `git diff --check` 通过 |
| independent review | 两名独立 Verifier；High 0；Medium 0 |

## 当前边界

M2-07 不执行 task routing、claim/lease、promotion workflow、forge synchronization、Skill resolution 或 ProjectAdaptation A/B。`requested_runtime_profile` 只保存部署意图；枚举值不能授予共享 CAS、unique claim、multi-writer 或 lease 保证。实际保证必须来自 M2-08 StateStore capability manifest 和 State MCP receipt；M10-09 负责激活时的 conformance gate。M3 提供任务图与路由；M4 提供 Skill manifest 和 resolver；M8 提供 adaptation lifecycle、forge adapter 与并发协调。默认 runtime 继续使用 `local-embedded`，外部数据库、容器和网络服务数量为 0。

M3-01 的 Windows/macOS native fixture 依赖仍由 M2-09 阻塞。M4-01 只依赖已完成的 M2-01，是当前 next-ready required leaf。
