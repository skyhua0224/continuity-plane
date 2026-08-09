# Context Control Plane Target-State Architecture

版本：1  
日期：2026-08-09  
状态：current architecture contract  
governance authority：`MASTER.md` revision 21

## 三文档默认投影

Context Control Plane 安装到个人项目、协作项目或公开大型项目时，默认生成并维护三份人类与 Agent 共读文档：

| 文档 | 权限与内容 | 人类入口 | Agent 入口 |
|---|---|---|---|
| `MASTER.md` | 项目使命、边界、任务 DAG、治理决定和完成门；具有 governance authority | 审批主线、计划和 promotion | 按 active work ID 读取对应任务合同 |
| `STATUS.md` | active leaf、blocker、next action、revision 和最小恢复引用；Typed State 上线后生成 | 快速查看当前执行状态 | 每次恢复的第一个读取对象 |
| `docs/architecture/target-state.md` | 目标组件、权限、依赖方向、当前差距和验证归属 | 检查功能覆盖与架构完整性 | 按当前任务引用的组件行有界展开 |

三份文档使用稳定 ID、revision、authority、状态、evidence ref 和 completion gate 关联。运行时 Typed State 与 Event Log 保存高频动态事实；文档提供离线恢复、人类审阅和跨 Agent 的稳定投影。STATUS 不保存完整事件历史，MASTER 不吸收实验日志，目标态全表不保存当前 Session 叙事。

## 目标态架构全表

下表使用 `context.*` 独立命名空间，归属 Context Control Plane。

| Slot | Module | 权限与功能 | Backend | 交付阶段 |
|---|---|---|---|---|
| `context.state` | `typed-state-store` | Project、Work、Claim、Idea、Decision、Constraint、Evidence、Blocker、Effect 与 Checkpoint 权威机器状态 | StateStore SPI；SQLite embedded 默认，PostgreSQL optional shared backend | M2 |
| `context.events` | `hash-chained-event-log` | 追加式事件、revision、supersedes、replay high watermark | StateStore SPI + artifact manifest | M2 |
| `context.work-coordination` | `shared-work-ledger` | 项目级 active work set、claim/lease、repo/path/symbol/capability/effect ownership、重复工作检测和冲突恢复；支持 modular、monolith 与 mixed topology | local State MCP + Git forge adapter；optional shared service | M2/M3/M8 |
| `context.forge-coordination` | `forge-work-adapter` | 将 Issue、PR、branch、assignee、review 和 CI 映射为共享 Work/claim/evidence projection；声明离线与未发布工作的保证缺口 | GitHub / Gitea / GitLab adapters | M8 |
| `context.task-routing` | `sticky-task-router` | 识别 continue、child、interrupt、switch、correction、discussion 和 blocking decision；选择 next-ready required leaf；副作用受 active/claim/path-owner 一致性门控制 | deterministic rules + bounded classifier | M3 |
| `context.workflow` | `durable-execution` | checkpoint、重试、幂等、lease、unattended required-work loop、multi-Agent fan-out/fan-in 和长流程恢复 | DBOS；Temporal 按需启用 | M8 |
| `context.skill-resolution` | `versioned-skill-registry` | Skill manifest、rule IDs、hash、依赖、冲突、失效和 quarantine | Git + selected StateStore metadata | M4 |
| `context.composition` | `execution-packet-composer` | 组装当前任务、当前 Skill、当前 evidence 与 Continuation Cursor 的有界执行包 | State MCP + artifact store | M5 |
| `context.replay` | `checkpoint-canary-validator` | 压缩、切任务、换模型、崩溃后的确定性恢复门 | deterministic validator | M1/M5 |
| `context.evidence` | `assertion-resolver` | 当前代码、标准、OS/软件官方文档的 version、validity 和 provenance | Git metadata、artifact store、`rg`、LSP、SCIP、RTFM | M6/M7 |
| `context.code-intelligence` | `bounded-code-retrieval` | 精确搜索、受影响图、跨仓线索和 index freshness | `rg`、Zoekt、LSP、SCIP、CodeGraph | M6 |
| `context.recall` | `candidate-memory-provider` | 提供偏好、历史讨论和时间性事实候选；权威提交权限为 0 | Hindsight / Mem0 / Graphiti SPI | M6 |
| `context.information-access` | `bounded-information-plane` | 最小读取范围、artifact range、retrieval receipt 和 freshness | State MCP、artifact store、索引、Recall SPI | M5/M6 |
| `context.adaptation` | `project-adaptation-loop` | 从已验证运行和明确纠正生成可审批、可回滚的 profile candidate | Typed State、OTel、A/B harness | M8/M10 |
| `context.review` | `independent-reviewer` | 冲突检查、阶段 handoff 和承重证据复核；权威提交权限为 0 | 本地或外置模型 | M6/M7 |
| `context.verification` | `continuous-integration-verifier` | push/PR 执行 test、compile、schema、projection、privacy、benchmark 和 secret gates；权威状态写权限为 0 | local verifier + Gitea Actions + Gitleaks | M0/M7/M8 |
| `context.observability` | `context-otel` | token、Skill 装载、检索、恢复、输入路由、Agent dispatch/handoff、误切、返工和质量指标 | OTel Collector + 可替换后端 | M8 |
| `context.presentation` | `docmost-project-graph` | 可选 Project Graph、Decision Timeline、Evidence Matrix、Context Health 和受控审批 | optional Docmost + State MCP provider；Obsidian 只读生成 | M9 |

