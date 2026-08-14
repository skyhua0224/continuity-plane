# Context Control Plane Status

版本：revision 36  
日期：2026-08-14  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M4 Skill 控制面 |
| active work | M4-10：官方 Skill、Agent Skills standard、GitHub 和 marketplace catalog adapter（🟡） |
| next action | 先为 official/standard/verified-organization/marketplace-community 四类 source snapshot 写 pinned revision、hash、license、trust tier、quarantine 和 deterministic replay 失败测试 |
| hard blocker | active leaf 无；M3-01 仍等待 M2-09 的 Windows/macOS 原生 live fixture |
| repository mode | research / shadow pilot |
| production state | local-embedded implemented / shadow pilot；shared production pilot planned |

## 已验证状态

| 对象 | 状态 |
|---|---|
| Canonical repository | `context-control-plane` |
| Repository role | Provider-neutral developer control plane |
| Raw transcript import | 0 files |
| Imported research | 上下文可靠性评估、来源规模清单 |
| M1-02/M1-03 | source registry、provenance、sanitizer 和 admission guard 离线验收完成；256-bit base64url secret boundary 与公开 key ID 验证通过 |
| M1-04 extraction | 三个受控 Codex/Claude 来源生成 40 个真实脱敏 fixture；40/40 从磁盘独立复验通过；corpus hash `1746514721b768a499757eb4738c8f9e9e6c50173a8d9cec8f98e2729e700990` |
| M1-05 fault coverage | 16 个 contract fixture 覆盖 E0-E9 10/10 与 required scenarios 10/10；contract coverage 100%；runtime coverage 0%，依赖 M2/M5/M8 |
| M2-01 typed state | 9 类核心对象 strict schema 与 semantic validator 完成；4/4 canonical round-trip fixture、12/12 定向测试、schema registry/hash gate 通过 |
| M2-02 state events | strict Event schema、连续 sequence/revision、SHA-256 chain、supersedes 和 deterministic reducer 完成；1/1 versioned replay fixture byte-equivalent；14/14 定向测试通过；验收见 `docs/migrations/m2-02-state-event-acceptance-2026-08-10.md` |
| M2-03 PostgreSQL backend | optional shared backend 11/11 定向测试；8/8 并发冲突显式、silent overwrite 0；Gitea run 1039 两个 jobs 通过；4 Event shadow 读回与 replay 一致；验收见 `docs/migrations/m2-03-postgresql-state-store-acceptance-2026-08-10.md` |
| M2-08 StateStore SPI | 52/52 capability 与双 adapter conformance 通过；M2-02/M2-03/M2-08 77/77；第三轮独立审查 High 0、Medium 0；验收见 `docs/migrations/m2-08-state-store-spi-acceptance-2026-08-10.md` |
| M2-09 SQLite backend | 48/48 定向测试；Linux WAL/`BEGIN IMMEDIATE`、跨进程 CAS、append-only、SIGKILL、Unicode 路径、WAL/SHM 清理和非 header page 损坏检测通过；commit p95 `0.6500 ms`，restore p95 `0.2678 ms`；Windows/macOS 原生 fixture 待协作者；验收见 `docs/migrations/m2-09-sqlite-state-store-acceptance-2026-08-10.md` |
| M4-01 Skill manifest set | `context.skill-manifest-set/v1alpha1` strict schema、SPDX 3.28.0 snapshot、runtime validator、33/33 定向测试（含 26/26 原始合同）、registry hash 和 fixture canonical round-trip 通过；独立审查 High 0、Medium 0；验收见 `docs/migrations/m4-01-skill-manifest-set-acceptance-2026-08-10.md` |
| M4-02 Compiled Skill packet | `context.compiled-skill-packet/v1alpha1` strict schema、dependency closure、rule binding coverage、source manifest-set digest verification、canonical fixture、registry hash 和 17/17 定向测试通过；static metadata proxy 从 2,967 bytes 降至 811 bytes（72.666%）；provider/live compaction 未宣称；验收见 `docs/migrations/m4-02-compiled-skill-packet-acceptance-2026-08-11.md` |
| M4-03 Skill drift quarantine | `context.skill-drift-assessment/v1alpha1` strict schema、bytes-only resolver boundary、manifest/digest/version/expiry/source-status checks、allow evidence gate、runtime semantic uniqueness、canonical fixture 和 registry hash 通过；33/33 显式负变体拦截（100%）；26/26 定向测试；验收见 `docs/migrations/m4-03-skill-drift-quarantine-acceptance-2026-08-11.md` |
| M4-04 layered Skill loading | `context.layered-skill-load-plan/v1alpha1` 与 load receipt strict schema、host-owned authorizer、M4-03 allow gate、bytes-only deterministic loader 和 canonical fixture 通过；35/35 定向测试；固定 corpus S0+S2 从 25,600 降至 5,120 composition bytes（80% proxy）；40 样本 loader p95 `<10 ms`；provider token/真实压缩未宣称；验收见 `docs/migrations/m4-04-layered-skill-loading-acceptance-2026-08-11.md` |
| M4-05 provider Skill adapter | `context.provider-skill-adapter-effect/v1alpha1` 与 probe strict schema、Codex/Claude 独立 materializer 和官方 Skill filesystem surface 通过；24/24 定向测试；40 组 byte-distinct compositions 产生 80/80 validated effects 与 240 次 compose；neutral mismatch `0/40`、provider replay mismatch `0/80`、state-write declaration `0/80`；Codex p95 `0.7937 ms`、Claude p95 `0.7750 ms`；High 0/Medium 0；provider process/token/cache/规则遵循/真实压缩未测；验收见 `docs/migrations/m4-05-provider-skill-adapter-acceptance-2026-08-11.md` |
| M4-06 Skill compatibility | lock/decision/migration/probe strict schema、selected manifest digest、exact provider applicability/contract-to-surface binding、live input/adapter identity 重算、evidence verifier、canonical replay/rollback、gated composition entrypoint 和 40 样本 fault matrix 通过；unselected metadata `8/8 compatible`，selected identity/provider contract `32/32 migration_required`，unauthorized gated composition `0/32`，synthetic verifier-authorized migration `4/4`，missing verifier/evidence/wrong binding allowed `0/4`，replay/rollback mismatch `0/4`，assessment p95 `0.8320 ms`；M4-05 materializer 与 provider process/network/State commit 均无外部副作用；验收见 `docs/migrations/m4-06-skill-compatibility-acceptance-2026-08-11.md` |
| M4-07 Skill catalog | `context.skill-catalog/v1alpha1` strict schema、registry hash、manifest/source/license/provenance/approval/verification/permission binding、五类来源 `5/5` 合法 entry、18/18 admission/quarantine 负变体、candidate projection、canonical manifest digest 的 M4-06 exact identity binding `1/1`、manifest metadata/content/status drift rejection 和 canonical fixture `1/1` 通过；18/18 定向测试；固定 fixture 200 次 validator p50 `0.0107 ms`、p95 `0.0140 ms`、max `0.1231 ms`；验收见 `docs/migrations/m4-07-skill-catalog-acceptance-2026-08-11.md` |
| M4-08 Skill proposal | `context.skill-proposal/v1alpha1` strict schema、M4-01 manifest schema reuse、64 KiB preflight/canonical input、16,384 Unicode scalar/64 KiB UTF-8 content 与 128 KiB output bound、strict SemVer/ID/timestamp gate、set-like input canonicalization、Verification Profile license policy、Project/User provenance isolation、expected-time-bound replay verifier、candidate-only body asset、registry hash 和 live-regenerated fixture 通过；34/34 定向测试；200 次 replay mismatch `0`，40/40 project-fact 变体 fingerprint/content digest 唯一，权限真值 `0`；M4-03 resolver `allow`；generate+validate+canonicalize p95 `<10 ms` 持续测试；production Verification Profile adapter 待 M7-03；验收见 `docs/migrations/m4-08-skill-proposal-acceptance-2026-08-11.md` |
| M2-04 artifact store | 20/20 定向测试；streamed SHA-256、atomic publication、并发去重、bounded range、损坏/symlink typed error 和 64 MiB benchmark 输入门通过；1 MiB 到 8 KiB context output bytes 下降 `99.2188%`；全库 303 tests、27 PostgreSQL skips；独立审查 High 0、Medium 0 |
| M2-05 State MCP | 26/26 contract/auth tests；默认拒绝、trusted actor、revision/CAS、validator、concurrent request idempotency、strict receipt、PostgreSQL busy normalization、claim/effect provenance、malformed intent 和 torn-read detection 通过；四工具 SQLite/PostgreSQL live parity 1/1；验收见 `docs/migrations/m2-05-state-mcp-acceptance-2026-08-10.md` |
| M2-06 checkpoint canary | 9/9 contract tests、8/8 benchmark/receipt tests；snapshot/manifest 双层 CAS、18 字段 deterministic restore、missing/tampered/stale/unknown/oversized fail-closed 和 SQLite 零服务链路通过；原 40 样本 receipt 字节保持不变，current provenance 复验使用独立 receipt；验收见 `docs/migrations/m2-06-checkpoint-canary-acceptance-2026-08-10.md` |
| M2-07 project governance profile | `context.project-governance-profile/v1alpha1` strict schema、runtime validator 和 versioned fixture 完成；35/35 定向测试覆盖 obligation 终态依据、requested runtime profile/manifest 权限边界、配置轴、topology、typed applicability、SemVer 2.0.0、trusted time、proposal/activation revision、active snapshot replay、rollback 和 approval；两名独立 Verifier 复审 High 0、Medium 0；验收见 `docs/migrations/m2-07-project-governance-profile-acceptance-2026-08-10.md` |
| State portability | Project Profile 只声明 requested runtime profile；运行保证只来自已验证 StateStore capability manifest，不代表用户等级；统一产品默认 `local-embedded`，任何单人或团队均可请求 `forge-coordinated`、`local-coordinator` 或 `shared-strong`；StateStore SPI、SQLite Linux backend 和本地 artifact store 已验收，forge adapter 待 M8-08 |
| Self-dogfood release order | Context Control Plane 自身是 M10-00 首个完整产品试点；AlkaidLab、Foundation Sunshine 和 ProjectCompute 等外部试点在其后执行 |
| M1-06 | retention、deterministic export/import、tombstone 和 deletion proof 离线验收完成；production adapter 待 M2/M8/M10 |
| M0-07 | schema registry/hash、semver transition、migration/replay/rollback 和 unknown-version quarantine 离线验收完成 |
| E0/E1 synthetic canary | 4 场景；768 字符时 E1 恢复 100%、旧决定复活 0、Skill 输入下降 75%、token proxy 下降 20.7031% |
| E0/E1 real replay | 40 场景；768 字符时 E1 恢复 100%、旧决定复活 0、Skill 输入下降 74.7903%、token proxy 下降 12.6042%；512 字符 capacity veto |
| External Skill catalog | OpenAI/Anthropic/GitHub/Agent Skills/MCP/Skills.sh metadata-only；active 0 |
| Harness research | OpenAI/Codex、Anthropic/Claude Code/Agent SDK、DeepSeek Harness、Pi compaction/durable AgentHarness 与 Cordis reference catalog、adoption matrix 和 M4/M5/M8 完成门已落盘；DeepSeek/Pi/Cordis active adoption 0，provider-native state authority 为 0 |
| Idea continuity | capture-and-continue、correction 写保护、checkpoint/switch/context return 架构合同已落盘；typed implementation 待 M2-M5 |
| Adaptive information | bounded retrieval receipt、ProjectAdaptation proposal/shadow/approval/rollback 合同已落盘；实现待 M2/M6/M8/M10 |
| Documentation lifecycle | 文档分类、更新触发、容量预算、supersedes 和投影规则已落盘；第 4 次恢复发现 STATUS/evidence 漂移并登记为 M0-10 validator 反例 |
| Default project projections | MASTER、STATUS、目标态架构全表为人类/Agent 默认安装方案；公开项目只接收三文档、最小 Profile、已采用 policy 和脱敏证据 |
| Git collaboration | branch/commit/PR/merge 和 staged admission 合同已落盘；公开 Gitea remote 为 `skyhua/context-control-plane`，默认分支 `main`；repo-local identity 已与托管账号核验 |
| Continuous integration | 独立 Verifier 权威状态写权限为 0；M2-03 runs 1037/1038 暴露 service lifecycle 与 job-network 故障，run 1039 的两个 jobs 全绿；`main` 禁止 direct/force push、禁止 admin merge override，并要求 4 个 push/PR status contexts |
| Reference catalog | 26 个候选来源；Codex/Claude/DeepSeek/Pi harness、Cordis paper 与 Yundi339 Docmost fork 均固定 URL、revision/hash、license、validity、refresh trigger 和 adoption status；active adoption 0 |
| Project dogfood baseline | 27 次 compaction 的结构字段恢复 100%，Continuation Cursor 累计恢复 78/80；首动作不匹配和已确认事项重播累计各 2 次，后续未新增，整体趋势仍为 regressed；25 次 input routing 无未授权切换；92 个 Skill body 共 942,873 bytes，其中重复 763,452 bytes；provider context/token/latency 不可见 |
| Autonomous progression | required/conditional/optional、bounded escalation、next-ready selector、unattended dispatcher 与 multi-Agent claim/lease/handoff 验收合同进入 revision 21；runtime 实现待 M2-M8；当前 provider-host multi-Agent 只计 shadow evidence |
| Repository verification | 612 tests 通过；28 个无 DSN PostgreSQL live tests skipped；repository verifier、Python compile 和 diff check 通过；fixture privacy、documentation link/style、schema registry、transcript admission 和 Gitea secret-scan checks 保持提交门 |
| Governance authority | `MASTER.md` revision 36 |
| Operational router | `STATUS.md` |

