# 轻量日志与效果度量设计

## 状态

v1 首批实现。基于 alpha.10 的 core/search/state 插件拆分，已实现独立策略合同、State
MCP 轻量探针、Session 汇总、跨进程安全追加、保守保留和统一离线报告；自动调参、
深度 verifier 调度和 provider token 采集仍不在当前范围。

## 背景

Continuity Plane 需要回答两个不同问题：

1. 恢复、checkpoint、claim 和 Work 转换是否正确发生；
2. 恢复包是否减少了压缩后的重复读取和上下文载荷。

现有本地集成已经能记录 MCP 调用的起止、摘要、资源快照、JSONL hash chain 和
分片 seal，但每次调用保存两份较大的资源文档，并在进程启动时全量扫描历史分片。
随着 Session 增加，日志大小、`fsync` 次数和启动校验时间线性增长。

alpha.10 core 已使用 `context.codex-hook-observation/v1alpha1` 记录生命周期。本设计不
复制或替换该合同：State 插件只写已登记的 `context.light-observation/v1alpha1`，离线
报告通过 project/session hash 读取两类事件。两类目录和保留责任保持隔离。

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

- Hook 生命周期：`session-start`、`precompact`、`postcompact`、`autorun`、
  `hook-error`；
- MCP 边界：`resume`、`state_write_completed`、`state_write_failed`、
  `tool_call_failed`、`slow_call`、`session_end`；
- `diagnostic` 模式额外允许 `tool_call`。

`probes_enabled: false` 关闭普通计数、慢调用和资源采样，但不关闭以下安全记录：

- `state_write_completed`；
- `state_write_failed`；
- `tool_call_failed`。

`resume`、`health`、`audit_verify` 等普通读取不逐条写详细记录，只在 Session 汇总中
累加计数、字节数和耗时。慢调用或失败调用例外。关闭可选探针后，如果产生过上述强制
安全记录，仍写 `session_end` 作为 retention 的可回收边界。

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
策略位于独立的 `.continuity/observability-policy.yaml`，不扩展严格的
`context.project/v1alpha1`。文件缺失时使用下列默认值；早期实验版写入 project profile
的 `continuity_policy` 只做内存迁移，不回写 profile。

```yaml
schema_version: context.observability-policy/v1alpha1
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
`diagnostic`。长期策略必须写入独立策略文件，且不得在配置中保存凭据、机器绝对路径或
原始会话数据。

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
  "event_type": "state_write_completed",
  "observed_at_utc": "2026-08-29T09:00:00.000000+00:00",
  "session_sha256": "...",
  "project_sha256": "...",
  "policy_sha256": "...",
  "preset": "balanced",
  "success": true,
  "duration_ms": 18.4,
  "tool_name": "continuity_work_complete"
}
```

### 通用字段

- `schema_version`：事件合同版本；
- `event_type`：固定枚举；
- `observed_at_utc`：UTC 时间；
- `session_sha256`、`project_sha256`：隐私化关联 ID；
- `policy_sha256`、`preset`：本次 Session 固定的策略标识；
- `success`：布尔结果；
- `duration_ms`：边界操作耗时。

### 按需字段

- `packet_bytes`：恢复包的 UTF-8 字节数；
- `tool_name`：仅 State 写、失败或慢调用；
- `error_category`：经过 allowlist 的错误类别，不记录原始异常文本；
- `request_bytes`、`response_bytes`：仅 diagnostic 或慢调用；
- `input_tokens`、`cached_input_tokens`、`output_tokens`、
  `reasoning_tokens`：仅 host 明确提供；
- `measurement_source`：`host`、`plugin` 或 `unavailable`。

