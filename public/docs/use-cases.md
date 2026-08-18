# 使用场景

[English](use-cases.en.md)

Continuity Plane 处理的是长期 AI 辅助开发中反复出现的状态、检索和协作事故。
每个场景都区分当前已验证的控制面合同和仍需前端或 provider adapter 的部分。

## 压缩与长 Session

常见表现：压缩后 Agent 重答已经完成的问题，重新读取大量文件，把已完成或回滚
的工作重新列为待办，或在恢复后直接执行副作用。

控制面保存 active leaf、latest decision、constraint、return point、acknowledged
input、continuation cursor 和 effect watermark。PostCompact canary 在写代码、提交、
部署或其他副作用前验证这些字段；不一致时恢复为只读。

当前实测的匹配任务中，冗余历史输入下降 `40.25%`，近上限历史输入下降 `95.06%`，
expected-answer quality 为 `3/3`。这组结果暂不代表真实窗口利用率或压缩间隔已普遍
提高；纵向指标见 [实测方法](benchmarks.md)。

## 多 Session、多人和部署竞态

常见表现：Session A 正在部署时，Session B 合入了新的 main；两个 Session 同时发布、
回滚或修改同一 migration；PR、CI 和本地 branch 各自显示不同 head；失败重试重复
执行同一个外部操作。

Work Ledger、claim/lease、path ownership、expected revision、effect identity 和
通知 cursor 把部署、review、merge 和 rollback 绑定到同一状态。SQLite local profile
覆盖同一设备的多个 Session；跨设备唯一 claim 使用显式 shared State 或 forge adapter。
PostgreSQL 和 Docmost 都不是普通 PR 的前置条件。

当前协作实测：重复 tool call 下降 `55.88%`，并行 wall time 下降 `22.65%`；双 Session
同 revision `1000/1000`，重复通知抑制 `1000/1000`，离线 catch-up `2000/2000`，
authority violation `0`。

## 多 Agent 重复实现

一个协作者在本地实现了模块，另一个协作者看不到 unpublished Work 又实现一遍；
reviewer 重新检索已经验证过的源码和官方文档；交接只剩摘要，丢失 blocker、next
action 和 return point。

Project Graph、Work Ledger、Evidence Matrix 和 forge projection 记录 owner、scope、
branch、claim、证据和当前 revision。memory 和模型输出只能产生候选，不能完成
Work，也不能授予副作用权限。

## Idea、中断与任务切换

执行中出现的想法先进入 candidate/parked 队列。默认动作是 capture-and-continue，
原 active leaf 不改变。只有明确 switch，或经过 review、CAS、attempt budget、expiry
和 promotion gate，Idea 才能进入 canonical queue。

原任务的 checkpoint 保留 return point 和禁止副作用。恢复时只加载当前 packet 与相关
Idea ref，避免把旁支讨论重新灌入主线。

## 大型项目定位与影响分析

目录树无法表达跨仓依赖、任务关系、决定历史和修改影响。人和 AI 都需要知道当前
Work 属于哪个 Campaign、谁拥有它、它依赖什么、会影响哪些 Product 和测试。

Project Graph 提供确定性 DAG，Relationship/Impact 提供有界的节点、边、cluster、
focus 和 impact set。Decision Timeline/Evidence Matrix 解释为什么做、何时推翻以及
完成声明依赖哪些证据。

当前 projection core 已通过规模门；完整 Docmost Web UI、Obsidian Canvas/Bases 和
跨前端交互仍按 [图形化产品计划](visual-products.md) 实施。

## Memory、Skill 与文档漂移

历史 memory 中的旧路径、旧决定和旧约束只能作为 candidate。Skill 使用 manifest、
version、hash、rule IDs、applicability、license、dependency 和 expiry；漂移、冲突或
缺失进入 quarantine。MASTER 保存治理主线，STATUS 保存当前路由，报告和投影视图不能
直接改变 active state。

当前 Skill source bytes 下降 `96.54%`。provider token、window utilization 和长期
压缩间隔只有在 host trace 可见时才计为 measured。

## 质量与故障恢复

系统不以 token 降幅替代质量门。E0-E9 当前 `10/10`，compaction、Idea、interrupt 和
worker-loss fault `4/4`；stale history revival、静默 CAS 覆盖和 authority violation
均为 `0`。完整数据、测试方法、verifier 成本和限制见 [实测方法](benchmarks.md)。
