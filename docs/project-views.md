# 大型项目视图

[English](project-views.en.md)

大型仓库的核心问题是无法判断当前看到的文件、模块、决定和任务在整个项目中
处于什么位置。Continuity Plane 提供一组只读 projection，
把同一 revision 的状态转换为可以由 Web、CLI、Docmost 或静态 vault 展示的数据。

## Project Graph

Project Graph 展示：

- Campaign、Goal、Work 和 Experiment 层级；
- active work set、primary leaf 和 dependency；
- owner、claim、lease、branch 和 scope ownership；
- blocker、重复候选、过期工作和 return point；
- 验证状态和当前 revision。

它用于回答“当前主线是什么”“哪些工作可以并行”“谁在修改这个范围”“这个分支
应该回到哪里”。128 Work 和 223 x 223 stress path 的 accepted worst-path p95 为
`6.319825 ms`。

## Decision Timeline 与 Evidence Matrix

Decision Timeline 展示决定的时间、状态和 supersedes 链。Evidence Matrix 连接
Work、Decision、Constraint、测试、源码和官方来源，区分 candidate、verified、
stale 和 rejected evidence。

它用于回答“为什么这样做”“这个决定何时被推翻”“完成声明依赖什么证据”。
1,000-sample accepted worst-path p95 为 `47.421031 ms`。

## Context Health 与 Replay

Context Health 汇总 compaction、Skill load、retrieval、reference freshness、token、
误切和恢复指标。Replay 视图展示 checkpoint、event head、continuation cursor 和
effect watermark。

它用于定位“为什么压缩后跑偏”“哪些 Skill 已漂移”“哪次恢复没有通过 canary”。
常规 accepted p95 为 `15.409254 ms`；3,500-event scale p95 为 `253.994472 ms`。

## Relationship / Impact Force-Directed Projection

Relationship/Impact projection 把项目状态、依赖和已验证代码线索输出为节点、边、
cluster、filter、focus 和 impact set。它同时输出 force-directed layout 合同与确定性
seed；坐标由渲染器计算，不进入 State，也没有权威权限。

它用于探索大型项目中难以通过目录树理解的关系簇和变更影响。2,000 nodes / 5,000
edges complete rebuild 为 `25/25`，accepted scale p95 为 `187.459764 ms`。

## Obsidian Vault

只读 vault 把 Project Graph、Decision/Evidence、Context Health 和 Replay 投影为带
签名 manifest 的 Markdown 文件。人工修改、额外文件和内容篡改会被拒绝，不会反向
修改权威状态。

## 当前可用边界

| 能力 | 内部状态 | alpha wheel |
|---|---|---|
| Project Graph / Work Ledger | verified | included |
| Decision Timeline / Evidence Matrix | verified | included |
| Relationship / Impact | verified | included |
| signed Obsidian Markdown vault | verified | included；完整输入由 adapter 提供 |
| external State provider / controlled governance | verified | included |
| Context Health / Replay | verified | 未进入 alpha wheel；provider-neutral canary SPI 待实现 |
| Docmost Web UI 与 connector | preview | 未发布 |
| Obsidian Canvas / Bases dashboard | planned | 未发布 |

完整图形化产品的页面、交互、权限路径和验收门见
[`visual-products.md`](visual-products.md)。任何 UI 都必须通过 State MCP、
authorization、expected revision 和 validator，不能直接修改数据库。
