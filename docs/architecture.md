# 架构

[English](architecture.en.md)

Continuity Plane 是集成库，不是项目运行时的构建依赖。Agent host、IDE、CI 或
人类控制台通过 adapter 调用服务，并提供可信 request context。

```text
Integration adapter
  -> authorization boundary
  -> State service
       -> typed snapshot
       -> append-only events
       -> claim / lease / effect records
  -> artifact store
  -> context composer
  -> checkpoint and replay canary
```

## 权威状态

Typed State 拥有当前任务、决定、约束、claim、revision 和 effect watermark。
检索、memory、代码图、模型输出和人类视图只能提供候选或投影，所有状态提交
都必须经过同一授权、revision 和 validator 边界。

## 本地优先存储

SQLite 是默认 State adapter，适合个人项目、本地 Agent Session 和离线恢复，
不需要服务进程。共享部署可以选择实现相同 capability/conformance 合同的后端。

部署层级相互独立：

- `local-embedded`：每个项目使用自己的 SQLite authority；
- `forge-coordinated`：增加 Git forge 可见性，协作者保留本地状态；
- 个人 PostgreSQL：用于 SQL 检查或本地 worker，但不是必需依赖；
- Docmost：人类投影和审批界面，不是 typed-state authority；
- `shared-strong`：通过显式共享 State MCP 服务提供 claim、lease 和 CAS authority。

alpha CLI 只直接配置 `local-embedded`，其他层级需要显式 adapter 或部署配置。

## 大型项目观察面

Project Graph、Decision/Evidence 和 Relationship/Impact projection 把同一 State
revision 转换为可由 CLI、Web、Docmost 或 Obsidian 消费的有界数据。确定性 DAG
用于主线与依赖，force-directed 关系图用于探索关系簇和影响范围；布局坐标没有
State 权限。

Docmost 是可选共享控制台。Obsidian 是生成的离线只读 vault。当前 alpha 提供部分
projection core 和 signed vault 生成器，完整页面、connector、Canvas/Bases 与
provider-neutral Context Health export 按
[`visual-products.md`](visual-products.md) 的阶段交付。

## 故障模型

每个外部可见操作都有稳定 identity。Checkpoint 记录 event head、state revision、
continuation cursor 和 effect watermark；恢复时先验证这些绑定，再允许新的
副作用执行。