## Runtime Capability Profiles

Runtime capability profiles are not user editions. Profile 只声明运行时保证、资源需求和失效边界；单人、多人、私有项目和公开项目均可启用任意 profile。

Context Control Plane 作为一个 cohesive monolith 交付，使用统一安装、升级、迁移和卸载入口。State、Event、Checkpoint、Skill、检索、验证、adapter 与可选控制台属于同一产品的组合能力；启用 PostgreSQL、Docmost、Temporal 或 OTel 不会形成独立产品或用户等级。

| Profile | 默认状态 | 状态与协作位置 | 新增用户管理服务 | 一致性与权限边界 |
|---|---|---|---:|---|
| `local-embedded` | 默认 | 本机 SQLite + 本地 artifact store | 0 | 单机 revision/CAS；本机多 Agent 经同一 local State MCP；无跨设备唯一 claim |
| `forge-coordinated` | 检测到 remote 后 proposal | 每人本机 SQLite；GitHub/Gitea/GitLab 提供共享 Work projection | 0 | 对已发布 Issue/PR/branch/assignee 状态执行 expected revision/ref 检查；offline 与 unpublished Work 只在同步后显式冲突 |
| `local-coordinator` | opt-in | 一台成员设备运行 State MCP + SQLite，其他客户端连接 | 1 个控制面进程，数据库服务 0 | coordinator 可达期间提供共享 CAS/claim；失联客户端降级为 candidate/read-only |
| `shared-strong` | opt-in | State MCP + PostgreSQL；Temporal、Docmost、OTel 按需启用 | 由运行者管理 | 多 writer transaction/CAS、lease、tenant、audit 和长期 workflow |

Profile capability manifest 必须声明 `shared_authority`、`offline_write`、`unique_claim`、`multi_writer`、`lease_clock`、`artifact_scope`、`expected_revision`、`migration_source` 和 `migration_target`。调用方只能使用 manifest 明确提供的保证。安装器默认选择 `local-embedded`；发现 Git remote 只生成 `forge-coordinated` proposal，不静默上传本地 checkpoint、Skill、个人偏好或历史记忆。

运行成本、SQLite transaction 边界、forge CAS 候选和跨平台验收见 [`state-store-portability-assessment-2026-08-10.md`](../research/state-store-portability-assessment-2026-08-10.md)。

依赖方向固定为：

```mermaid
flowchart TD
    Agents["Codex / Claude / Other Agents / Collaborators"] --> Control["Context Control Plane"]
    Control --> Local["SQLite / Local Event Log / Artifact Store"]
    Control --> Forge["Existing GitHub / Gitea / GitLab"]
    Docmost["Optional Docmost Console"] -. opt-in .-> Control
    Control -. opt-in .-> Shared["PostgreSQL / Temporal / OTel"]
    Control --> Retrieval["Code / Official Docs / Standards"]
    Control --> Projects["Personal / Collaborative / Public Projects"]
    Recall["Memory Providers"] -. candidates only .-> Control
    Projects -. provider adapter and Project Profile .-> Control
```

项目级与个人工作边界见 [`project-work-governance.md`](project-work-governance.md)。Skill 编排见 [`skill-orchestration.md`](skill-orchestration.md)。Agent harness 见 [`../research/agent-harness-assessment-2026-08-09.md`](../research/agent-harness-assessment-2026-08-09.md)。`context.skill-resolution` 只负责版本化规则资产的发现、适用性判定、冲突处理和有界装载；Task、owner、claim、revision、checkpoint 和当前 evidence 由 Typed State、Event Log 和 State MCP 管理。

Project Profile 分别声明 direction state、governance owner mode、execution worker mode、repository topology、task sources 和 runtime capability profile。模块化、非模块化与混合仓库使用同一 Work/Claim/Event core schema，只切换 scope resolver。Foundation Sunshine 在 Platform 完工前按 monolith profile 协作；未来模块化 Platform 与收敛后的 Product 继续使用同一控制面协议。

## 公开项目 Markdown 准入

默认安装在目标项目 Git 中提交三文档投影和最小 Project Profile。项目明确采用的 policy、ADR、API 合同、sanitized fixture 和验收报告可以进入该项目；每个文件必须具有项目归属、license、provenance、更新触发和容量边界。

以下内容保留在控制面状态服务、受控 provider archive 或 artifact store：原始 transcript、模型 reasoning、用户私有习惯、完整 event/checkpoint 流、大日志、私有路径、密钥、未脱敏 evidence，以及与目标项目无直接采用关系的控制面研究库。本控制面仓库自身的 protocol、research、sanitized fixture 和 acceptance 文档属于产品交付与可复核证据，可以进入其公开 Git。

## 读取与完整性门

Agent 恢复顺序为 `STATUS -> MASTER active leaf -> target-state referenced rows -> evidence/artifact range`。完整 MASTER 和完整目标态全表只在治理审计、架构审计或 validator 失败时展开。

默认投影必须满足：

- STATUS active leaf 在 MASTER 中存在且状态一致；
- MASTER task 的组件引用在目标态全表中存在；
- 每个承重完成声明具有 current evidence 和 Verification Profile；
- 三文档 revision/digest 可关联，stale projection 在写操作前被 validator 拒绝；
- 三文档拆分后关键恢复字段保持 100%，直接读取 bytes 和重复检索量按 E5/M6-07 计量；
- 公开项目的默认 Markdown 集不包含原始会话、私密身份、密钥或未脱敏运行数据。
