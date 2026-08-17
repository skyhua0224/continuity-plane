# 配置

[English](configuration.en.md)

生成的 `.continuity/project.yaml` 是项目拥有的配置入口：

```yaml
schema_version: context.project/v1alpha1
project_id: my-project
display_name: My Project
runtime_profile: local-embedded
state_store:
  adapter: sqlite
  path: .continuity/state.sqlite3
collaboration:
  mode: solo
  shared_state: false
```

`project_id` 和 `display_name` 由项目自行决定。包名和仓库名是发行标识，不是
项目必须采用的名称。

凭据应放在环境配置或 secret store 中，不要写入 project profile。禁止写入
provider token、数据库密码、原始 transcript 或机器路径。

## Profile

| Profile | 默认状态 | 额外基础设施 | 状态 |
|---|---|---|---|
| `local-embedded` | 本地 SQLite | 无 | alpha 默认支持 |
| `forge-coordinated` | 本地 SQLite + forge 记录 | 已有 Git forge | 集成合同 |
| 个人 PostgreSQL | 显式 PostgreSQL adapter | 本地/私有 PostgreSQL | 可选库 adapter |
| 个人 Docmost | State MCP 仍是权威 | Docmost + connector | 可选投影 preview |
| `shared-strong` | 共享 State MCP | conformed service，常见为 PostgreSQL | 显式部署 preview |

PostgreSQL 和 Docmost 都是选择，不是依赖。项目可以完整生命周期只使用本地
模式。

共享 adapter 只有在声明 CAS、claim、lease fencing、durable receipt 和
project isolation 支持后，才能承担权威操作。
