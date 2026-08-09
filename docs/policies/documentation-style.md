# Documentation Style Policy

版本：1  
日期：2026-08-09

## 适用范围

本规范适用于 MASTER、STATUS、架构、策略、迁移、实验、验收和协作文件。原始研究摘录与外部引用保留来源原文，并明确标注引用边界。

文档分类、更新触发、容量边界、拆分、supersedes 和投影生成按 [`documentation-lifecycle.md`](documentation-lifecycle.md) 执行。

## 语言规范

- 使用稳定属性、权限、状态、指标和验收门描述系统。
- 使用声明式语句；结论附带来源、revision、hash 或验证方法。
- 架构归属通过“命名空间、仓库职责、依赖方向、提交权限”表达。
- 范围排除通过表格或 admission policy 表达。
- 时点状态集中记录于 `STATUS.md`、migration inventory 或 event log。
- MASTER 保存长期目标、边界、DAG、任务合同与验收标准。
- 对话式措辞、反问、主观修饰和修辞性对比不进入规范文档。
- 避免“不是 X 而是 Y”“第一刀”“我们在哪”“更聪明”“偷偷”等口语表达。
- 使用 `planned / implemented / verified / deprecated / superseded / rejected` 等明确状态。
- 禁止使用创建动作本身证明架构正确；完成状态必须引用完成门证据。

## 信息位置

| 信息 | 位置 |
|---|---|
| 长期目标与主线 DAG | `MASTER.md` |
| 当前 active work、blocker、next action | `STATUS.md`；Typed State 上线后自动生成 |
| 架构与协议 | `docs/architecture/` 或 versioned schema |
| 研究与组件评估 | `docs/research/` |
| 本机来源规模与迁移批次 | `docs/migrations/` |
| 访问、保留、脱敏和准入规则 | `docs/policies/` |
| 原始会话 | 受控 provider archive 或加密对象存储 |

## 审核门

规范文档提交前检查：

1. 是否包含会快速过期的会话事实。
2. 是否存在修辞性“不是 X 而是 Y”句式。
3. 权威来源、提交权限和数据位置是否明确。
4. 状态是否具备 revision 或验证证据。
5. 同一事实是否在 MASTER、STATUS 和投影视图中重复维护。
6. 术语、状态符号和指标是否与 schema 一致。
7. 更新触发、authority、revision、supersedes 和容量边界是否符合 lifecycle policy。
