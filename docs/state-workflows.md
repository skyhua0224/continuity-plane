# State 与已有治理文档接入

[English](state-workflows.en.md) · [文档索引](README.md)

这是进阶路径。日常任务卡、检索、规则、消息与报告无需先建立 State claim。

## 接入已有 MASTER／STATUS

已有计划不能用 init 模板覆盖。项目已经初始化后，先只读生成 proposal，再明确批准：

```bash
continuity attach plan --root . --master /path/to/MASTER.md --status /path/to/STATUS.md --work-id current --work-title 'Current work' --owner-ref controller --scope testing
continuity attach approve --root . --actor-ref controller --claim-id claim-current
```

第二步是权威写入。检查 proposal 的路径／hash、操作者与 scope 后再运行；源文档变化会使旧 proposal 失效。重复批准不会创建重复事件。精确参数与错误后的 next_action 以当前 help 和 receipt 为准。

## 类型化 Work、决定与约束

```bash
continuity work add --root . --work-id next --title 'Next step' --source-file .continuity/MASTER.md --scope testing
continuity decision add --root . --decision-id approach --work-id next --statement 'Use the tested path' --source-file .continuity/MASTER.md
continuity constraint add --root . --constraint-id safety --work-id next --statement 'Do not change production configuration' --source-file .continuity/MASTER.md
```

这些命令默认预览。确认后用 receipt 的 expected revision 明确 apply，经过授权、CAS 与 validator。普通 Markdown 完成标记不是 State 验收证据。checkpoint、claim、source freshness 的门不能用“元数据仅建议”绕过。

## 部署模式

| 方式 | 当前含义 |
|---|---|
| local-embedded | 默认本地 SQLite，支持本地生命周期 |
| forge-coordinated | 本地 State 与 forge 集成合同，不自动部署共享服务 |
| PostgreSQL adapter | 可选库与数据库依赖；需自行部署及验证 |
| shared-strong | 显式服务部署与一致性要求，不是开箱跨机 server |
| Docmost／Obsidian | 部分投影及计划能力，不能替代 State 权威；见各自覆盖说明 |

不要把 profile 名称或示意图当成已经运行的集群。更改 project.yaml 也不代表数据已迁移。需要跨 adapter 迁移时，先验证目标的 CAS、lease fencing、隔离、durable receipt 与回放门，再安排维护窗口。

## 备份和回滚

`export`、`import`、`rollback` 是 State bundle 操作。先读各自 `--help`，保存来源 revision/hash 与完成门证据，不覆盖未知数据。协作注册表、worker 产物、规则附件、索引和原生账号分别管理；不能把 State bundle 当成所有数据的备份。

任何 compaction、交接、权威任务切换都应有可验证的 checkpoint。metadata 健康度不能自行授权额外项目操作；State 写入失败也不自动禁止普通源码开发。

更多：[架构](architecture.md) · [配置](configuration.md) · [Python API](api.md) · [投影视图](project-views.md) · [图形产品范围](visual-products.md)
