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

## Continuity 策略与轻量探针

旧项目无需修改配置，缺少 `continuity_policy` 时使用 `balanced`。需要覆盖默认策略时，
在 `.continuity/project.yaml` 增加：

```yaml
continuity_policy:
  preset: balanced
  resume:
    explicit_policy: once_per_connection
  checkpoint:
    on_pre_compact: true
    on_work_complete: true
    after_state_writes: false
    min_interval_seconds: 30
  verification:
    startup_scope: recent
    deep_verify: manual
  observability:
    mode: minimal
    probes_enabled: true
    slow_call_threshold_ms: 1000
    resource_sampling: boundaries_failures_and_slow
    retention_max_bytes: 67108864
```

可用 preset 为 `balanced`、临时排障用的 `diagnostic`，以及异常恢复期使用的
`reliability-first`。显式子项覆盖 preset。未知字段、错误类型和越界数值会使配置验证
失败；PreCompact checkpoint 和 Work completion checkpoint 是安全边界，不能关闭。
当前 alpha 中，checkpoint 间隔和 verification 字段约束自动化策略合同；尚无额外自动
checkpoint 或 deep verifier 调度时，它们不会增加后台工作。

`CONTINUITY_OBSERVABILITY_MODE=diagnostic` 可临时提升观测级别，但长期策略应写入项目
配置。minimal 模式下普通成功读取只在 MCP 进程内累计，只有边界、State 写、失败、
慢调用和 Session 汇总落盘。观测失败不会回滚或阻塞已经完成的 State 操作。

离线查看探针结果：

```bash
continuity observe report --root .
```

报告只读取隐私化的本地 observation，不读取 transcript、源码或工具响应正文，也不会
自动修改配置。provider/host 没有提供 token usage 时，报告明确标记不可用。
