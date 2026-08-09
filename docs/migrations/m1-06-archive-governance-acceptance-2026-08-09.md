# M1-06 Archive Governance Acceptance

版本：1  
日期：2026-08-09  
状态：🧑‍💻（offline contract verified / production adapter planned）

## 范围

- `context_control_plane/archive_governance.py`：retention validation、canonical export/import、tombstone 和 deletion-proof reference adapter；
- `schemas/m1-06/archive-governance.schema.json`：archive record、export bundle 和 tombstone wire contract；
- `docs/policies/archive-lifecycle.md`：存储、权限、保留、导出、删除与证明边界；
- `tests/test_m1_06_archive_governance.py`：9 个正反 contract tests；
- `schemas/registry.yaml`：`context.archive-governance@m1-06.v1` artifact hash 与 compatibility metadata。

所有 payload 均为合成 sealed bytes。未读取、复制、导出或删除真实 provider archive、原始 transcript、密钥或用户数据。

## 验收结果

| 门 | 结果 |
|---|---|
| retention class | `ephemeral/project/audit` 可验证；ephemeral 缺失或无效 finite expiry 被拒绝 |
| deterministic export | record/object/tombstone 稳定排序；相同输入和 `exported_at` byte-equivalent |
| import integrity | bundle/manifest/payload hash、size、count、project、orphan 与 active/deleted conflict 受校验 |
| private identity | manifest/record/tombstone 不包含 provider 原 ID 或 archive/local path |
| delete authorization | 无效 authorization ref 被拒绝；legal hold 阻止删除 |
| deletion proof | record/object absence、最小 tombstone 字段、proof digest 和 tamper detection 通过 |
| schema governance | `m1-06.v1` 已进入 registry；artifact hash 验证通过 |

验证命令：

```text
python3 -m unittest tests.test_m1_06_archive_governance -v
python3 -m unittest tests.test_schema_governance -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

结果：M1-06 定向测试 9/9 通过；schema governance 7/7 通过；仓库全量测试 55/55 通过；Python 编译、JSON/YAML 解析、文档语言和 raw transcript admission 检查通过。

## 权限与剩余边界

Reference adapter 只证明 canonical bundle round-trip、合同校验以及受测 in-memory inventory 的 record/object absence。它不提供 production encryption、KMS、tenant isolation、authorization service、legal hold registry、后端物理擦除、backup expiry、provider deletion API 或独立审计。

Production archive adapter 必须在 M2/M8/M10 绑定 State MCP revision/CAS、authorization、Project Profile、envelope-encryption key revision、backend deletion receipt、independent absence verification、export/import disaster recovery 和 audit event。上述 live evidence 完成前，状态保持 `🧑‍💻`。
