# 轻量日志与效果度量设计

## 状态

v1 首批实现。已实现有界策略解析、Hook/MCP 轻量探针、Session 汇总、并发安全追加、
安全保留和离线调优报告；自动调参、深度 verifier 调度和 provider token 采集仍不在
当前范围。

## 背景

Continuity Plane 需要回答两个不同问题：

1. 恢复、checkpoint、claim 和 Work 转换是否正确发生；
2. 恢复包是否减少了压缩后的重复读取和上下文载荷。

现有本地集成已经能记录 MCP 调用的起止、摘要、资源快照、JSONL hash chain 和
分片 seal，但每次调用保存两份较大的资源文档，并在进程启动时全量扫描历史分片。
随着 Session 增加，日志大小、`fsync` 次数和启动校验时间线性增长。

上游当前以 Codex hook observation 为主要观测面。本地 MCP access audit 应作为迁移
来源合并到同一轻量合同，而不是继续维护两套无法关联的日志。

## 目标

- 默认配置对模型 token 消耗为零：日志不自动注入模型上下文；
- 能证明一次 SessionStart、PreCompact、PostCompact 和关键 State 转换是否成功；
- 能用隐私化 ID 关联 hook、MCP 和 State revision；
- 普通调用不递归扫描 `.continuity/`，不采集完整资源快照；
- 默认记录保持在几百字节，失败记录保持在 2 KiB 以内；
- provider 未提供 token usage 时明确标记不可用，不从字节数推算 token；
- 保留当前本地使用需要的损坏检测能力，但不引入重型安全基础设施。

## 非目标

- 不记录原始聊天、提示词、推理文本、源码、命令输出或工具响应正文；
- 不在默认路径接入 HMAC、远端时间戳、外部日志服务或 SIEM；
- 不为日志单独引入 PostgreSQL、消息队列、后台 worker 或索引服务；
- 不试图从 UTF-8 字节数估算不同 provider、model 或 tokenizer 的 token；
- 不把本地日志作为 State、Event Log 或 checkpoint 的替代 authority；
- 不承诺跨设备、跨用户的对抗性防篡改。

## 核心原则

### 1. State 负责权威，日志负责解释

Work、claim、revision、checkpoint 和完成状态已经由 State/Event Log 持久化。日志只
记录“哪个 Session 在什么边界观察或调用了什么，以及结果如何”，不复制完整 State。

### 2. 只记录边界，不记录每一步思考

默认需要持久化的事件只有：

- `session_start`；
- `pre_compact`；
- `post_compact`；
- `state_write_completed`；
- `state_write_failed`；
- `session_end`。

`resume`、`health`、`audit_verify` 等普通读取不逐条写详细记录，只在 Session 汇总中
累加计数、字节数和耗时。慢调用或失败调用例外。

### 3. token 与本地字节分开

恢复包字节、hook additionalContext 字节和工具响应字节是本地可测量值。input、cached
input、output 和 reasoning token 只有 provider/host 明确提供时才记录。

两类数据必须使用不同字段和 `measurement_source`，避免把 source bytes 包装成 token
收益。

## 默认模式

日志提供两个模式：

- `minimal`：默认。记录边界事件、State 写结果和 Session 汇总；
- `diagnostic`：显式、临时启用。增加逐工具耗时、摘要和资源采样。

不增加第三套长期运行的“完整审计”模式。需要排障时短暂启用 `diagnostic`，排障结束
后恢复 `minimal`。

## 配置模型

上述行为必须可配置，但配置面保持有界，避免大量独立布尔开关形成不可测试的组合。
项目配置以 `.continuity/project.yaml` 为入口，新增单一 `continuity_policy` 命名空间；
实现时需要先扩展 profile schema 和兼容校验，旧配置缺少该段时使用下列默认值。

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

`preset` 只提供少量经过测试的组合：

- `balanced`：默认，即上面的轻量策略；
- `diagnostic`：临时增加逐工具耗时和资源采样，不改变 State authority；
- `reliability-first`：增加 checkpoint 和增量校验频率，用于异常恢复期。

显式子项覆盖 preset，但字段必须使用枚举或有上下限的数值。安全底线不可关闭：
PreCompact checkpoint、State revision/CAS、claim fencing、失败记录和恢复 canary 始终启用。
配置在 SessionStart 读取并固定到当前 Session；运行中修改只对下一个 Session 生效，避免
同一 Session 前后语义漂移。每条 Session 汇总记录配置摘要 hash，便于比较策略效果，但
不复制完整配置内容。

环境变量只允许覆盖临时运行参数，例如把 `observability.mode` 临时提升为
`diagnostic`。长期策略必须写入项目配置，且不得在配置中保存凭据、机器绝对路径或原始
会话数据。

## 轻量探针与调优闭环

探针默认开启，但必须比被测操作更轻。探针记录原始事实，不在热路径计算复杂评分，也不
自动修改配置。v1 只在 Session 内维护计数器和固定桶直方图，并在边界事件或 Session
汇总时一次落盘。

### 默认探针

