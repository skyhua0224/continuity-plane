# M4-07 Skill Catalog Acceptance

版本：1  
日期：2026-08-11  
状态：accepted offline catalog contract

```yaml
document_id: context.m4-07-skill-catalog-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-canonical-fixture-2026-08-11
supersedes: null
affected_tasks: [M4-07, M4-08, M4-09, M4-10, M5-01]
next_review: M4-08
```

## 范围

M4-07 注册 `context.skill-catalog/v1alpha1` 与 strict runtime validator。Catalog entry 将一个 M4-01 manifest 与 source kind、固定 URL/revision/path、publisher、license、provenance、capability、approval/verification evidence、状态和权限声明绑定。Built-in、External、Project、User、Workflow 五类来源均使用同一合同。

Catalog 权限字段只允许明确的 `false` 值：entry 不能写 Typed State、切换任务、claim、产生 effect、promotion 或 evidence gate。`candidate` 记录只用于发现，`approved`/`active` 需要 approval 与 verification artifact refs；外部、项目和用户来源禁止使用 `dynamic-index` 进入 approved/active。`active_skill_manifest_set` 只投影 approved/active manifest，未批准候选不会进入 compiled packet。

M4-06 lock binding 在 delivery 前复核 catalog manifest 的版本、content digest 和 rule IDs。Catalog source 或 manifest identity 变化必须先重新编译并走 compatibility migration；旧 lock 不会被 catalog metadata 静默替换。

## 验证

| 门 | 结果 |
|---|---|
| strict schema/runtime | `schemas/m4-07/skill-catalog.schema.json` 已注册，registry hash `9dca5455197236d90b4a1240a7fdc55ebe184a1b3ee497e60c690e5e9ed407ca`；13/13 定向测试通过 |
| source coverage | Built-in、External、Project、User、Workflow `5/5` 合法 entry 通过同一 validator |
| admission/quarantine | 未批准 active、dynamic revision、权限提升、source-kind mismatch、license/provenance mismatch 与重复 ID `8/8` 被拒绝；候选不进入 active manifest projection |
| canonical replay | `m4-07-skill-catalog-v1alpha1.json` 独立读取、runtime validate 和 canonical round-trip 通过 `1/1` |
| compatibility lock | exact version/content/rule identity binding `1/1` 通过；内容 digest 篡改 `1/1` 拒绝 |
| local validator latency | 固定 fixture、Linux x86_64、CPython 3.14；200 次 validate，p50 `0.0106 ms`、p95 `0.0135 ms`、max `0.1317 ms`；仅测本地 catalog 校验 |
| authority boundary | catalog、validator、fixture 均为本地无副作用；provider process/network、State commit、token/cache 和真实压缩未测 |

## 后续边界

M4-08 负责从项目结构、Verification Profile 和用户偏好生成可审批 proposal；proposal 不得自动激活。M4-09 负责 role/operation-aware resolver，M4-10 负责官方、标准、GitHub 与 marketplace snapshot adapter。真实 provider dispatcher、Execution Packet、State authorization、context-window 和压缩恢复仍由 M5/M8 验收。
