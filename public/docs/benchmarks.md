# 实测方法

[English](benchmarks.en.md)

`benchmarks/reference-results.json` 保存脱敏的 aggregate observation，不包含
prompt、repository path、thread ID、provider credential 或私有项目名。

真实工作对比使用匹配任务、相同模型配置和精确 expected-answer oracle，每个臂
运行 3 次。质量指标要求所有承重事实存在，且没有 forbidden assertion。

指标定义：

- input reduction = `(baseline - candidate) / baseline`；
- tool-call reduction 使用每次运行完成的工具调用数；
- wall time 是 dispatch 到最终结果的 elapsed time；
- source-byte reduction 对比 applicable Skill source 和选中的 compiled packet；
- collaboration duplication 统计多个 worker 的重复 discovery 调用。

## 用户 token、窗口利用率与一致性

场景级 input token 降幅只描述该场景。用户长期节省量按 accepted Work 归一化，
并分开记录 input、output、cached input 和 provider 可见的 reasoning token：

- `billable tokens per accepted Work`：同 provider、model、task class 和窗口配置下，
  完成且通过验证门的 Work 平均消耗；
- `useful context utilization`：当前任务权威状态、当前 evidence、选中 Skill、必要
  tool result 和新增交互 token 占 provider 已用窗口的比例；
- `active time to compaction`：PostCompact canary 通过到下一次 PreCompact 之间的
  活跃执行时间，不包含用户空闲和显式 blocker；
- `accepted Work per compaction`：每次可见压缩之间通过完成门的 Work 或 durable
  artifact 数；
- `continuity consistency`：active leaf、最新决定、约束、首动作、acknowledged input、
  effect watermark 和 CAS 冲突的独立正确率与错误计数。

当前公开数据支持冗余历史、近上限历史、Skill、代码检索和协作场景的独立结论。
它还不能给出所有用户的统一 token 节省率，也不能证明真实会话达到下一次压缩所需
的时间已经延长。provider window 使用量、compaction signal 和 accepted Work 必须来自
同一 trace；无法导出时结果保持 `unavailable`。任何一致性 veto 失败都会否决节省声明。

纵向结论至少需要每个实验臂 3 段匹配真实会话，并报告任务类别、窗口大小、缓存
语义、定价快照、空闲时间排除方式和完整 consistency vector。

## 压缩与恢复

| 对比 | Baseline | Continuity Plane | 结果 |
|---|---:|---:|---:|
| 冗余历史恢复 | `32,857` input tokens | `19,633` | `-40.25%` |
| 接近上限的历史恢复 | `397,631` input tokens | `19,633` | `-95.06%` |
| expected-answer quality | `3/3` | `3/3` | 无退化 |

1M provider A/B 使用 `3+3+3` 次匹配运行；700K compaction 从 `783,628` 压缩到
`24,776`，下一 packet 为 `59,172`，恢复率 `100%`。900K upstream compact 失败，
因此没有把它写成成功能力。窗口有效利用率和两次压缩之间的工作量仍等待 M10-11
纵向 trace。

## 代码检索

| 指标 | 无有界检索 | Bounded retrieval | 改善 |
|---|---:|---:|---:|
| input tokens | `146,987.67` | `73,471.33` | `-50.02%` |
| tool calls | `12.67` | `5.33` | `-57.89%` |
| wall time | `25.54 s` | `18.54 s` | `-27.41%` |
| quality | `3/3` | `3/3` | 无退化 |

## Skill 装载

全量 Skill source `501,543 B`，选中 compiled packet `17,349 B`，source bytes
下降 `96.54%`；expected-answer quality `3/3`。provider input 的长期变化必须使用
host token trace，不能从 source bytes 直接推导。

## 多 Session 协作

| 指标 | 未协调 | Continuity Plane | 改善 |
|---|---:|---:|---:|
| worker tool calls | `14.00` | `7.33` | `-47.62%` |
| duplicate tool calls | `11.33` | `5.00` | `-55.88%` |
| parallel wall time | `24.58 s` | `19.01 s` | `-22.65%` |

独立 verifier 使协作 input token 增加 `10.75%`。这是质量成本，不从平均降幅中隐藏。
是否值得付出该成本按 task class、verification profile 和 accepted Work 评估。

## 一致性与限制

| 门 | 结果 |
|---|---:|
| compaction / Idea / interrupt / worker loss fault | `4/4` |
| E0-E9 campaign | `10/10` |
| dual-Session same-revision delivery | `1000/1000` |
| duplicate-notification suppression | `1000/1000` |
| offline catch-up | `2000/2000` |
| authority violations | `0` |

这些结果来自单机、脱敏 fixture 和匹配 task class。它们没有证明跨模型、跨设备的
总体 token 节省，也没有证明完整 Docmost UI 已部署。任何 host metric 不可见、
provider trace 不完整或一致性 veto 失败的运行，都不能产生总体节省声明。

公开 synthetic benchmark：

```bash
python benchmarks/run_local_state.py --iterations 1000
```

该命令创建临时项目，执行 1,000 次经过验证的 SQLite authority read，报告
median、p95、成功数、Python 版本、操作系统类别和机器架构，不使用网络或外部
服务。

aggregate receipt 包含 content digest 和限制说明。原始 prompt、仓库名、文件
路径、thread identifier 和 provider metadata 不进入公开 benchmark artifact。
