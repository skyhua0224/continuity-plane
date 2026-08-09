# M2-05 State MCP Acceptance

版本：1  
日期：2026-08-10  
状态：accepted provider-neutral state boundary

```yaml
document_id: context.m2-05-state-mcp-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-live-dual-adapter-parity-2026-08-10
supersedes: null
affected_tasks: [M2-05, M2-06, M3-04, M6-03, M8-05, M9-01]
next_review: M2-06
```

## 范围

M2-05 提供 transport-neutral `context.state.read`、`context.state.commit`、`context.state.claim` 和 `context.state.effect` 合同。请求使用 strict versioned envelope；transport 注入 trusted `RequestContext`，authorizer 在任何 StateStore 访问前执行并默认拒绝。写入由服务端生成 actor、Event identity、时间、sequence、hash 和 project projection，客户端只能提交受支持的 intent。

通用 commit 不能修改 Claim 或 Effect。Claim 只允许 Work owner 原子认领 ready Work；Effect 必须绑定同一 actor 的 active Claim、Work 和 exact scope。mutation 使用 expected revision/CAS；相同 request ID 与相同 payload 返回原 receipt，不同 payload 返回 conflict。错误被归一为 permission、not-found、conflict、integrity、capability、busy 和 unsupported 分类。

## 验证

| 门 | 结果 |
|---|---|
| strict schema / registry | `context.state-mcp/v1alpha1` 已注册；schema hash 与 registry 一致 |
| contract/auth tests | 26/26 passed |
| authorization | default deny、authorizer failure fail-closed、untrusted context 和 unauthorized backend access 0 |
| CAS / validator | stale revision、invalid projection 和 dedicated collection bypass 均原子拒绝 |
| request idempotency | 同 request/payload receipt replay；同 request/different payload conflict；同服务并发相同 intent 只产生一个 Event并返回同一 receipt |
| claim/effect boundary | Work owner、active Claim、immutable Claim ID、actor、scope、operation、effect key 和 watermark 绑定通过 |
| protocol integrity | root wire schema、read/mutation receipt 和 ok/result/error 关系严格验证；malformed nested intent 返回 integrity；snapshot/Event head revision 撕裂返回 conflict |
| backend availability | PostgreSQL connect/query OperationalError 映射为 stable busy receipt；非 OperationalError 保持显式失败 |
| adapter parity | SQLite/PostgreSQL 对同一 read/claim/effect authorize/effect complete/commit/stale CAS 流程的 receipt、snapshot 和 Event stream 完全一致，1/1 live test passed |
| full repository | 330 tests passed，0 failed，0 skipped；PostgreSQL 18.4 live DSN |
| compile / verifier | Python compile、repository verifier 和 `git diff --check` 通过 |

## 权限与部署边界

默认 `local-embedded` profile 使用 SQLite 和本地 artifact store，不要求独立数据库、daemon、Docker 或网络。PostgreSQL 只在 `shared-strong` 等 opt-in profile 和 live CI 中启用。State MCP service receipt 当前保存在单个服务生命周期内；跨进程 durable request dedupe、lease clock、path hierarchy overlap、tenant isolation、network transport 和 production authentication 分别由 M2-06、M3、M8 和 deployment adapter 交付。

M2-05 不授予 recall provider、Docmost、Skill、模型或 provider adapter 直接修改权威状态的权限。所有后续写入仍须经过 authorization、expected revision/CAS、validator 和专用 claim/effect 边界。

完成门要求的 contract/auth coverage 和 SQLite/PostgreSQL adapter parity 已通过，active queue 可推进至 M2-06。