## 恢复入口

1. 普通任务先读本文件。
2. 根据 task ID 只展开 `MASTER.md` 对应章节。
3. 聊天迁移先读 `docs/policies/conversation-ingestion.md`。
4. 组件选择和实验结论读 `docs/research/context-reliability-assessment-2026-08-09.md`。
5. 当前代码或官方证据与历史 memory 冲突时，以当前证据为准。
6. 压缩、Skill 选择或 MASTER revision 后按 `context.dogfood-observation/v1alpha1` 追加受控观察事件；仅在 schema/hash 变化或 validator 失败时重读完整 policy。
7. 文档更新、拆分和 supersedes 按 `docs/policies/documentation-lifecycle.md`，恢复时只展开当前任务对应的最小合同。

## 当前控制门

| 门 | 约束 |
|---|---|
| Raw archive access | 大型 rollout 采用流式、区间化读取 |
| Source namespace | key material 只能由 secret manager/受保护文件/进程注入；Git 和公开记录只保留安全 key ID |
| Historical memory | handoff、project memory 和聊天摘要保持 candidate 状态 |
| Idea intake | 默认 capture-and-continue；明确 interrupt 先 checkpoint；未批准 Idea 不改变 active state |
| Fixture admission | sanitizer、provenance、current-evidence verification 和 independent validator 全部通过后开放 |
| External Skill activation | 需固定 revision/hash、license/provenance、权限检查、审批和 replay；外部 active 0；M4-07 catalog validator 已通过离线 admission/quarantine |
| Reference adoption | 发现器只写 candidate/stale signal；承重 assertion 必须 current provenance 与 validator |
| Platform boundary | Context Control Plane 通过外部 Project Profile 与 API 集成 |
| Repository topology | modular、monolith、mixed 使用同一 core；Foundation Sunshine 当前按 monolith Profile 协作，模块边界不构成准入条件 |
| Runtime profile | 默认 `local-embedded` 无独立数据库/daemon；Git remote 只触发 `forge-coordinated` proposal；PostgreSQL、Temporal、Docmost、OTel 均为 opt-in |
