# 使用场景

[English](use-cases.en.md)

如果你让 Agent 连续工作数小时，或者同时打开多个 Session，下面这些事故很容易发生。
这些事故通常源于当前任务、权限和副作用没有一个独立于聊天窗口的稳定状态，
与代码能力本身无关。

## 压缩后像换了一个人

- 压缩前 Agent 正在修测试，压缩后又回答一次你半小时前已经问过的问题；
- 你只说“继续”，它重新读取 MASTER、重新规划，甚至重做已经完成的工作；
- 被拒绝、回滚或已经完成的方案重新出现在待办里；
- 为了恢复上下文又读了一大堆源码和文档，刚压缩不久窗口再次被填满；
- 1M context 延缓了压缩，却没有自动解决重复历史和无效输入。

控制面把 active task、最新决定、约束、return point、已确认输入和首个允许动作
写入 checkpoint。恢复先过 canary，再允许代码修改、提交或部署。当前匹配任务中，
冗余历史输入下降 `40.25%`，近上限历史输入下降 `95.06%`，答案质量 `3/3`。

## 同一仓库的多个 Session 会互相踩踏

- Session A 正在部署，Session B 刚把另一个 revision 合入 main；
- 两个 Session 都判断“没人部署”，同时发布、回滚或更新同一个环境；
- 一个 Session 修改 PR 或 force-push，另一个仍按旧 branch/head 工作；
- 失败重试没有 effect identity，重复执行同一个外部操作。

Work Ledger 记录谁认领了什么范围、哪个 revision 有效、部署是否正在进行。SQLite
默认支持同一台机器的多 Session；跨设备唯一 claim 使用已有 Git forge 或显式 shared
State。PostgreSQL 和 Docmost 都是可选项。当前实测 duplicate tool calls 下降
`55.88%`，双 Session 同 revision `1000/1000`，authority violation `0`。

## 多人和多 Agent 不知道别人已经做了什么

- 一个人已经在本地实现了模块，另一个人看不到 unpublished work，又实现一遍；
- reviewer、executor 和部署 Session 反复搜索同一批源码和官方文档；
- PR 已经验证过的决定没有进入共享证据，下一位协作者重新争论；
- 交接只剩聊天摘要，blocker、next action 和 return point 丢失。

Project Graph、Work Ledger、Decision Timeline 和 Evidence Matrix 让人和 Agent 看到
当前主线、owner、依赖、证据和影响范围。memory 只能提供候选，不能宣布完成，也不能
授予副作用权限。

## 临时 Idea 很容易把主线带跑

- 你在执行中随口补充一个未来想法，Agent 立即停下当前工作去实现它；
- 实验分支没有 return point、attempt budget 或 promotion gate；
- 你只是讨论方案，模型却把讨论当成批准后的执行指令；
- 用户真正要求切换时，原任务没有 checkpoint，只能从头恢复。

默认动作是 capture-and-continue：先保存 Idea，继续当前 active task。只有明确切换，
并通过 review、范围检查、预算和 promotion gate，Idea 才能进入主线。返回原任务时只
展开相关 checkpoint 和引用，不把整段旁支聊天重新塞进窗口。

## 大型项目里，人和 AI 都不知道“哪里是哪里”

- 几百个模块、几千个任务和跨仓依赖堆在一起，目录树无法表达真实关系；
- AI 找到了同名 symbol，却不知道它是主线、实验、旧实现还是替代路径；
- 人类接手时只能翻 Issue、PR、聊天和报告，很难复盘一个决定为什么产生；
- 修改底层能力后，不知道会影响哪些 Product、Work、Constraint 和测试。

Project Graph 给出确定性的任务地图，Relationship/Impact 用关系图帮助探索影响范围，
Decision Timeline/Evidence Matrix 说明为什么做、何时推翻、证据在哪里。投影 core 已
通过规模测试；完整 Docmost 页面、Obsidian Canvas/Bases 和交互验收按
[图形化产品计划](visual-products.md)推进。

## Memory、Skill 和文档会漂移

- 旧路径、旧决定和旧限制在压缩后被当成当前事实；
- Skill 每次都全文重载，既浪费 token，又可能读到错误版本；
- Skill 路径、digest、依赖或适用范围变化后，没有人发现；
- MASTER/STATUS 要么长期不更新，要么把所有会话叙事都堆进去；
- 外部标准、OS 文档和软件文档被反复全量搜索，却没有 revision 和 freshness。

历史 memory 标记为 candidate。Skill 使用版本、hash、rule ID、适用范围和 quarantine；
MASTER 保存治理主线，STATUS 保存当前路由，报告和投影视图没有权威状态提交权。当前
Skill source bytes 下降 `96.54%`，但长期 token 和窗口利用率仍需 host trace 才能测量。

## 质量不能让位给速度

- 为了少读一点，Agent 删掉了承重证据或必要约束；
- 为了快一点，跳过 affected build/test、故障注入或 live 验证；
- 模型声称“完成”，却没有测试、artifact hash 或 current provenance；
- crash、503 或 checkpoint 损坏后恢复失败，却继续执行副作用。

当前 E0-E9 `10/10`，compaction、Idea、interrupt 和 worker-loss fault `4/4`；stale
history revival、静默 CAS 覆盖和 authority violation 均为 `0`。完整数字、方法和限制
见 [实测方法](benchmarks.md)。