默认不记录完整 Work、claim、checkpoint 或 event head。需要关联时只记录 `work_id`、
`claim_id` 和 checkpoint digest 的短前缀；权威内容仍从 State 按 revision 查询。
State observation schema 使用 `additionalProperties: false`；运行时只接纳 schema 明确声明
的计数、资源、工具名和 provider usage 字段。调用方传入的 `schema_version`、
`event_type`、`success`、prompt、原始异常或响应正文均不能覆盖或扩展持久记录。

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
  "resume_calls": 1,
  "duplicate_resumes": 0,
  "request_bytes_total": 512,
  "response_bytes_total": 21140,
  "latency_buckets": "0,1,4,3,2,0,0,0,0",
  "observation_degraded": false
}
```

该汇总足以计算：

- MCP 读写与失败调用数量；
- 重复 resume 比例；
- 请求/响应本地字节量；
- 固定桶调用延迟分布；
- observation 是否发生降级。

恢复、压缩和 canary 结果从对应 Hook 边界事件统计。只有 host 将 token usage 明确提供给
observation 合同时，报告才允许计算 token 指标；当前实现默认标记为不可用。

## 资源采样

默认不在每次工具调用前后扫描磁盘或读取完整进程资源。

资源采样仅发生在：

- SessionStart；
- SessionEnd；
- 调用失败；
- 调用耗时超过 `slow_call_threshold_ms`，默认 1000 ms；
- 用户显式调用 health/diagnostic。

采样字段只保留当前 RSS（Linux/Windows；其他 POSIX 无当前值时明确使用
`peak_rss_bytes`）、State 存储字节和当前 Session 日志字节。Python heap、文件系统
总量和逐字段 delta 不进入默认持久日志。

## 持久化和校验

### 写入

- core Hook 继续写 alpha.10 的 `live-events/`；State MCP 写独立的
  `state-mcp-events/`，两者不共享写入生命周期；
- 每个 State MCP Session 使用独立 JSONL 文件；所有追加和清理尝试取得一个不会删除的
  目录级 `.retention.lock`，避免锁 inode 被替换后新旧 writer 分裂；线程锁或 OS 锁繁忙
  时立即放弃本次观测/清理并标记降级，不等待、不阻塞 State/MCP；
- 每条记录使用 canonical JSON；
- 单条记录循环写到全部字节完成；短写后的失败会在持锁期间回退到写入前文件大小，避免
  把半行当作成功观测；
- observation 不承担 State authority，因此不增加 hash chain、seal 或逐记录 `fsync`；
- 普通读取计数保存在内存中，在 Session 汇总时一次写入；
- MCP 正常结束时写 `session_end`；异常退出的 State 文件允许缺少该事件，并由保守的
  orphan retention 处理。core Hook 的 retention 仍由 core 合同负责。

### 后续完整性校验（未实现）

v1 不在 SessionStart 扫描或重放历史 observation。若未来增加 observation 完整性校验，
默认仍只允许检查：

- 当前 Session 文件；
- 最近一个已 seal 分片；
- 未 seal 的 orphan 分片。

不得在普通 SessionStart 全量 `rglob` 并重放全部历史。完整校验只能通过未来显式的
deep 工具执行，不能进入恢复热路径。

### 保留策略

v1 不引入复杂归档系统。State MCP 默认上限为 64 MiB，在 Session 结束时执行 retention；
只删除最旧的已完成 Session，或超过 24 小时且不是当前 Session 的 orphan 文件。追加和
删除只有在非阻塞取得同一个稳定目录锁后才执行，因此当前文件和正在写入的文件不会与
清理竞态；锁繁忙时本轮 retention 直接跳过。稳定锁文件不删除，也不额外写
`retention_prune` 事件。

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
- State 写或 compaction 边界：至多一次非阻塞有锁追加，不执行日志 `fsync`；
- 1000 条历史记录下 retention/report 不进入普通工具调用热路径；
- 日志关闭或 retention 不阻塞 State transaction；
- 日志故障不得把已提交的 State 伪装成未提交，返回结果必须允许按 State revision
  对账。

## 兼容与迁移

- 现有 MCP access audit JSONL 保持只读，不重写、不导入新热路径；
- 新 State 实现使用已登记的新 schema 和目录，避免 core/旧 verifier 误判；
- core Hook 和 State MCP 保持各自合同，通过公共 hash 字段在报告阶段关联；
- 上游 hook 模型是目标基线，本地资源审计只迁移其中仍有价值的摘要字段；
- 旧日志的 deep verify 保留为兼容工具，不在 SessionStart 调用。

## 测试

已实现的最小测试集：

1. core manifest 仅注册 SessionStart、PreCompact、PostCompact，默认 auto/observe 不阻塞；
2. 普通读取只增加内存计数，不产生逐调用持久记录；
3. 关闭可选探针后只保留强制安全记录，并以 `session_end` 形成可回收文件；
4. provider usage 缺失时不出现伪造 token 值；
5. 线程和真实多进程 append+prune 不发生交叉、半行或锁删除竞态，锁繁忙立即降级；
6. observation 目录不可写时不阻断 State 操作；
7. retention 保留当前 Session，删除已完成或保守判定的旧 orphan；
8. 报告容忍损坏行，并限制单文件读取尾部大小；
9. 缺少独立策略文件的项目得到 `balanced` 默认行为，旧字段仅内存迁移；
10. preset 覆盖、非法枚举和数值上下限通过测试；
11. 无效恢复包不建立 MCP binding，普通读取复用已有 binding；
12. 所有 State 写及 checkpoint create 在执行前后刷新 binding，过期 lease/source 拒绝写；
13. 策略和 State observation 均通过 Draft 2020-12 schema 与公共 registry hash 校验。
14. extra 白名单不能覆盖权威字段，短写会补齐或回退，不产生已报告成功的半行。

后续测试包括真实崩溃恢复、POSIX/macOS 进程指标、磁盘满和跨进程长期压力；这些不在
当前实现的已验证声明中。

## 分阶段实现

### 阶段 1：合同与插件边界

- 增加最小事件合同；
- 定义独立 policy schema、`balanced` 默认值和兼容解析；
- 复用 alpha.10 core Hook 生命周期观测，不向 core 加回 State MCP；
- State observation 进入公共 schema registry；
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
