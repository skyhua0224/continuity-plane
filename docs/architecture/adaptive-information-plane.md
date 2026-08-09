# Adaptive Information Plane Contract

版本：1  
日期：2026-08-09  
状态：architecture contract / M2-M10 implementation planned

## 目标

Context Control Plane 的信息访问路径应减少重复的全仓、全档案和全历史读取，同时保持承重事实可验证。外置存储、索引和候选记忆只承担定位、缓存和候选召回；当前状态、当前源码和带 provenance 的权威来源决定可执行结论。

优化对象包括：

- 从长期历史中定位与当前 active leaf 相关的 material event；
- 从代码、标准、OS/软件官方文档中定位最小承重片段；
- 在压缩、handoff、IDEA capture 和任务恢复时重建最小 Execution Packet；
- 复用已验证的检索结果、Skill packet 和 artifact range，避免重复读取；
- 根据已验证运行、用户纠正和失败样本提出项目适配改进。

## 信息访问层

一次 bounded lookup 必须产生可复核的 `RetrievalReceipt`：

```yaml
request_id: opaque-id
task_revision: uint64
query_kind: state | code | reference | memory | artifact
source_ref: opaque-id
source_revision: string
source_hash: sha256
selection_method: exact | symbol | range | metadata | candidate
selected_refs: [artifact-ref | assertion-id | event-id]
bytes_read: uint64
bytes_emitted: uint64
freshness: current | stale | unknown
verification: passed | failed | pending
```

访问顺序按问题类型裁剪：

1. `state` 只读取当前 revision 的 typed fields、checkpoint 和 event high watermark；
2. `code` 优先使用 `rg`，再按需要使用 Zoekt、LSP、SCIP 和 CodeGraph 影响线索；承重符号由 `rg/LSP` 双检；
3. `reference` 使用固定 revision/hash 的 catalog snapshot 和 bounded range；动态来源先做 freshness 检查；
4. `artifact` 通过 content-addressed hash 和 byte range 展开；大日志默认只返回摘要、行号/范围和 hash；
5. `memory` 只返回带时间、source ref 和 validity 的候选，必须与当前 state/evidence 比较后才能进入 packet。

`bytes_emitted` 是进入模型上下文的代理计量。provider 提供真实 token、cache 或 latency 时同时记录实测字段；未提供时保持 `null`，不得用字符数冒充 provider token。

## Execution Packet 组合

Packet 只包含当前 active leaf 的任务字段、最新决定、不可违反约束、blocker、return point、唯一 next action、锁定 Skill rule IDs 和承重 evidence refs。候选 Idea、memory、完整报告和大型代码输出只保留 opaque refs；只有验证通过的 bounded expansion 才能进入上下文。

每个 expansion 需要满足：

- `task_revision`、claim、path owner 和 packet digest 一致；
- source revision/hash 与 receipt 一致；
- current-evidence verification 通过；
- requested range 不超过操作和 packet budget；
- 失败、过期或 hash 漂移时返回只读/quarantine 结果。

## 受约束自适应

安装到项目后的适配循环为：

```text
observe verified runs and explicit corrections
-> aggregate bounded metrics and failure samples
-> generate versioned ProjectAdaptation proposal
-> shadow/A-B replay against current profile
-> human or governance-owner approval
-> lock profile hash and activate at a new revision
-> monitor safety vetoes and rollback on regression
```

`ProjectAdaptation` 只能描述项目和用户层面的可调参数，例如检索优先级、常用目录/命令、验证 Profile 提示、Skill applicability 和非安全表达偏好。它不得改变 active task、claim、path ownership、authorization、validator、evidence gate、promotion gate、retention 或 provider-neutral 协议。

每个 proposal 至少包含：

```yaml
adaptation_id: stable-id
profile_id: stable-id
version: semver
content_sha256: sha256
scope: project | user | provider-adapter
inputs: [run-id | correction-id | failure-fixture | evidence-ref]
applicability: [project-id | repo | path | operation | provider]
metrics_before: object
metrics_after: object
safety_veto_results: object
replay_receipts: [artifact-ref]
expiry: rfc3339 | null
rollback_to: profile-version | null
status: candidate | shadow | approved | active | quarantined | rejected | superseded
```

只有明确批准且通过 replay、A/B 和安全门的 proposal 才能影响后续 packet。项目升级、provider 更换或 Skill/schema 变更会触发兼容检查；不兼容时保留旧 profile、建立 migration 或回滚，禁止静默替换。用户可查询、批准、拒绝、暂停、重置或导出自己的适配记录。

## 观测与验收

至少记录以下可比较指标：

- `bytes_read`、`bytes_emitted`、重复读取 bytes 和重复检索次数；
- 承重 assertion precision、freshness 和 current-evidence verification 通过率；
- state/decision/constraint/return point 恢复率、stale decision 复活、伪路径和无证据完成；
- Skill body 与 compiled packet bytes、规则遵循、quarantine；
- restore、PreCompact、检索和验证时延；
- 用户 correction、返工、误切、副作用重试和 CAS conflict；
- proposal 采用率、回滚率、expiry 和 opt-out/reset。

优化声明必须同时满足：相同 fixture、provider、budget 和 Verification Profile 至少三次可比较样本；所有 E1/E2/E4/E6/E8/E9 veto 通过；build/test/mutation/live coverage 不退化。缺少 provider telemetry 时只声明本地 proxy 或 `null` limitation。

## 失败处理

外置 memory、索引、缓存或 reviewer 不可用、返回 503、超时、过期或结果冲突时，系统回退到当前 State MCP、当前源码和固定官方 snapshot；权威状态和 active task 不受影响。任何无法证明 freshness、provenance 或权限的结果只能显示为 candidate/quarantined，并且不能触发副作用。
