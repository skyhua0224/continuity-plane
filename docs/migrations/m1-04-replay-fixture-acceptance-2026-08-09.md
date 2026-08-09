# M1-04 Replay Fixture Acceptance

版本：1  
日期：2026-08-09  
状态：✅（40 个真实来源最小 fixture 已通过 admission 与独立复验）

## 范围

本验收覆盖三个受控 Codex/Claude archive 的有界 candidate inventory、连续 fragment extraction、sanitizer、opaque provenance、current-evidence verification、fixture admission 和 content-free receipt。原始 archive、provider 原始 thread ID、本机路径和 source namespace key material 未进入 Git。

## Corpus

| 字段 | 结果 |
|---|---|
| fixture 数 | 40 |
| 受控来源数 | 3：Codex 1、Claude 2 |
| corpus SHA-256 | `1746514721b768a499757eb4738c8f9e9e6c50173a8d9cec8f98e2729e700990` |
| public source registry | `profiles/alkaidlab/source-registry.json`；只含 `thr_...` 与公开 key ID |
| validation manifest | `replay/fixtures/validation-receipts.json` |
| sanitizer | `m1-03.v1`；40/40 clean |
| independent validator | `m1-04-independent/v1alpha1`；40/40 passed |
| current evidence | 40/40 至少绑定一个带内容 hash 的 `artifact://sha256/...` section ref |

每个 fixture 包含最小 input event、initial/expected typed state、allow/veto 期望、archive/range/source-fragment/content hash、opaque thread/range ref、sanitization proof 和 current evidence ref。manifest 的 40 个 receipt 从磁盘重新读取 fixture 后逐例重建，fixture hash、receipt hash 和 corpus hash 可重复。

## 覆盖

Corpus 覆盖 compaction recovery、task routing、stale decision、evidence gap、scope drift 与 experiment promotion，包括 N-42/N-67 blocker、N-68 rollback、CL queue authorization、LocalSend promotion、单次 ping 推断、pacer 比较、final SRTT、10 Gbps 归因和实验性 Product measurement path。

## 验证

```text
python3 -m unittest tests.test_m1_04_archive_extraction -v
python3 -m unittest tests.test_m1_04_claude_archive_extraction -v
python3 -m unittest tests.test_m1_04_fragment_extraction -v
python3 -m unittest tests.test_m1_04_replay_fixture -v
python3 -m unittest tests.test_m1_04_fixture_batch -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q context_control_plane experiments tests
```

当前结果：M1-04 定向测试 26/26 通过；仓库全量测试 88/88 通过；40/40 fixture 独立复验通过；Python compile、JSON/YAML parse 和 raw transcript admission checks 通过。

## 边界

本验收证明首批真实、脱敏 replay corpus 的准入完整性，不证明 provider live tokenizer、模型输出质量、State MCP runtime、并发 CAS、SIGKILL/503 恢复或生产 archive backend。这些门分别由 M1-05、M2、M5、M7、M8 和 E0-E9 后续实验完成。
