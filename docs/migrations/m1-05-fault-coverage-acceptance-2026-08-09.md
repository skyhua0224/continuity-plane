# M1-05 Fault Coverage Acceptance

版本：1  
日期：2026-08-09  
状态：✅（contract completion gate passed；runtime evidence deferred）

```yaml
document_id: context.m1-05-fault-coverage-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-fixture-matrix-2026-08-09
supersedes: null
affected_tasks: [M1-05, M2-01, M5-03, M8-01, M8-02]
next_review: null
```

## 范围

本验收覆盖 E0-E9 的最小故障合同、确定性 validator、versioned schema 和 coverage summary。Fixture 只定义 initial state、injection boundary、expected gate/state/effect、runtime dependency 和 evidence ref；未声明尚未执行的 runtime fault injection。

## 结果

| 门 | 结果 |
|---|---|
| E0-E9 experiment coverage | 10/10，100% |
| fixture 数 | 16 |
| contract fixture coverage | 16/16，100% |
| runtime verified fixture coverage | 0/16，0% |
| required scenario coverage | 10/10 |
| 安全期望 | duplicate effect、unauthorized effect、stale decision revival 均为 0 |
| schema registry | `context.fault-coverage/v1alpha1`，artifact hash 已绑定 |

Required scenarios 包括 task switch without checkpoint、parallel ready work、path-owner conflict、issue-backed ready work、duplicate-work detection、stale Skill digest、SIGKILL commit boundary、provider 503、checkpoint corruption 和 concurrent CAS。重复工作 fixture 要求在第二个未协调 claim 前阻断，并保持 effect watermark 不变。

## 验证

```text
python3 -m unittest tests.test_m1_05_fault_coverage -v
python3 -m unittest tests.test_project_work_governance -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

M1-05 定向测试 11/11 通过；项目工作治理测试 7/7 通过；仓库全量测试 125/125 通过。Python compile、统一 repository verifier、结构化数据、schema/projection、fixture privacy、文档 link/style 和 Gitleaks Git scan 均通过。

## 边界

本验收证明故障合同完整、可解析且不会冒充 runtime evidence。PostgreSQL reducer/CAS、State MCP、PreCompact/PostCompact、DBOS/Temporal、真实 SIGKILL/503、损坏恢复和并发写仍由 M2、M5 与 M8 实现和复验。Runtime coverage 0% 不影响 M1-05 的 contract completion，但禁止任何 production recovery 完成声明。
