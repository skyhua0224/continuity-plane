# E0/E1 Context Compression Benchmark

版本：1  
日期：2026-08-09  
状态：🧑‍💻（合成 canary 已完成；provider live A/B 和真实 fixture 待后续实验）

## 目的与边界

本实验用四个脱敏合成场景比较两种输入组合：

- `E0 baseline`：当前状态、历史事件和全部 Skill 顺序拼接，再用尾部保留模型模拟压缩。
- `E1 execution packet`：从结构化当前状态组装 active task、latest decision、constraint、return point、blocker、next action 和相关 Skill rule。

实验不读取原始 Codex/Claude transcript，不调用模型，也不代表任何 provider 的真实 tokenizer。`token proxy = ceil(UTF-8 bytes / 4)`；该指标用于本地相对比较，账单级数据由后续 E0/E3 live profile 补充。

运行命令：

```text
python3 experiments/run_e0_e1_context_benchmark.py --budgets-chars 256 512 768 1024
```

## 结果

| 压缩预算（字符） | 场景数 | E0 关键状态恢复率 | E1 关键状态恢复率 | E0 旧决定复活 | E1 旧决定复活 | Skill 输入下降 | token proxy 下降 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 256 | 4 | 0% | 0% | 4 | 0 | 75.0% | 0.0% |
| 512 | 4 | 0% | 70.84% | 4 | 0 | 75.0% | -0.1953% |
| 768 | 4 | 0% | 100% | 4 | 0 | 75.0% | 20.7031% |
| 1024 | 4 | 0% | 100% | 4 | 0 | 75.0% | 40.5273% |

## 结论

1. 结构化 Execution Packet 在 768 字符以上的本地 canary 预算下完整恢复六个关键字段；尾部截断的 E0 baseline 在四个预算均未恢复完整当前状态。
2. E0 在所有预算均保留了已拒绝决定而丢失最新决定，产生 4/4 旧决定复活；E1 在同一模型下为 0/4。
3. 相关 Skill 选择将 8 条规则压缩到 2 条，Skill 输入下降 75%。
4. 512 字符仍不足以承载当前合成 packet；这构成 packet capacity/canary 门，不能用 token 降幅掩盖恢复失败。

## 验收状态与后续

本结果支持 E0/E1 的离线 harness 和相对指标，不构成生产准入。后续必须：

- 用 M1-04 的真实、脱敏、带 provenance 的 replay fixtures 重跑相同 harness；
- 在至少两个 provider 上使用同一预算、同一任务和同一 Verification Profile 做 A/B；
- 记录真实 tokenizer、输入 token、恢复 p95、规则遵循、build/test/mutation 和返工率；
- E1 的 veto 门仍为关键字段恢复 100%、旧决定复活 0；任一失败都否决优化。