- `resume_probe`：恢复次数、原因、packet bytes、耗时和是否重复绑定；
- `checkpoint_probe`：触发原因、耗时、距上次 checkpoint 的间隔、是否被去重；
- `compaction_probe`：PreCompact/PostCompact 配对、canary、恢复到首个有效动作的耗时；
- `verification_probe`：校验范围、扫描分片数/字节数、耗时和首个错误类别；
- `state_probe`：读写计数、写入耗时、revision conflict 和 claim/lease 拒绝计数；
- `resource_probe`：边界/失败/慢调用时的 RSS、State/日志磁盘字节；
- `usage_probe`：仅记录 host 明确提供的 input/cached/output/reasoning token。

探针不得记录工具响应正文、原始异常、提示词、源码或 transcript。普通成功读取只增加内存
计数；一次操作只有在失败或超过慢调用阈值时才产生独立记录。直方图使用固定桶，不保存
每次调用的无界明细。

### 调优输出

提供显式的本地报告命令，例如 `continuity observe report`，按配置摘要比较最近若干个
已完成 Session，并输出证据和建议：

- 重复 resume 比例高时，建议检查 MCP 连接生命周期；
- checkpoint 被短时间重复创建时，建议提高 `min_interval_seconds` 或关闭普通 State
  写后的 checkpoint；
- recent verify 仍扫描过多历史时，建议修复游标或 shard seal，而不是直接关闭校验；
- 慢调用集中在 deep verify 时，建议保持 manual；
- compaction 后恢复失败时，建议临时切换 `reliability-first` 或 `diagnostic`。

v1 不做自动调参。只有当多个版本积累了足够样本，并且策略变更具备上下限、冷却时间、
自动回滚和可复现实验后，才考虑加入 opt-in autotune。任何未来自动调参都不得关闭上述
安全底线。

### 探针自身预算

- 普通调用探针更新 P95 小于 0.1 ms，且不执行磁盘读取或 `fsync`；
- Session 汇总序列化后不超过 2 KiB；
- 固定桶和计数器的 Session 内存预算不超过 64 KiB；
- 探针写入失败不得阻断 State transaction，只记录一次降级状态；
- 报告生成离线读取本地 observation，不进入 resume packet，也不在 SessionStart 自动运行。

## 最小事件合同

典型记录应保持扁平，避免重复嵌套整个请求、响应或资源文档：

```json
{
  "schema_version": "context.light-observation/v1alpha1",
  "event_type": "post_compact",
  "observed_at_utc": "2026-08-29T09:00:00.000000+00:00",
  "session_sha256": "...",
  "turn_sha256": "...",
  "project_sha256": "...",
  "success": true,
  "duration_ms": 18.4,
  "state_revision": 42,
  "packet_bytes": 5928,
  "canary_passed": true
}
```

### 通用字段

- `schema_version`：事件合同版本；
- `event_type`：固定枚举；
- `observed_at_utc`：UTC 时间；
- `session_sha256`、`turn_sha256`、`project_sha256`：隐私化关联 ID；
- `success`：布尔结果；
- `duration_ms`：边界操作耗时；
- `state_revision`：操作完成后观察到的 State revision。

### 按需字段

- `packet_bytes`：恢复包或 additionalContext 的 UTF-8 字节数；
- `recovery_read_bytes`、`recovery_read_budget_bytes`：压缩后有界读取；
- `canary_passed`：PostCompact 验证结果；
- `tool_name`：仅 State 写、失败或慢调用；
- `error_category`：经过 allowlist 的错误类别，不记录原始异常文本；
- `request_bytes`、`response_bytes`：仅 diagnostic 或慢调用；
- `input_tokens`、`cached_input_tokens`、`output_tokens`、
  `reasoning_tokens`：仅 host 明确提供；
- `measurement_source`：`host`、`plugin` 或 `unavailable`。

默认不记录完整 Work、claim、checkpoint 或 event head。需要关联时只记录 `work_id`、
`claim_id` 和 checkpoint digest 的短前缀；权威内容仍从 State 按 revision 查询。

## Session 汇总

`session_end` 或下次恢复 orphan Session 时生成一条汇总：

```json
{
  "schema_version": "context.light-observation/v1alpha1",
  "event_type": "session_end",
  "session_sha256": "...",
  "read_calls": 8,
  "write_calls": 2,
  "failed_calls": 0,
  "compactions": 1,
  "accepted_work": 1,
  "packet_bytes_total": 5928,
  "tool_output_bytes_total": 21140,
  "provider_usage_available": false
}
```

该汇总足以计算：

- 恢复成功率；
- 每次可见压缩完成的 accepted Work；
- 压缩后恢复字节与读取预算；
- 恢复到首个成功 State 写或有效动作的耗时；
- provider usage 可用时的 tokens per accepted Work。

## 资源采样

默认不在每次工具调用前后扫描磁盘或读取完整进程资源。

资源采样仅发生在：

- SessionStart；
- SessionEnd；
- 调用失败；
- 调用耗时超过 `slow_call_threshold_ms`，默认 1000 ms；
- 用户显式调用 health/diagnostic。

采样字段只保留 RSS、State 存储字节、Continuity 总字节和日志总字节。Python heap、
文件系统总量和逐字段 delta 留在 diagnostic 输出，不进入默认持久日志。

