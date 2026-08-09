# M0-07 Schema Governance Acceptance

版本：1  
日期：2026-08-09  
状态：offline verified / runtime migration planned

## 范围

- `context_control_plane/schema_governance.py`：registry、digest、wire version 和 release transition validator；
- `schemas/registry.yaml`：7 个当前 schema/profile/experiment artifact 及内容 hash；
- `docs/policies/schema-version-release-governance.md`：compatibility、migration、replay、release 和 rollback 权限；
- `tests/test_schema_governance.py`：7 个正反 contract tests。

## 验收结果

| 门 | 结果 |
|---|---|
| artifact path 与 SHA-256 | 7/7 匹配 |
| schema ID 唯一 | 7/7 |
| registry digest 确定性 | 条目顺序翻转后相同 |
| current wire version | 7/7 可解析 |
| unknown wire version | 100% quarantine |
| backward-compatible version gate | patch bump 被拒；minor bump通过 |
| breaking version gate | major + migration + rollback + replay 才能通过 |
| migration evidence | 缺 replay/idempotency/rollback 的条目被拒 |

Registry digest：`c4aea9851746ce236f3662deb8fea4ad6193a70744ea73b80ddbfe00cc29a1be`。

验证命令：

```text
python3 -m unittest tests.test_schema_governance -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

结果：M0-07 定向测试 7/7 通过；仓库全量测试 55/55 通过；Python 编译、JSON/YAML 解析、文档语言和 raw transcript admission 检查通过。

## 剩余边界

当前 registry 和 validator 为离线实现。PostgreSQL migration、State MCP revision/CAS、checkpoint registry digest、N-1 live reader、shadow canary 和 production rollback 在 M2、M5、M8 与 M10 实施。离线完成状态不授予 production state 写权限。
