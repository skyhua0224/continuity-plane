# M4-02 Compiled Skill Packet Acceptance

版本：1  
日期：2026-08-11  
状态：accepted provider-neutral static Skill packet contract

```yaml
document_id: context.m4-02-compiled-skill-packet-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-independent-review-2026-08-11
supersedes: null
affected_tasks: [M4-02, M4-03, M4-04, M4-05, M4-06, M5-01, M5-05]
next_review: M4-03
```

## 范围

M4-02 注册 `context.compiled-skill-packet/v1alpha1`。编译器接收已经通过 M4-01 validator 的、显式批准的 Skill ID 集合，闭包化其依赖，并输出稳定的 Skill、版本、内容 SHA-256 和 rule ID bindings。输出绑定依赖完整覆盖、全局唯一 rule ID、source manifest-set SHA-256 和 canonical JSON bytes。

Packet 只保存稳定规则资产的选择与身份。`active_task_id`、`idea_id`、`owner`、`claim_id`、`revision`、`blocker`、`checkpoint_id`、当前 evidence、effect 和其他动态状态字段在 root、selection、binding 三层均被拒绝。Skill 正文、S0-S3 装载、provider adapter、Execution Packet 的当前状态注入和 PostCompact canary 属于后续任务。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | `context.compiled-skill-packet/v1alpha1` 使用 Draft 2020-12 strict schema；未知字段和动态状态 fail-closed |
| runtime validator | dependency closure、approved status、rule ID 全局唯一、selection/binding 完整覆盖、版本与内容 digest 一致、source manifest-set digest 对账和 canonical ordering 通过 |
| replay fixture | `experiments/skills/m4-02-compiled-skill-packet-v1alpha1.json` 通过独立 source verification 与 canonical byte round-trip |
| registry | schema artifact hash `bf279ff6df7a351a468f7b64d85991525b97cfe2a40ecbc2c071de4a7f850048` 与 `schemas/registry.yaml` 一致 |
| 定向测试 | 17/17 通过 |
| repository gate | 448 tests 通过；28 个无 DSN PostgreSQL live tests skipped；repository verifier、Python compile 和 diff check 通过 |
| static input proxy | M4-01 canonical manifest set `2,967` bytes；选择 `core.recovery` 的 compiled packet `811` bytes；下降 `72.666%`，高于 `60%` contract proxy 门 |
| independent review | High 0；Medium 0 |

## 权限与后续边界

编译器不决定 Task、owner、claim、path ownership、evidence、effect、promotion 或 provider applicability；这些信号由 M3 resolver、State MCP、Execution Packet 和 provider adapter 按其权限合同提供。Manifest-set source digest 变化会使旧 packet verification 失败，不能静默复用。

静态 input proxy 只说明 manifest metadata 选择范围变小，不代表 provider tokenizer、真实上下文压缩、Skill 正文遵循、恢复质量或 live token/cost 改善。M4-03 负责 drift/quarantine，M4-04 负责 S0-S3 正文分层与装载指标，M5 负责动态 Execution Packet 和压缩恢复实测。
