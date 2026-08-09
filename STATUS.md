# Context Control Plane Status

版本：revision 20  
日期：2026-08-10  
canonical plan：`MASTER.md`

## 当前状态

| 字段 | 值 |
|---|---|
| 当前 Campaign | M2 Typed State、Event 与 Checkpoint |
| active work | M2-08：backend-neutral StateStore SPI 与 capability manifest（🟡） |
| next action | 先写未声明 capability 的 adapter 被拒绝、PostgreSQL adapter 通过通用 create/read/commit/events conformance 的失败测试 |
| hard blocker | 无；State MCP 与 runtime effect authorization 分属 M2-05/M8 |
| repository mode | research / shadow pilot |
| production state | planned；M2 阶段实施 |

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
| State portability | 默认 `local-embedded`、零新增服务的 `forge-coordinated`、可选 `local-coordinator` 和 `shared-strong` capability profiles 已进入 revision 19；SQLite/SPI/forge adapter 实现待 M2-08/M2-09/M8-08 |
| M1-06 | retention、deterministic export/import、tombstone 和 deletion proof 离线验收完成；production adapter 待 M2/M8/M10 |
| M0-07 | schema registry/hash、semver transition、migration/replay/rollback 和 unknown-version quarantine 离线验收完成 |
| E0/E1 synthetic canary | 4 场景；768 字符时 E1 恢复 100%、旧决定复活 0、Skill 输入下降 75%、token proxy 下降 20.7031% |
| E0/E1 real replay | 40 场景；768 字符时 E1 恢复 100%、旧决定复活 0、Skill 输入下降 74.7903%、token proxy 下降 12.6042%；512 字符 capacity veto |
| External Skill catalog | OpenAI/Anthropic/GitHub/Agent Skills/MCP/Skills.sh metadata-only；active 0 |
| Harness research | OpenAI/Codex、Anthropic/Claude Code/Agent SDK、agent teams/subagents/advisor/worktree reference catalog 与 adoption matrix 已落盘；provider-native state authority 为 0 |
| Idea continuity | capture-and-continue、correction 写保护、checkpoint/switch/context return 架构合同已落盘；typed implementation 待 M2-M5 |
| Adaptive information | bounded retrieval receipt、ProjectAdaptation proposal/shadow/approval/rollback 合同已落盘；实现待 M2/M6/M8/M10 |
| Documentation lifecycle | 文档分类、更新触发、容量预算、supersedes 和投影规则已落盘；第 4 次恢复发现 STATUS/evidence 漂移并登记为 M0-10 validator 反例 |
| Default project projections | MASTER、STATUS、目标态架构全表为人类/Agent 默认安装方案；公开项目只接收三文档、最小 Profile、已采用 policy 和脱敏证据 |
| Git collaboration | branch/commit/PR/merge 和 staged admission 合同已落盘；公开 Gitea remote 为 `skyhua/context-control-plane`，默认分支 `main`；repo-local identity 已与托管账号核验 |
| Continuous integration | 独立 Verifier 权威状态写权限为 0；M2-03 runs 1037/1038 暴露 service lifecycle 与 job-network 故障，run 1039 的两个 jobs 全绿；`main` 禁止 direct/force push、禁止 admin merge override，并要求 4 个 push/PR status contexts |
| Reference catalog | 15 个 Codex/Claude harness 来源；URL、retrieval hash、validity、refresh trigger 和 adoption status 离线校验通过 |
| Project dogfood baseline | 9 次 compaction 的结构字段恢复 100%，已观测 Continuation Cursor 字段累计恢复 10/12；第 7/8 次首动作不匹配和已确认事项重播累计各 2 次，第 9 次精确续接 M2-03 CI 红测，整体趋势仍为 regressed；21 次 input routing 无未授权切换；27 个 Skill body 共 320,845 bytes，其中重复 155,558 bytes；数据库 Skill digest drift 已 quarantine；provider context/token/latency 不可见 |
| Repository verification | 171/171 tests；14 个 repository verifier 正反场景；M2-01 4/4 snapshot 与 M2-02 1/1 replay fixture byte-equivalent；Python compile、JSON/YAML、schema/projection、fixture privacy、documentation link/style、transcript admission 和 Gitea secret-scan checks 通过 |
| Governance authority | `MASTER.md` |
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
| External Skill activation | 需固定 revision/hash、license/provenance、权限检查、审批和 replay；当前 active 0 |
| Reference adoption | 发现器只写 candidate/stale signal；承重 assertion 必须 current provenance 与 validator |
| Platform boundary | Context Control Plane 通过外部 Project Profile 与 API 集成 |
| Repository topology | modular、monolith、mixed 使用同一 core；Foundation Sunshine 当前按 monolith Profile 协作，模块边界不构成准入条件 |
| Runtime profile | 默认 `local-embedded` 无独立数据库/daemon；Git remote 只触发 `forge-coordinated` proposal；PostgreSQL、Temporal、Docmost、OTel 均为 opt-in |