## 持久化和校验

### 写入

- 每个 Session 使用独立 JSONL 文件，避免多进程竞争同一文件；
- 每条记录使用 canonical JSON；
- 保留分片内 SHA-256 chain 和关闭时 seal；
- 默认每个边界或 State 写最多执行一次 `fsync`；
- 普通读取计数保存在内存中，在 Session 汇总时一次写入；
- 进程异常退出时允许缺少 `session_end`，下一次启动写 `session_recovered`。

### 启动校验

启动只校验：

- 当前 Session 文件；
- 最近一个已 seal 分片；
- 未 seal 的 orphan 分片。

不得在普通 SessionStart 全量 `rglob` 并重放全部历史。完整校验只通过显式
`continuity audit verify --deep` 或等价工具执行。

### 保留策略

v1 不引入复杂归档系统。默认只设置一个简单上限：日志总量超过 64 MiB 时删除最旧、
已经 seal 且通过校验的分片，并在当前分片写入 `retention_prune` 记录。活跃、未 seal、
损坏或无法验证的分片不得自动删除。

## token 边界

- 日志文件从不自动加入 resume packet；
- health 默认只返回一行摘要，不返回分片列表或资源明细；
- audit verify 默认只返回计数、耗时和首个错误类别；
- 详细报告写本地 artifact，由用户显式打开；
- hook additionalContext 继续执行现有字节上限，目标常态不超过 6 KiB；
- 同一 MCP Session 首次写前执行一次 resume，普通问答不重复 resume。

因此日志量只影响本地磁盘和极小的边界 I/O；除非用户显式读取详细 artifact，否则
不会增加模型 token。

## 性能预算

以下是实现验收预算，不是未经测量的性能声明：

- 常规 minimal 记录：序列化后不超过 512 B；
- 失败记录：不超过 2 KiB；
- 普通读取调用：不执行持久日志 `fsync`；
- State 写或 compaction 边界：最多一次日志 `fsync`；
- 1000 条历史记录下最近分片启动校验 P95 不超过 50 ms；
- 日志关闭或 retention 不阻塞 State transaction；
- 日志故障不得把已提交的 State 伪装成未提交，返回结果必须允许按 State revision
  对账。

## 兼容与迁移

- 现有 MCP access audit JSONL 保持只读，不重写、不导入新热路径；
- 新实现使用新的 schema 和目录，避免旧 verifier 误判；
- hook observation 与 MCP 最小事件共用字段命名和关联 ID；
- 上游 hook 模型是目标基线，本地资源审计只迁移其中仍有价值的摘要字段；
- 旧日志的 deep verify 保留为兼容工具，不在 SessionStart 调用。

## 测试

最小测试集：

1. SessionStart、PreCompact、PostCompact 和 State 写生成预期事件；
2. 普通读取只增加内存计数，不产生逐调用持久记录；
3. provider usage 缺失时不出现伪造 token 值；
4. crash 后产生 `session_recovered`，且不重写原 JSONL；
5. 最近分片损坏会阻止信任该分片，但不会扫描全部历史；
6. 并发 Session 各写独立文件，不发生交叉或半行记录；
7. 日志目录只读、磁盘满、时钟回拨和 PID 复用返回可诊断错误；
8. 1000 条记录基准满足大小、启动和写入预算；
9. detailed artifact 不会被自动注入恢复上下文；
10. 旧 audit 日志仍可通过显式 deep verify 校验。
11. 缺少 `continuity_policy` 的旧项目得到 `balanced` 默认行为；
12. preset 覆盖、非法枚举、数值上下限和 Session 内配置固定行为通过测试；
13. 普通调用探针不落盘，Session 汇总包含配置摘要和固定桶统计；
14. 调优报告只给出有证据的建议，不修改项目配置。

## 分阶段实现

### 阶段 1：合同与 hook 边界

- 增加最小事件合同；
- 定义 `continuity_policy` schema、`balanced` 默认值和兼容解析；
- 统一 hook 的 Session/turn/project 关联字段；
- 记录 compaction、packet bytes、canary 和 Session 汇总；
- 不改 State schema。

### 阶段 2：MCP 最小记录

- State 写和失败调用接入同一合同；
- 普通读取改为 Session 内计数；
- 增加固定桶探针和配置摘要关联；
- 资源快照改为边界/失败/慢调用采样；
- SessionStart 改为最近分片校验。

### 阶段 3：验证与基准

- 增加 crash、并发、磁盘失败和 PID 复用测试；
- 记录 1000 条事件的文件大小、写入 P50/P95 和启动校验 P95；
- 增加离线调优报告，不自动修改配置；
- 若性能预算失败，优先减少字段和采样，不引入新服务。

## 暂缓事项

除非出现明确的共享部署或合规需求，以下事项不进入当前实现：

- HMAC、非对称签名和远端锚定；
- 集中日志服务和跨项目查询；
- 每调用完整资源快照；
- provider token 的本地估算；
- 原始 transcript 或工具输出采集；
- 为日志单独建立新的数据库或后台进程。
