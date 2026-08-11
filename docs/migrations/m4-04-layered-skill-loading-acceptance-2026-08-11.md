# M4-04 Layered Skill Loading Acceptance

版本：1  
日期：2026-08-11  
状态：accepted deterministic loader and composition-byte proxy

```yaml
document_id: context.m4-04-layered-skill-loading-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-benchmark-and-independent-review-2026-08-11
supersedes: null
affected_tasks: [M4-04, M4-05, M4-09, M5-01, M5-03, M5-05]
next_review: M4-05
```

## 范围

M4-04 注册 `context.layered-skill-load-plan/v1alpha1` 与 `context.layered-skill-load/v1alpha1`。load plan 绑定 M4-02 packet digest、manifest-set digest、受信任时间、完整 Skill-to-layer 映射和请求层；计划编译只生成候选，激活必须通过 `LayeredSkillPlanAuthorizer`。加载 receipt 记录 plan digest 和 opaque authorization ref。

加载器只接受 M4-03 已验证的 `bytes`、`allow` drift assessment 和已授权 plan。packet、assessment 与 plan 在资产访问前 canonical snapshot；资产按 compiled packet 顺序读取并重新计算 SHA-256。S0 为必选 bootstrap 层；返回的正文映射、receipt 和内部 canonical bytes 不允许调用方修改权威副本。

## 验证

| 门 | 结果 |
|---|---|
| strict wire/schema | 两个 Draft 2020-12 strict schema 已注册；load-plan schema hash `ebaa6b091ab885bab13cd1b702c2f4d8cce99863e4a55081f272476497982020`；load receipt schema hash `a7e840bad03efdfd38c5cc54891fc14dc59e6e3a0fc3f3e2476e8cdbb8b6c970` |
| plan authority | 候选 plan 与激活授权分离；authorizer 对 canonical plan、packet 和 manifest-set digest 判定；拒绝未授权 relabel、缺失 authorizer 和非法 authorization ref |
| drift and asset gates | 非 `allow` assessment、packet/manifest/time 漂移和非法 S0/S1/S2/S3 plan 在资产访问前 fail-closed；缺失或额外 asset、digest 不一致、直接路径和非 bytes 在确定性资产 snapshot/校验阶段 fail-closed |
| deterministic fixture | plan fixture SHA-256 `61215ac6bf8eb0ae79c6e53c72dcfa5c8ade7dcf0c33c10576f66b8788044298`；load fixture SHA-256 `2fc1b70322499745a13c8f4210fcb1831c12978897c2546a8501b52239c42cf5`；canonical round-trip 与当前实现输出一致 |
| bounded composition | 固定 corpus 共 25,600 bytes；S0 1,024 bytes、S2 4,096 bytes；S0+S2 向 composition 转发 5,120 bytes，省略 20,480 bytes，静态正文选择 proxy 下降 `80%` |
| repeated workload | 40 次 all-layer 基线为 1,024,000 selected bytes；分层路径为 204,800 selected bytes；避免向 composition 重复转发 819,200 bytes |
| loader latency | 当前 Linux x86_64、CPython 3.13.14、40 样本、1 次 warmup；p50 `0.5735 ms`、p95 `0.5887 ms`、max `0.5967 ms`；p95 `<10 ms` 门通过 |
| determinism/control | 40/40 receipt digest 相同；all-layer control 加载 25,600/25,600 bytes；外部服务 0 |
| 定向测试 | M4-04 plan/loader/benchmark 35/35 通过 |
| M4 组合回归 | M4-01/M4-02/M4-03/M4-04 111/111 通过 |
| repository gate | 全库 511 tests 通过；28 个无 DSN PostgreSQL live tests skipped；Python compile、repository verifier 和 `git diff --check` 通过 |
| independent review | High 0；Medium 0；M4-02 wire immutability、plan authorization、schema/runtime parity、TOCTOU snapshot 和 benchmark provenance 已复核 |

原始 40 个 latency 样本、每次 receipt digest、运行参数、环境、实现/fixture/schema/test hash 和限制位于 [`m4-04-layered-skill-loading-results.yaml`](../../experiments/state/m4-04-layered-skill-loading-results.yaml)。validator 会重新计算 percentile、bytes、digest、fixture 输出和所有 provenance；stale receipt 不通过验收。

## 权限与后续边界

M4-04 authorizer 是 host-owned 受信任 adapter 边界。当前 benchmark 使用 synthetic fixed-digest allow-list 和 frozen clock，只证明授权门被执行；未证明 State MCP actor、revision、expiry 或 project policy 集成。该集成由 M4-09/M8 验收。

本次数据衡量 in-memory validation/selection latency 和向 composition 转发的 Skill 正文字节。加载器仍校验内存中的全部 25,600 bytes，因此没有 filesystem/source read reduction 证据。结果不表示 provider input/billable token、prompt cache、Codex/Claude context-window、真实压缩质量、Skill 规则遵循、跨 provider replay 或跨设备性能改善。

M4-05 负责至少两种 provider adapter 的同合同 replay；M5 负责 Execution Packet、PostCompact canary、provider token/cache accounting 和真实压缩 A/B。任何 live 质量、token 或上下文改善声明必须等待对应 evidence gate。
