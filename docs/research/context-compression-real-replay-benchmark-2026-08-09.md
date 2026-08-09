# Context Compression Real Replay Benchmark

版本：1  
日期：2026-08-09  
状态：verified offline benchmark

## 问题与方法

实验使用 M1-04 已 admission 的 40 个真实来源脱敏 fixture，对比 E0 自由历史 tail retention 与 E1 typed Execution Packet。两组使用相同字符预算；恢复指标检查 `active_task`、`latest_decision`、`constraints`、`blocker`、`return_point` 和 `next_action` 六个承重字段，同时记录 rejected decision 复活、Skill 输入与 UTF-8 token proxy。

机器结果位于 `experiments/results/e0-e1-real-replay-2026-08-09.json`，绑定 corpus SHA-256 `1746514721b768a499757eb4738c8f9e9e6c50173a8d9cec8f98e2729e700990`。

## 结果

| 字符预算 | E0 恢复率 | E1 恢复率 | E0 / E1 旧决定复活 | Skill 输入下降 | token proxy 下降 |
|---:|---:|---:|---:|---:|---:|
| 512 | 0% | 66.67% | 40 / 0 | 74.7903% | 0% |
| 768 | 0% | 100% | 40 / 0 | 74.7903% | 12.6042% |
| 1024 | 19.17% | 100% | 40 / 0 | 74.7903% | 40.0688% |
| 1536 | 64.17% | 100% | 22 / 0 | 74.7903% | 60.6433% |
| 2048 | 80% | 100% | 10 / 0 | 74.7903% | 64.7567% |
| 4096 | 100% | 100% | 0 / 0 | 74.7903% | 66.7056% |

512 字符预算未恢复全部承重字段，触发 capacity veto。768 字符是当前 corpus 中 E1 达到 40/40 场景、六个字段全部恢复且旧决定复活为 0 的最小受测预算。4096 字符时 E0 也能完整恢复，但 E1 的输入规模仍下降 66.7056%。

## 可复现性

```text
python3 experiments/run_real_e0_e1_context_benchmark.py \
  --output experiments/results/e0-e1-real-replay-2026-08-09.json
python3 -m unittest tests.test_context_benchmark -v
python3 -m unittest tests.test_m1_04_fixture_batch -v
```

相同 fixture corpus、budget 和实现产生确定性 JSON。benchmark runner 在运行前检查 manifest fixture count；fixture batch test 从磁盘独立复验全部 receipt。

## 限制

当前 compaction 是确定性 tail retention，token 数为 UTF-8 bytes/4 proxy。实验未使用 Codex、Claude 或其他 provider 的 live tokenizer、cache accounting、真实 compaction hook、模型生成、恢复时延或相同模型同预算盲评。结果支持 typed packet 在该 corpus 上的离线恢复与输入量结论，不构成生产 token、成本、时延或跨 provider 优势声明。上述结论需在 M4、M5、M7 和 M8 绑定 provider telemetry 与 A/B replay 后复验。
