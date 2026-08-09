# M2-01 Typed State Acceptance

版本：1  
日期：2026-08-09  
状态：✅（versioned schema、semantic validator 与 canonical round-trip 已验证）

```yaml
document_id: context.m2-01-typed-state-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-schema-registry-2026-08-09
supersedes: null
affected_tasks: [M2-01, M2-02, M2-03, M2-05, M3-04, M4-01]
next_review: M2-02
```

## 范围

本验收覆盖 `Project`、`Work`、`Claim`、`Idea`、`Decision`、`Constraint`、`Evidence`、`Blocker` 和 `Effect` 九类核心对象。`context.typed-state/v1alpha1` 使用 strict-versioned registry 条目，wire schema 与 Python semantic validator 共同约束字段、引用、状态投影、scope ownership、dedupe、evidence gate 和 effect high watermark。

## 结果

| 门 | 结果 |
|---|---|
| schema registry | `context.typed-state/v1alpha1` 已注册，artifact SHA-256 匹配 |
| strict object fields | 根对象与 9/9 核心对象均拒绝未知字段 |
| canonical round-trip | 4/4 fixture byte-equivalent |
| solo / multi-worker | 单 worker 与两个不相交 scope 的并行 claim 通过 |
| duplicate-work gate | active/completed overlap 在协调前禁止激活 |
| claim/effect authorization | 第二个 scope owner、stale revision 和无 current claim 的 effect 被拒绝 |
| claim boundary | scope 必须属于 Work；active lease 必须覆盖 snapshot 时间 |
| current projections | active Work、accepted Decision、active Constraint 和 open Blocker 与 Project 投影一致 |
| completion evidence | completed Work 缺 verified Evidence 时被拒绝 |
| supersedes | Work、Decision、Constraint 与 Blocker 引用受完整性与无环检查 |

验证命令：

```text
python3 -m unittest tests.test_m2_01_typed_state -v
python3 -m unittest tests.test_schema_governance tests.test_repository_verification -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

当前结果：M2-01 定向测试 12/12 通过；schema governance 与 repository verifier 21/21 通过；仓库全量测试 138/138 通过；Python compile 通过。

## 后续边界

本叶提供离线 snapshot 合同和语义校验。Append-only Event/reducer、PostgreSQL revision/CAS、State MCP authorization、immutable checkpoint 和 live concurrent effect enforcement 分别由 M2-02、M2-03、M2-05、M2-06 与 M8 实现。M2-01 完成状态不授予 production state 写权限。
