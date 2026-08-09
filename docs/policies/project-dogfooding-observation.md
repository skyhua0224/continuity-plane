# Project Dogfooding Observation Policy

版本：1  
日期：2026-08-09  
状态：implemented baseline / automated telemetry planned

## 目标

Context Control Plane 在自身研发过程中持续记录压缩恢复、消息与 Idea 路由、Skill 装载、任务切换、MASTER 演进和验证结果。项目只能基于可比较数据声明优化趋势；主观顺畅感和单次成功属于辅助观察。

## 观察事件

| 事件 | 必记字段 |
|---|---|
| compaction/handoff/model change | active leaf before/after、关键字段恢复、stale decision、误切、return point、输入 bytes/token、restore latency |
| message/Idea/context addition | input kind、candidate ID/summary hash、route、active leaf before/after、return point、context window 可见性、tool interruption |
| Skill selection/load | skill ID、version/hash、触发原因、body bytes、重复装载 bytes、quarantine、rule drift |
| task/Idea switch | parent、authority、checkpoint、return point、claim/path owner、未授权副作用 |
| MASTER revision | before/after、用户/治理来源、任务 diff、mission 是否变化、return point |
| verification | test/build/mutation/live 结果、scope violation、返工和 evidence refs |

观察只记录可见消息、文件、工具调用、hash、计数、时延和验证结果，不记录模型隐藏 reasoning、原始 provider thread ID、secret、PII 或完整 transcript。

Provider 未提供实时 context window、input/cache token、自动压缩信号或 restore latency 时，对应字段保持 `null` 并记录 limitation。字符数、Skill bytes 和 summary bytes 可以作为独立 proxy 指标，但不得替代 provider token 或 latency。Checkpoint summary 的出现只能证明发生了可见恢复边界，不能推导压缩算法、丢弃范围或隐藏上下文占用。

## 触发时点

- PreCompact 前和 PostCompact canary 后；
- 用户消息、Idea、correction、status query 或 interrupt 完成路由后；
- task、model、provider、collaborator 或 active claim 变化时；
- Skill/AGENTS/provider contract hash 或选择结果变化时；
- MASTER、STATUS、schema registry 或 Verification Profile revision 变化时；
- release、rollback、SIGKILL、503、checkpoint 损坏和 CAS conflict 实验后。

M2/M8 上线前，脱敏观察保存在 `experiments/dogfood/`，当前 active 状态仍只由 STATUS 路由。State MCP 上线后，观察写入 append-only event/artifact；OTel 上线后通过 `context.*` trace 自动关联 run、task revision、packet、Skill digest 和 evidence。

## 趋势判定

任何 safety veto 失败都标记 `regressed`：关键字段恢复低于 100%、stale decision 复活、未授权 task/goal change、重复 effect、静默 CAS 覆盖或 scope violation。至少三个相同 fixture/provider/budget 的 compaction 样本才允许判断恢复输入、Skill 重复装载、token 或时延趋势。

在全部 veto 保持通过时，以下变化才构成优化信号：

- state-only restore 与 PreCompact p95 下降；
- 恢复输入、重复 Skill body、重复检索 token 下降；
- 用户纠正、返工、无证据完成和错误 task switch 下降；
- build/test/mutation/live coverage 不退化；
- MASTER 变更具有明确来源，项目使命和 active leaf 无未授权漂移。

少于三个可比较观测只建立 baseline，状态为 `baseline-insufficient-samples`。缺少 provider token、latency 或 cache trace 时保留 null 和 limitation，禁止以字符估算替代 live 指标。

## 当前基线

结构化样本位于 [`experiments/dogfood/observations-2026-08-09.yaml`](../../experiments/dogfood/observations-2026-08-09.yaml)。当前记录五次 checkpoint/摘要恢复、十四次 input routing、十六个 Skill body 装载、十次用户授权的 MASTER 演进和 repository verification。关键字段恢复为 100%，stale decision、未授权 task switch、未授权 goal change、verification failure 和 scope violation 均为 0；Skill body 累计输入为 185,560 bytes，其中压缩恢复后的重复 body 装载为 73,910 bytes。五次 compaction 均缺少可比较的 provider context/token/latency 数据，趋势状态保持 `baseline-insufficient-samples`。第 4 次恢复检测到 `STATUS.md` 落后于已生成 fixture 和 benchmark 的仓库证据，该事件作为 M0-10 freshness validator 的真实反例，不计为恢复字段丢失。

## 验收门

- observation schema、ID、时间、hash、计数和 event-specific fields 校验通过；
- Skill body 总量与各条目 hash/bytes 可复核；
- message/Idea route 的最小摘要与 hash 一致；`capture-and-continue` 保持 active leaf 和 return point；
- provider 未暴露的 context/token/cache/latency 字段保持 `null`；
- 无 authority 的 MASTER revision 被拒绝；
- 少于三个可比较样本时趋势保持 `baseline-insufficient-samples`；
- recovery、stale、误切或未授权 goal 任一失败时趋势为 `regressed`；
- 三个以上可比较样本后才输出 improving/stable，并同时报告 veto 指标。
