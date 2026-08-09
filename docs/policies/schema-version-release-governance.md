# Schema, Version, and Release Governance Policy

版本：1  
日期：2026-08-09  
状态：implemented offline gate / runtime migration planned

## 目标

所有 Context Control Plane schema、profile、event、checkpoint、Skill manifest、reference catalog 和 Harness Run 合同都具有稳定 ID、可比较版本、兼容窗口、migration、replay 和 rollback 证据。协议变更不得依赖模型记忆或自然语言说明。

## Registry

机器可读 registry 位于 [`schemas/registry.yaml`](../../schemas/registry.yaml)。每个条目至少记录：

```yaml
schema_id: stable namespace
current_semver: semver
current_wire_version: immutable wire id
supported_wire_versions: [current, verified legacy]
artifact_path: repository-relative path
content_sha256: sha256
status: current | deprecated
compatibility_mode: strict-versioned | profile-contract
migrations: [{from, to, replay_passed, idempotent, rollback_ref}]
```

`schema_id` 不随版本变化；`wire_version` 一旦发布不可复用。`artifact_path` 必须位于仓库内，hash 与实际文件一致。Registry digest 由 canonical JSON 计算，条目顺序不影响 digest；运行时 checkpoint 绑定 registry digest。

## Versioning

- `metadata` 变更使用 patch release，并保持 major/minor 不变。
- `backward-compatible` 变更至少使用 minor release；新增 wire 字段必须通过兼容 reader 或新 wire version。现有 strict `additionalProperties: false` 合同不能静默接收新字段。
- `breaking` 变更使用 major release，并要求 migration、rollback 和完整 replay 证据。
- 所有 release transition 都要求 deterministic replay；失败时状态为 quarantined，禁止 active。
- alpha/beta 版本仍遵守同一规则；发布通道不会降低权限、证据或安全门。

## Migration 与 rollback

Migration 必须确定性、幂等、可审计且不调用网络或模型。它保留 provenance、unknown field findings 和 source hash；无法无损转换的输入进入 quarantine。每条 migration 都有目标 wire version、replay 结果、idempotency 结果和 rollback ref。

读取旧版本时只接受 registry 中声明的 `supported_wire_versions`。写入使用当前版本；读取旧版本必须先完成已验证 migration。rollback 回到上一 release 时使用显式 migration 或 immutable artifact，不直接覆盖 event log、checkpoint 或 effect。

Schema、reducer、Skill provider contract 或 Verification Profile 变更前，先创建 checkpoint 并记录 registry digest。恢复时若 digest 不一致，validator 只允许 migration/replay，禁止代码写入、提交、部署和新副作用。

## Release 生命周期

```text
proposal -> compatibility review -> migration/replay -> rollback drill
-> frozen registry digest -> shadow canary -> current
-> deprecated -> superseded
```

人类治理 owner 批准 release 与 deprecation。自动扫描只能提出 change、stale 或 quarantine 事件，不能自行发布、回滚或修改 MASTER。

## 完成门

- registry 所有 artifact path、hash、schema ID 和 current wire version 校验通过；
- registry digest 在字段顺序变化后保持稳定；
- unknown schema/wire version 100% quarantine；
- backward-compatible 与 breaking transition 的版本门和 evidence gate 有正反测试；
- breaking migration 缺失 replay、idempotency 或 rollback 时激活率为 0；
- 当前 schema、profile 和 catalog 可被同一 registry 识别；
- 后续 State MCP、checkpoint reducer 和 provider adapter 必须引用 registry digest，不能私自解释版本。
