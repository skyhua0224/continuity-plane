# M4-08 Skill Proposal Acceptance

版本：4  
日期：2026-08-11  
状态：accepted offline proposal contract / production input adapter pending M7-03

```yaml
document_id: context.m4-08-skill-proposal-acceptance
document_revision: 4
change_type: evidence
authority_ref: repository-tests-canonical-fixture-and-local-probe-2026-08-11
supersedes: context.m4-08-skill-proposal-acceptance@3
affected_tasks: [M4-08, M4-09, M4-10, M5-01, M7-03]
next_review: M7-03
```

## 范围

M4-08 注册 `context.skill-proposal/v1alpha1` 与 strict runtime validator。Proposal schema 通过固定 `$id` 复用 M4-01 canonical manifest schema；runtime 复用同一 manifest validator 与 M0-07 SemVer 2.0.0 parser。生成器只接收字段集固定的 project facts、Verification Profile 和用户显式偏好；复制、去重与排序前执行 64 KiB UTF-8 与 4096 list-item preflight，canonical input 上限为 64 KiB，单个 candidate content 同时限制为 16,384 Unicode scalar 与 64 KiB UTF-8，输出 proposal 上限为 128 KiB，派生 ID 上限为 256 字符。原始 transcript、未知字段、缺失 provenance 和超限输入在生成前被拒绝。Languages、required gates、source refs、preference IDs、tool preferences 和 style preferences 在 fingerprint 与正文生成前排序；build/test 命令保留输入顺序，反引号在 Markdown body 中确定性编码。Candidate license ref 必须由 Verification Profile 显式提供，并继承该 Profile 的 provenance。

每个 proposal 必定包含一个 Project candidate；只在存在显式用户偏好时生成 User candidate。Project candidate 只绑定 project 与 verification provenance，用户偏好变更不改变项目候选 digest；User candidate 额外绑定显式偏好 provenance。每个 candidate 内嵌确定性的 body-only UTF-8 Markdown asset，LF 结尾且无 BOM、frontmatter 或 CRLF；`manifest.content_sha256` 只标识该正文的实际字节。`proposal_skill_assets()` 输出 `skill_id -> bytes`，可直接交给 M4-03 bytes-only resolver。

Proposal 状态固定为 `proposed`，activation 固定为 `not_active`，candidate 与 manifest 分别固定为 `candidate` 和 `proposed`，六个权限字段只允许 `false`。Candidate ID、manifest Skill ID、source kind 和 provenance 必须一致；同一 source kind 不得重复。`verify_skill_proposal_inputs()` 使用原始有界输入、预期 generator version 和外部期望生成时间重生成并比较 canonical bytes；proposal 时间与外部期望不一致时拒绝 replay。Proposal 不提交 Typed State，不进入 active catalog，不产生副作用。

## 验证

| 门 | 结果 |
|---|---|
| strict schema/runtime | `schemas/m4-08/skill-proposal.schema.json` 已注册，schema hash `01ccc353087518aee38980df7c0059429c7579d00006b27a74d52cb6b58aac9e`；复用 M4-01 manifest schema；34/34 定向测试通过 |
| bounded input | unknown field、缺失 project provenance、4097-item list、超过 64 KiB preflight/canonical input 被拒绝；超限列表在 copy/sort 前拒绝；空用户偏好不生成 User candidate；set-like 字段重排结果相同，build/test 命令重排结果不同 |
| schema/runtime boundary | 234 字符 repo ID 的所有派生 ID 通过 runtime/schema，235 字符输入 fail-closed；超过 16,384 Unicode scalar 或 64 KiB UTF-8 的正文、BOM、frontmatter、CRLF、无 LF 结尾和孤立 surrogate 被两层拒绝；非法 calendar date/offset、`1.0.0-01` 与 `1.0.0-alpha.01` 被两层拒绝；applicability、dependency 和 license 负变体均被两层拒绝 |
| canonical replay | `m4-08-skill-proposal-v1alpha1.json` 独立读取、实时重生成与 canonical round-trip `1/1` 通过；fixture digest `37899b8d2f28035c7aa25c02b9836ae867df2235b049ea0468d5cb3204c9a9d0`；canonical proposal 4,239 bytes，candidate body 合计 1,098 bytes |
| deterministic change | 同输入 200 次 replay mismatch `0/200`；40 个 project-fact 变体的 fingerprint/content-digest 组合唯一率 `40/40` |
| input-bound verification | fingerprint、content digest、generator version、generated timestamp、input refs、proposal ID、repo applicability 和协调式 content/digest 篡改均被原始有界输入与外部期望时间 replay 拒绝 |
| asset compatibility | candidate content digest `2/2` 匹配；BOM、frontmatter、CRLF 和无 LF 结尾变体 `4/4` 拒绝；synthetic approved projection 经 M4-02 compile 与 M4-03 resolver 得到 `allow` `1/1` |
| license provenance | Project/User candidate 的 license ref `2/2` 来自 Verification Profile；生成器内硬编码 license 为 0 |
| authority boundary | 固定 fixture 的 2 个 candidate 共 12 个权限字段，`true=0`；proposal/candidate status 篡改、identity/provenance 漂移和重复 source kind 均被拒绝 |
| local generation latency | 200 次 generate+validate+canonicalize 的 p95 `<10 ms` 作为持续测试门；不保存依赖单次调度的精确 latency 小数 |
| repository gate | 全库 610 tests 通过、28 个无 DSN PostgreSQL live tests skipped；repository verifier、Python compile、changed Python files Ruff 和 `git diff --check` 通过 |

## 后续边界

M7-03 负责生产 Project Verification Profile 与实仓 input adapter；本验收只覆盖有界 typed input 到 candidate-only proposal 的离线合同。M4-03 `allow` 使用将 proposed manifest 复制为 approved 的 synthetic test projection，只证明正文 bytes/digest 接口兼容，不构成 promotion 或 catalog admission 证据。M4-09 在 M3-04 完成后提供 role/operation-aware resolver；M4-10 提供官方、标准、GitHub 与 marketplace catalog adapter。Proposal 的审批、catalog admission、State revision/CAS 和真实 provider 激活仍由 M4-07/M7/M8 对应门禁验收。
