# 第三方声明

[English](THIRD_PARTY_NOTICES.en.md)

Continuity Plane 使用或与以下第三方组件互操作。各组件仍遵循自己的许可证。

## 运行时依赖

| 组件 | 用途 | 许可证 | 来源 |
|---|---|---|---|
| PyYAML | YAML 解析 | MIT | <https://github.com/yaml/pyyaml> |
| jsonschema | JSON Schema 验证 | MIT | <https://github.com/python-jsonschema/jsonschema> |
| psycopg | 可选 PostgreSQL adapter | LGPL-3.0-only | <https://github.com/psycopg/psycopg> |

项目不把这些 Python 包源码 vendor 到仓库。安装器根据声明的依赖解析它们，
PostgreSQL 依赖保持可选。

## SPDX License Identifier Snapshot

Skill validator 包含一份 SPDX license identifier 生成快照，来源为
`spdx/license-list-data` revision
`c4a7237ec8f4654e867546f9f409749300f1bf4c`。快照只保存 identifier 和
provenance metadata，不包含完整许可证正文。

来源：<https://github.com/spdx/license-list-data>

## 开发工具

构建、测试、lint、审计和发布工具属于开发依赖，不进入 runtime wheel。它们的
许可证由各自发行包提供。
