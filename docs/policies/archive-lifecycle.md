# Archive Lifecycle Policy

版本：1  
日期：2026-08-09  
状态：offline contract verified / production adapter planned

## 范围

本策略管理受控 provider archive 或加密对象存储中的原始会话及派生内容。Git 只保存协议、schema、测试和脱敏验收证据。Archive export 不构成 Git admission，导出包不得进入 Git、公共 CI 日志、issue 或 PR。

`context_control_plane/archive_governance.py` 是离线 reference adapter。Production archive service、密钥管理、存储后端擦除 receipt、authorization、legal hold registry、State MCP event commit 和审计集成在 M2、M8 与 M10 实施。

## Retention Classes

| Class | 生命周期 | 到期合同 | 删除门 |
|---|---|---|---|
| `ephemeral` | 临时提取、短期迁移或验证窗口 | 每个 record 必须具有晚于 `created_at` 的有限 `expires_at` | 到期 sweep 或已授权用户请求 |
| `project` | 项目持续使用的原始或派生档案 | `expires_at` 可由 Project Profile 指定；未指定时由项目关闭或治理动作触发 | 已授权用户请求、项目关闭或显式治理动作 |
| `audit` | 合规、争议或变更审计所需的受控档案 | `expires_at` 由适用 policy 指定；未指定不代表永久保存授权 | 已授权删除且无有效 legal hold |

Retention class 不替代 authorization、数据主体权利、legal hold 和 jurisdiction policy。未定义期限的 `project` 或 `audit` record 必须由 production policy resolver 提供删除触发条件；reference adapter 只验证类型，不推导法律期限。

## Archive Record

Archive record 只保留 project ID、opaque source/object refs、sealed payload hash/size、retention class、时间和 legal hold 标记。Provider 原始 thread ID、本机路径、archive path、密钥、未封装正文和 provider 隐藏字段禁止进入公开 record、manifest 或 tombstone。

Payload 在进入 export boundary 前必须由 archive adapter 使用项目批准的 envelope encryption 封装。`sealed-by-archive-adapter` 表示调用边界，不声明 reference adapter 自身执行了加密。Production import 必须对照 Project Profile 验证算法、key revision、tenant/project ownership 和 storage receipt。

## Export And Import

Export bundle 使用 canonical UTF-8 JSON，包含 manifest、按 opaque object ref 索引的 sealed payload 和 bundle SHA-256。Manifest 记录 wire version、project、export time、record/tombstone 计数、稳定排序的 metadata、content protection 和 manifest SHA-256。

Import 必须在 admission 前完成以下验证：

1. wire version 位于 schema registry 支持窗口；
2. bundle 与 manifest digest 匹配；
3. record、tombstone、object ref 唯一且 project 一致；
4. record count、tombstone count、payload hash 和 size 匹配；
5. 不存在 orphan object、缺失 object、active/deleted 冲突或不稳定排序；
6. payload protection、authorization 和目标 Project Profile 有效。

相同 record、tombstone、sealed payload 和显式 `exported_at` 的 export/import/export 结果必须 byte-equivalent。密钥 rotation 或 schema migration 会产生新的 bundle revision，并通过 migration/replay/rollback 门验证。

## Delete And Tombstone

删除操作需要 opaque deletion request ref、authorization ref、目标 source ref 和时间。有效 legal hold 阻止删除并产生拒绝事件；reference adapter 只返回错误，State MCP 上线后由 append-only event 保存拒绝事实。

已授权删除按以下顺序执行：

1. 解析 source record 与 object ref；
2. 删除 active record 与 sealed payload；
3. 在同一 adapter inventory 验证 source/object ref 不存在；
4. 生成 digest-bound tombstone；
5. production adapter 保存 backend deletion receipt，并由独立 verifier 复核当前不存在。

Tombstone 只保存 opaque refs、retention class、已删除 sealed content 的 hash/size、请求与授权 refs、删除时间、范围、absence result 和 proof digest。它不保存 payload、provider identity、路径、密钥或正文。

Deletion proof 证明指定 record/object 在受测 adapter inventory 中已移除，且 tombstone 未被篡改。底层介质物理擦除、备份过期和供应商档案删除必须由 backend receipt、retention sweep evidence 和独立验证共同证明；tombstone digest 不能单独证明物理擦除。

## 验收门

- 三类 retention 及 ephemeral finite expiry 具有正反测试；
- export manifest 稳定排序且不暴露 private identity/path；
- export/import/export byte-equivalent；
- manifest、bundle、payload hash/size 篡改被拒绝；
- 未授权删除和 legal hold 删除被拒绝；
- 删除后 record/object 不存在，tombstone digest 可验证；
- schema registry hash、wire version 和 migration policy 一致；
- raw transcript 与真实 archive payload 的 Git admission 保持 0。
