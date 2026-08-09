# Context Control Plane MASTER

版本：revision 13  
日期：2026-08-09  
状态：研究与 shadow pilot 准备阶段  
适用范围：Codex、Claude、Cursor、外置模型、本地模型及未来 provider；AlkaidLab 与其他长期软件项目；单人、子 Agent 和多人协作

## 0. 项目治理状态

| 属性 | 定义 |
|---|---|
| 仓库职责 | Provider-neutral 开发协作上下文控制面 |
| 部署形态 | 独立服务与工具链 |
| 项目集成 | Project Profile、State MCP、CLI 和受控检索接口 |
| 当前阶段 | 研究与 shadow pilot 准备 |
| Canonical 治理计划 | `MASTER.md` |
| 日常恢复入口 | `STATUS.md` |
| 动态执行状态 | Phase M2 完成后由 revisioned Typed State 管理 |
| 原始会话档案 | 受控归档；规模与迁移状态记录于 `docs/migrations/` |

状态只使用：

| 状态 | 含义 |
|---|---|
| `⏳` | 未开始或等待前置条件 |
| `🟡` | 正在执行，必须有当前原子步骤和退出条件 |
| `✅` | 代码、测试、证据及所需实测全部完成 |
| `🧑‍💻` | 实现、离线测试、TDD、编译与守卫完成，只缺用户或真实环境 live 验证 |

## 1. 总目标

建立一套可移植、provider-neutral、跨项目、支持多人协作的上下文控制面。压缩、任务切换、模型切换、协作者交接、进程崩溃和检索后端故障均采用同一恢复合同：active task、最新决定、约束、阻塞、return point、已执行副作用和验证缺口由 checkpoint 与确定性 validator 恢复。

最终系统必须同时做到：

- 权威状态由 Typed State、Event Log 和 revision/CAS 管理；
- 重复输入和重复检索 token 达到量化降幅，代码质量与规则遵循保持基线或提升；
- 通过有界信息访问、artifact range、代码/参考索引和候选 recall 减少直接读取量，同时保留承重 provenance 与 current-evidence verification；
- 历史记忆提供候选召回，执行权限由 validity、supersedes 和当前证据决定；
- 行业标准、OS 官方文档、软件官方文档和当前源码都能形成带版本与 provenance 的 assertion；
- 外部参考和 harness 实践通过可刷新 catalog、snapshot hash、validity 与 adoption decision 持续进入研究证据；
- MASTER 分支任务形成可计算 DAG，具备依赖、阻塞、回流、promotion、attempt budget 和完成证据；
- Docmost 提供持续的人类观察、审批、任务图、证据矩阵和上下文健康度；
- 同一套服务能够接入 AlkaidLab、ProjectCompute 以及未来任意项目和协作者。

## 2. 范围与权限边界

| 范围 | 合同 |
|---|---|
| Git 内容 | 保存协议、schema、代码、项目 Profile、脱敏 fixture 和治理文档 |
| 原始会话 | 保存在供应商档案或加密对象存储 |
| Platform 集成 | 通过 Project Profile 与外部接口完成；Platform Runtime、SDK、Core 和 Module 保持零依赖 |
| 权威状态提交 | 仅 State MCP 在 revision/CAS、权限与 validator 通过后执行 |
| Recall 与检索组件 | Mem0、Hindsight、Graphiti、CodeGraph、RTFM、Obsidian、Docmost 和模型提供候选或投影 |
| 承重字段 | Task State、Decision、Constraint、External Effect 和 Verification Gate 采用无损存储 |
| 组件采用 | 依据源码审查、可重复测试、故障注入和真实项目 A/B |
| 验证完整性 | TDD、编译、contract、golden、mutation、loopback、弱网、性能和 live profile 按项目要求执行 |

## 3. 目标态架构全表

`MASTER.md`、`STATUS.md` 和 [`docs/architecture/target-state.md`](docs/architecture/target-state.md) 构成默认安装到个人、协作和公开项目的人类/Agent 双读投影。MASTER 保存治理主线与完成门，STATUS 保存最小运行路由，目标态架构全表保存组件、权限、依赖和交付阶段。三者通过 revision、stable ID、evidence ref 和 validator 关联；高频事件与私有状态不复制进入 Git 文档。

完整 `context.*` Slot/Module/Backend 表、依赖图、公开项目 Markdown 准入范围和有界读取顺序由目标态架构全表维护。日常恢复先读 STATUS，再按 active leaf 展开 MASTER 和全表对应行；完整文件只用于治理或架构审计。

## 4. 权威状态、记忆和文档边界

| 对象 | 权限 | 保存位置 | 恢复规则 |
|---|---|---|---|
| MASTER | 人类主线意图和治理计划 | Git | 变更经过治理流程 |
| Typed State | 当前任务、依赖、决定、门禁和 owner | PostgreSQL | expected revision/CAS |
| Event Log | 所有状态变化和 supersedes 历史 | PostgreSQL | append-only + hash chain |
| Checkpoint | 压缩、切换、交接的不可变恢复点 | PostgreSQL + artifact store | checksum + canary |
| Evidence/Assertion | 当前代码与权威来源的承重证据 | Git/RTFM/artifact store | authority、version、hash、validity |
| ReferenceSource/Snapshot | 外部来源、检索 revision/hash、freshness 和采用决定 | Git metadata + artifact store | change detection + supersedes review |
| Skill | 稳定协议、方法、词汇、门禁 | Git + compiled packet | version、hash、rule IDs、quarantine |
| Raw Transcript | 供应商原始对话档案 | 供应商目录或加密对象存储 | 按 retention 与受控提取策略访问 |
| Handoff/Memory | 历史候选和快速索引 | 派生存储 | background；current-evidence verification 后晋升 |
| Replay Fixture | 脱敏、最小化、带期望结果的故障样本 | Git | 每次模型/Skill/schema/provider 变更复跑 |
| Docmost | 人类控制台、审批、纠偏、promotion 和审计入口 | Docmost DB + State MCP provider | 受 authorization、revision/CAS、validator 和 promotion gate 约束 |
| Obsidian | 生成的只读 vault | generated vault | 无权威状态提交权 |
| 报告与目标态架构全表 | 治理与汇报投影 | Git/Docmost | 承重断言具有 current provenance；不能自行改变 active state |

文档分类、更新触发、容量边界、拆分、supersedes 和生成投影规则见 [`docs/policies/documentation-lifecycle.md`](docs/policies/documentation-lifecycle.md)。

## 5. 聊天记录迁移合同

原始会话保留在受控档案。项目迁移采用以下 provenance 提取流程：

```text
Provider archive
-> metadata inventory
-> project/thread classification
-> secret and personal-data scan
-> material-event extraction
-> Decision/Evidence/Constraint/Work/Preference candidates
-> current-evidence verification
-> typed import or sanitized replay fixture
```

每个导入对象必须记录：

```yaml
source_provider: codex | claude | other
source_thread_ref: opaque-hash
source_range_ref: opaque-range
project_id: string
extracted_at: rfc3339
extractor_version: string
content_sha256: sha256
classification: decision | evidence | constraint | work | preference | replay
validity: candidate | verified | stale | rejected
verified_against: [artifact-ref]
contains_sensitive_data: boolean
retention_class: ephemeral | project | audit
```

Git admission policy 排除完整 JSONL、隐藏提示词、工具凭据、访问 token、未脱敏用户数据、大段构建输出、完整 diff、模型 reasoning 和供应商专有字段。入选历史样本保留最小触发输入、必要状态、期望结果和来源 hash。

## 6. MASTER 任务 DAG

```mermaid
flowchart LR
    M0["M0 仓库与治理"] --> M1["M1 聊天来源与 Replay"]
    M1 --> M2["M2 Typed State"]
    M2 --> M3["M3 任务路由"]
    M2 --> M4["M4 Skill 控制面"]
    M3 --> M5["M5 压缩与 Context Composition"]
    M4 --> M5
    M5 --> M6["M6 检索与 Recall Providers"]
    M5 --> M7["M7 幻觉与代码质量"]
    M7 --> M8["M8 多协作者与耐久工作流"]
    M8 --> M9["M9 Docmost 人类控制台"]
    M9 --> M10["M10 跨项目生产发布"]
```

分支规则：任何新分支必须有 `parent_id`、scope、return point、exit criteria、attempt budget、expiry 和 promotion target。实验分支默认 `mainline_authority: false`；canonical queue 变更须通过 promotion gate。

### 6.1 想法接入与上下文回返合同

执行过程中出现的新想法、旁支问题或潜在架构改进先登记为 `Idea` candidate，不直接改变 active work。每个 Idea 至少绑定：

```yaml
idea_id: stable-id
parent_task_id: stable-id
source_thread_ref: opaque-hash
captured_at: rfc3339
summary: string
scope: string
status: candidate | parked | proposed | approved | rejected | superseded
return_point: stable-id
expiry: rfc3339 | null
attempt_budget: uint32 | null
promotion_target: work-id | null
evidence_refs: [artifact-ref | assertion-id]
```

Idea 的默认路由是 `capture-and-continue`：记录候选、保留当前 active leaf、继续当前任务。Idea 生命周期通过 `idea_captured`、`idea_parked`、`idea_proposed`、`idea_approved`、`idea_rejected` 和 `idea_superseded` 追加式事件管理。Idea 只有在用户明确要求切换，或经过可审计的 `switch proposal` 与 CAS 激活后，才能成为 active work。需要进入 canonical queue 的 Idea 必须经过 evidence review、scope/ownership 检查、attempt budget 和 promotion gate；未批准的 Idea 不得修改主线、Product 代码或权威状态。

当 Idea 需要立即处理时，先为原任务提交滚动 checkpoint，再写入 `task_suspended` 与 `task_activated` 事件。Checkpoint 必须保留原任务的 canonical digest、active leaf、latest decision、reverted/rejected decision、hard blocker、return point、验证缺口和禁止副作用。恢复原任务时，只加载其 Execution Packet 和 checkpoint。Execution Packet 只携带与当前任务相关的 Idea ID 和单行摘要；完整 Idea 通过 bounded lookup 展开。其他 parked/candidate Ideas 保留在状态服务，不进入 prompt、Skill 或执行权限。

压缩前冻结新的外部副作用并提交增量 checkpoint。压缩后 validator 先恢复原 active task，再允许读取 Idea 候选或发起 switch proposal。若 checkpoint、revision、path ownership 或 current evidence 不一致，权限降级为只读，禁止写代码、提交、部署和新任务派发。

Idea 分类、去重、correction 写保护、自然语言交互和回返 packet 的完整合同见 [`docs/architecture/idea-continuity.md`](docs/architecture/idea-continuity.md)。

### 6.2 Skill 编排与初始化合同

Skill 是带版本、hash、rule ID 和适用范围的规则资产。Skill 不保存 active task、动态 blocker、owner、claim、revision 或临时决定；这些字段只存在于 Typed State、Event Log、Checkpoint 和 Execution Packet。每个 Skill manifest 至少声明：

```yaml
skill_id: stable-id
version: semver
content_sha256: sha256
source_kind: builtin | external | project | user | workflow
license_ref: artifact-ref | SPDX-id
applicability: [task_kind, project_id, repo, path, operation, role, provider]
rule_ids: [stable-rule-id]
dependencies: [skill-id@range]
conflicts: [skill-id@range]
expires_at: rfc3339 | null
compatibility: [schema-version | provider-contract]
provenance_refs: [artifact-ref | assertion-id]
status: proposed | approved | active | quarantined | deprecated | rejected
```

Skill catalog 包含五类来源：

1. `builtin`：跨项目的最小核心规则，例如状态提交门、证据门、checkpoint/canary、敏感信息处理和 provider-neutral 术语。
2. `external`：第三方或组织 Skill。必须经过 license、来源、hash、依赖和安全审查，默认以候选状态进入项目。
3. `project`：项目 Profile 绑定的构建、测试、目录、官方来源和发布约束；由项目治理流程批准。
4. `user`：用户偏好和习惯的显式定制。它只能影响表达、工具偏好和非安全性流程参数，不能放宽权威状态、证据、权限或验收门。
5. `workflow`：Thinker、Executor、Verifier、review、handoff 等角色和操作流程。它描述角色允许的动作与输入输出，不赋予角色直接写权威状态的权限。

Skill resolver 按以下顺序确定结果：显式的 `Task/Goal/Experiment`、active claim 和 path owner；Project Profile 的 repo/path/operation 约束；角色与 provider adapter；manifest applicability、依赖、冲突、版本和 expiry 校验；最后才使用相似度、历史 memory 或模型生成的候选排序。候选排序不能绕过显式绑定、审批、validator 或 CAS。项目初始化可以根据仓库、验证配置和用户习惯生成 Skill proposal，但发布前必须由用户或项目治理 owner 审批，并保留 proposal、输入证据、生成器版本和批准事件。

装载采用渐进披露：`S0` bootstrap 只含安全、状态和恢复门；`S1` manifest 只含适用性与依赖元数据；`S2` compiled packet 只含当前 active leaf 所需 rule IDs 和短规则；`S3` full Skill 与 references 按需读取。进行中的任务锁定已批准的 rule set、manifest hash 和 schema/provider contract；Skill 变更、缺失路径、hash/version/digest/expiry 漂移或冲突会进入 quarantine，禁止执行，直到兼容 replay 或显式 migration 通过。所有 resolver 决策、proposal、approval、quarantine、replay 和 deprecation 都写入可审计事件，并能从 checkpoint 重建。

### 6.3 Reference 与 Harness 演进合同

外部参考按 current source、standard、OS/software official、vendor engineering、research、organization、community 和 marketplace 分类。发现器登记 canonical URL、retrieval revision/hash、authority、license、validity、refresh trigger 和 adoption status；它只能产生 candidate 和 stale signal。承重 assertion 通过 current-evidence verification 与 M7 gate 后才能进入完成证据。完整合同见 [`docs/policies/reference-evidence-lifecycle.md`](docs/policies/reference-evidence-lifecycle.md)。

每个 provider-neutral Harness Run 绑定 task revision、claim、Execution Packet hash、Skill digest、tool grants、checkpoint、effect watermark、Verification Profile、reference validity watermark 和 OTel trace。Provider transcript、session ID、goal、host checkpoint 和 workflow state 作为 provenance；task completion、决定、effect 和 promotion 仍通过 State MCP 提交。

### 6.4 有界信息访问与项目适配合同

控制面通过 `RetrievalReceipt` 记录查询类型、source revision/hash、选中范围、读取与输出 bytes、freshness 和验证结果。Execution Packet 只装载当前 active leaf 所需字段、锁定 Skill rule IDs、承重 evidence refs 和唯一 next action；完整历史、报告、大型日志、候选 Idea 和 memory 通过 opaque ref 进行 bounded expansion。外置 memory、索引、缓存和 reviewer 在 provider 故障或结果冲突时降级，不改变权威状态。

安装到项目后，系统可以从已验证 run、明确用户纠正和失败 fixture 生成 versioned `ProjectAdaptation` proposal。proposal 可调整检索顺序、常用目录、Skill applicability、验证提示和非安全表达偏好；它不能改变 active task、claim、path ownership、authorization、validator、evidence gate、promotion gate、retention 或 provider-neutral 协议。proposal 必须经过 shadow/A-B replay、safety veto、审批、expiry、rollback 和 opt-out/reset 约束后才能影响新 packet。详细合同见 [`docs/architecture/adaptive-information-plane.md`](docs/architecture/adaptive-information-plane.md)。

## 7. 执行台账

### M0 仓库与治理

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M0-01 | ✅ | 建立独立 Git 仓库 | 与 ProjectCompute、Platform 生命周期解耦 | 任意项目可复用 | 无 | repo root、main branch、边界文件存在 |
| M0-02 | ✅ | 建立 canonical `MASTER.md` | 主线、DAG、状态和完成门统一 | 计划变更纳入治理 | M0-01 | 文档结构校验通过 |
| M0-03 | ✅ | 建立原始会话 Git admission policy | 控制隐私、仓库体积和上下文输入 | 保持仓库安全可移植 | M0-01 | `.gitignore` 与 ingestion policy 一致 |
| M0-04 | ✅ | 迁入上下文可靠性评估 | 研究证据进入项目资料集 | 支持研究结论复用 | M0-01 | 来源 hash 与编辑修订记录完整 |
| M0-05 | ✅ | 建立短小 `STATUS.md` 路由器 | 日常恢复通过定向引用完成 | 控制 MASTER 装载成本 | M0-02 | 当前任务、阻塞、next action 和文档引用可在小文件恢复 |
| M0-06 | ✅ | 建立正式文档语言规范 | 规范使用稳定属性、权限和验收指标 | 保持长期文档一致性 | M0-02 | style policy 与文档审计通过 |
| M0-07 | 🧑‍💻 | 建立 schema/version/release governance | 所有协议可演进和回放 | 保障 checkpoint 兼容性 | M0-02 | registry/hash、transition、migration/replay/rollback 和 quarantine 离线验收完成；runtime State MCP migration 待 M2 |
| M0-08 | 🧑‍💻 | 建立 ReferenceSource/Snapshot lifecycle、candidate catalog 与 harness 采用评估 | 外部资料可持续发现、固定、刷新、失效和复用 | 让研究证据进入后续 schema、adapter 与实验 | M0-03/M0-06 | Codex/Claude 官方来源带 URL/hash/refresh/adoption；4 个 catalog tests 通过；State/Watcher live integration 待后续阶段 |
| M0-09 | 🧑‍💻 | 建立项目 self-dogfood observation protocol 与首个 baseline | 研发过程记录 compaction、Skill 装载和 MASTER 演进 | 以项目自身数据验证逐步优化 | M0-05/M0-07 | compaction、Skill load、plan revision 和 verification 可复核；10 个正反测试通过；自动 trace 待 M5/M8 |
| M0-10 | ⏳ | 建立文档分类、更新触发、容量预算、supersedes 和生成投影生命周期 | MASTER、STATUS、细分文档和投影保持可定位、可更新、可收敛 | 防止长期陈旧与无界扩展 | M0-05/M0-06/M0-07 | policy validator、STATUS 与 evidence 漂移、重复全文、过期引用、容量超限和权限故障测试通过；拆分后恢复字段 100% |
| M0-11 | ⏳ | 建立 Git branch/commit/PR/merge 与 staged admission 合同 | Git 集成边界可回放且不冒充权威状态 | 让单人、多 AI 和多人协作具有一致的审计与发布节奏 | M0-02/M0-03/M0-09 | policy、message/PR packet validator、staged transcript/secret admission、regular-merge replay 和 first-commit audit 通过 |
| M0-12 | 🧑‍💻 | 建立 provider-neutral CI Verification Profile、统一 verifier 与 Gitea required jobs | push/PR 自动执行 test、compile、data/schema/projection、privacy、benchmark 和 secret gates | 让本地开发、协作者与托管平台使用同一可复现验收边界 | M0-03/M0-07/M0-09 | 12 个 verifier 正反测试、100/100 repository tests、compile 和 Gitleaks 本地通过；Gitea push/PR live evidence 与 branch protection 待 remote 配置 |

### M1 聊天来源与 Replay Corpus

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M1-01 | ✅ | 通过元数据盘点 Codex/Claude/派生 memory | 获得来源规模基线 | 制定归档与提取策略 | M0-03 | 文件数、bytes、大文件数已记录 |
| M1-02 | 🧑‍💻 | 建立 source registry 和 opaque thread IDs | 来源可追溯；本机路径与 provider ID 受隔离 | 支持跨机器迁移 | M1-01 | 同一来源稳定映射，公开输出无原 ID；15 个 TDD/边界测试通过 |
| M1-03 | 🧑‍💻 | 建立 secret/PII/license sanitizer | fixture/state admission 由 sanitizer 门控制 | 支持共享与未来开源 | M1-02 | secret、PII、machine path、SPDX license 注入测试通过；有 findings 或无 provenance 时 admission 为 0 |
| M1-04 | ✅ | 从 ALTP/ECN/弱网/10Gbps 抽取首批 40 个 fixtures | 真实返工形成回归集 | 提供真实项目评估样本 | M1-03 | 40/40 包含输入、期望状态、来源/range/content hash、脱敏证明和 current evidence；独立复验与 corpus hash 通过 |
| M1-05 | 🟡 | 覆盖 task switch、stale Skill、SIGKILL、503、并发 CAS | 正常与故障路径均可重复 | 建立完整 replay 覆盖 | M1-04 | E0-E9 各有最小 fixture |
| M1-06 | 🧑‍💻 | 建立 archive retention/export/delete | 可移植、可撤销、可审计 | 满足长期协作与隐私治理 | M1-02 | 9 个 retention/export/import/tombstone/deletion-proof 合同测试通过；production adapter 与 backend receipt 待 M2/M8/M10 |
| M1-07 | 🧑‍💻 | E0/E1 context compression 与 Execution Packet benchmark | 量化恢复率、旧决定复活、Skill 输入和 token proxy | 建立可复现实验基线 | M1-03/M1-04 | 合成 4 场景与真实 40 场景可重复；真实 corpus 在 768 字符时 E1 恢复 100%、旧决定复活 0；live tokenizer/A-B 待 M5/M7 |

### M2 Typed State、Event 与 Checkpoint

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M2-01 | ⏳ | 定义 Project/Task/Idea/Decision/Evidence/Blocker/Effect schema | 自然语言状态变成类型合同 | 无损恢复 | M1-04 | schema fixtures round-trip 100% |
| M2-02 | ⏳ | 定义 append-only Event、supersedes 和 reducer | 历史可 replay | 旧决定复活率为 0 | M2-01 | replay 结果与 snapshot byte-equivalent |
| M2-03 | ⏳ | PostgreSQL revision/CAS state store | 并发冲突显式返回 | 多协作者安全基础 | M2-02 | conflict test 100% 可见 |
| M2-04 | ⏳ | content-addressed artifact store | 大日志和 diff 通过 artifact ref 引用 | 降 token 并保存证据 | M2-01 | checksum、range read、损坏检测通过 |
| M2-05 | ⏳ | State MCP read/commit/claim/effect API | 各 agent/provider 使用统一协议 | provider-neutral | M2-03 | contract tests + auth boundary tests |
| M2-06 | ⏳ | immutable checkpoint 与 canary manifest | 压缩和交接可验证 | 建立确定性恢复 | M2-04/M2-05 | 关键字段恢复 100% |
| M2-07 | ⏳ | Project Profile 与 ProjectAdaptation typed schema | 项目接入和自适应候选可版本化、可回放 | 降低重复读取并贴合项目使用 | M2-01/M0-09 | proposal、输入证据、scope、hash、expiry、rollback 和 approval round-trip 100% |

### M3 任务图与智能切换

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M3-01 | ⏳ | 建立 Campaign/Goal/Work/Experiment DAG | 分支、阻塞、回流可计算 | 将发散与停滞转化为可检测状态 | M2-03 | 无环、无孤儿、无无回流分支 |
| M3-02 | ⏳ | sticky task router | 默认保持当前 active leaf | 保持压缩前后任务一致 | M3-01 | E2 classification 达标 |
| M3-03 | ⏳ | continue/child/interrupt/switch/correction 事件 | 切换可审计、可恢复 | 以信号和事件驱动任务切换 | M3-02 | return point 恢复 100% |
| M3-04 | ⏳ | active/claim/path-owner 副作用门 | task binding 冲突时授予只读权限 | 将副作用绑定至权威任务 | M3-03 | 误切写入/提交/部署为 0 |
| M3-05 | ⏳ | attempt budget、expiry、promotion gate | 实验按预算和期限运行 | 实验发现有序回流 MASTER | M3-01 | 无预算分支和未授权 promotion 为 0 |
| M3-06 | ⏳ | Idea candidate、parking、capture-and-continue 与 switch proposal | 新想法不污染当前 active leaf | 保留价值并控制上下文切换 | M3-03/M3-05 | Idea 有 parent/return point/expiry；未授权 Idea 不改变 active state |
| M3-07 | ⏳ | Idea relationship、dedupe、correction、urgency 与 impact review | 重复或跨域想法形成有界候选队列 | 支持持续输入并避免 prompt/主线污染 | M3-06/M6-02 | 去重确定；correction 写保护；expired/parked Idea 不进入执行权限 |

### M4 Skill 控制面

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M4-01 | ⏳ | Skill manifest/version/hash/applicability schema | Skill 可选择、可锁定、可追溯 | 控制压缩恢复的规则装载范围 | M2-01 | schema 与冲突测试通过 |
| M4-02 | ⏳ | 稳定 rule IDs 与 compiled packet | 当前任务规则形成有界 packet | 降低重复 token | M4-01 | 指令遵循保持基线，输入下降达到门槛 |
| M4-03 | ⏳ | missing path/digest/version/expiry validator | 漂移 Skill 自动 quarantine | stale rule 激活率为 0 | M4-01 | E4 故障 100% 拦截 |
| M4-04 | ⏳ | S0-S3 分层加载 | 恢复装载 1-2 KB bootstrap 与 2-6 KB packet | 缩短恢复热路径 | M4-02/M4-03 | p95 和 token 门槛通过 |
| M4-05 | ⏳ | Codex/Claude/其他 provider Skill adapter | 同一规则合同跨工具使用 | 可移植协作 | M4-04 | 两种以上 provider replay 一致 |
| M4-06 | ⏳ | Skill 变更 replay 与兼容锁 | 进行中任务固定使用已记录 rule set | 可持续迭代替换 | M4-03 | breaking change 被拒或显式迁移 |
| M4-07 | ⏳ | Built-in、External、Project、User、Workflow Skill catalog | 来源、license、provenance 和权限边界可审计 | 管理可复用与项目专属规则 | M4-01/M0-07 | 五类来源均有 manifest、准入和 quarantine 测试 |
| M4-08 | ⏳ | 项目初始化与用户习惯 Skill proposal | 从仓库、验证配置和显式偏好生成可审查候选 | 缩短接入并保持用户控制 | M4-07/M7-03 | proposal 可重现；未经批准不得 active |
| M4-09 | ⏳ | role/operation-aware Skill resolver | Thinker、Executor、Verifier 和 provider 获得最小规则集 | 控制动作权限与上下文成本 | M3-04/M4-04/M4-07 | 选择优先级、冲突、expiry、replay fixture 全部通过 |
| M4-10 | ⏳ | 官方 Skill、Agent Skills standard、GitHub 和 marketplace catalog adapter | 发现结果带直接 URL、revision、hash、license 和 trust tier | 智能复用外部能力并阻止未审查加载 | M4-07/M0-07 | official/standard/market/community 条目均可 snapshot、quarantine、replay |

### M5 压缩与 Context Composition

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M5-01 | ⏳ | Execution Packet composer | 当前叶包含任务、规则、证据、相关 Idea refs 和唯一 next action | 恢复成本与项目总历史解耦 | M3-03/M4-04 | packet 4-12 KB 且 canary 100% |
| M5-02 | ⏳ | material event 滚动 checkpoint | PreCompact 提交增量 delta | 提高压缩速度 | M2-06 | PreCompact p95 < 500 ms |
| M5-03 | ⏳ | PostCompact deterministic canary | 恢复错误在写代码前被阻断 | 保证一致性 | M5-01/M5-02 | decision/constraint/work 100% |
| M5-04 | ⏳ | artifact ref 与 bounded expansion | 大输出按需展开 | 降低 token 和注意力污染 | M2-04 | prompt 保存摘要、引用和必要片段 |
| M5-05 | ⏳ | token/cache/retrieval accounting | 形成成本与时延明细 | 以实测数据确定优化优先级 | M5-01 | provider 账单/trace 可对账 |
| M5-06 | ⏳ | Idea-aware checkpoint 与 context return packet | 压缩、切换后可回到原任务 | 保留 return point、相关 Idea refs 和禁止副作用 | M3-06/M5-03 | 原任务恢复 100%；Idea 正文不复制进 packet；candidate Idea 不获得执行权限 |
| M5-07 | ⏳ | Project dogfood compaction/input-routing/Skill/plan observation emitter | 每次恢复、消息或 Idea 路由、Skill 选择和计划演进产生可比较事件 | 持续验证控制面是否真实优化自身研发 | M0-09/M4-04/M5-05/M8-04 | active leaf/return point/路由/中断/Skill/目标 revision 事件覆盖率 100%；provider 未暴露指标保持 null；至少 3 个可比较样本后才输出趋势；任一 veto 失败标记 regressed |

### M6 检索、代码图与 Recall Providers

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M6-01 | ⏳ | 固化 `rg -> Zoekt -> LSP -> SCIP -> RTFM` 路由 | 按问题选择最小工具 | 减少重复全仓扫描 | M5-04 | E5 precision/recall/freshness 达标 |
| M6-02 | ⏳ | CodeGraph 跨仓影响线索 | 图关系与精确检索联合使用 | 控制图索引遗漏风险 | M6-01 | 核心符号用 `rg/LSP` 双检 |
| M6-03 | ⏳ | Recall Provider SPI | Mem0/Hindsight/Graphiti 可替换 | 新技术可消融和替换 | M2-05 | provider 503 不影响权威状态 |
| M6-04 | ⏳ | memory 消融测试 | Provider 准入依据真实增益 | 建立可重复的组件准入机制 | M6-03 | 相对 no-memory 基线有显著收益且安全指标保持基线 |
| M6-05 | ⏳ | 外置/本地 reviewer adapter | 难题和 handoff 获得第二意见 | 提高审查强度 | M5-01 | 状态提交权限为 0；超时采用异步降级 |
| M6-06 | ⏳ | MCP Registry/provider admission adapter | MCP server 发现、publisher、license、auth 和 tool scope 可审计 | 连接器按项目和操作受控启用 | M4-10/M2-05 | registry snapshot 固定 revision；未授权写工具激活率为 0 |
| M6-07 | ⏳ | RetrievalReceipt、bounded expansion 与 index/cache freshness | 外置资料只按最小范围进入 packet，重复读取可计量 | 减少直接读取并保持 current evidence | M2-04/M5-04/M7-01 | receipt provenance 100%；承重 assertion 通过率 100%；重复读取 bytes 相对 E0 下降 >=30% |

### M7 幻觉、证据与代码质量

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M7-01 | ⏳ | assertion authority/version/hash/validity | 承重结论可追溯 | 始终参考当前官方与代码证据 | M6-01 | 承重断言 provenance 100% |
| M7-02 | ⏳ | claim-evidence gate | 无证据完成声明和臆测路径被阻断 | 降低幻觉进入代码 | M7-01 | E6 无证据完成/伪路径为 0 |
| M7-03 | ⏳ | 项目化 Verification Profile | 各项目使用匹配的 TDD/build/live 门 | 保持通用协议与项目验证差异 | M2-01 | AlkaidLab 与第二项目 profile 通过 |
| M7-04 | ⏳ | 同模型同预算 patch A/B 盲评 | 隔离记忆系统对代码质量的真实影响 | 使用客观质量指标评估 | M7-02/M7-03 | build/test/mutation 保持基线，返工下降 |
| M7-05 | ⏳ | affected graph 与测试选择 | 缩短验证时间并保持完整覆盖 | 提高大项目效率 | M6-01/M7-03 | wall time 下降 >=30%，漏测 0 |
| M7-06 | ⏳ | ReferenceWatcher、freshness、hash change 与 assertion supersedes | 上游文档和标准变化可审计地使旧证据失效 | 持续吸收新资料并阻止 stale 结论 | M0-08/M7-01 | upstream change fixture 100% stale/quarantine；未复核 assertion 通过完成门为 0 |

### M8 多协作者与耐久执行

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M8-01 | ⏳ | DBOS checkpoint/effect workflow | 本地崩溃恢复和幂等 | 单机可靠运行 | M5-03 | SIGKILL/retry 重复副作用 0 |
| M8-02 | ⏳ | lease、claim、ownership、expiry | 工作叶具备唯一有效 claim | 团队协作 | M3-04/M8-01 | 并发静默覆盖 0 |
| M8-03 | ⏳ | Temporal 长流程与 Continue-As-New | 跨服务、长周期工作可 replay | 大型团队生产化 | M8-02 | replay/patch/versioning tests 通过 |
| M8-04 | ⏳ | OTel `context.*` trace | 切换、压缩、检索、返工可观察 | 持续优化而非凭感觉 | M5-05 | trace 与 state revision 可关联 |
| M8-05 | ⏳ | 权限、审计、tenant/project 隔离 | 协作者访问范围与授权一致 | 安全共享 | M2-05/M8-02 | 越权测试 100% 拒绝 |
| M8-06 | ⏳ | Provider-neutral Harness Run 与 feedback-loop contract | model、packet、工具、权限、checkpoint、evidence、effect 和 trace 绑定同一 revision | 使 Codex/Claude host 能力可替换和可回放 | M2-05/M4-05/M5-03/M8-04 | 双 provider replay 一致；run/effect/evidence trace 100%；host 故障不改变权威状态 |
| M8-07 | ⏳ | ProjectAdaptation observe/propose/shadow/approve/rollback loop | 项目和用户习惯可在安全边界内持续优化 | 使安装后的控制面随实测使用演进 | M5-07/M6-07/M7-04 | 未批准 proposal 激活率 0；相同 fixture/provider/budget 三次 A/B；回滚后 veto 指标恢复 |

### M9 Docmost 与人类观察

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M9-01 | ⏳ | 外部 State MCP provider | Docmost 读取同一权威状态 | 保持单一状态源 | M2-05 | 同 revision 视图一致 |
| M9-02 | ⏳ | MASTER Project Graph | 主任务、分支、阻塞、回流可视化 | 检测范围扩张与长期阻塞 | M3-05/M9-01 | 环/孤儿/过期分支可见 |
| M9-03 | ⏳ | Decision Timeline 与 Evidence Matrix | 看到为何做、何时推翻、证据在哪 | 降低重复争论和臆测 | M7-01/M9-01 | supersedes 与 provenance 可追溯 |
| M9-04 | ⏳ | Context、Reference 与 Harness Health、Replay 页面 | 人类看到压缩、Skill、reference freshness、run、token 和误切风险 | 持续监管自动化 | M7-06/M8-04/M9-01 | SLO、stale assertion 与失败 fixture 可下钻 |
| M9-05 | ⏳ | 审批、promotion、纠偏和审计入口 | 人类保留主线治理权 | 自动化方向始终一致 | M3-05/M8-05 | 未批准 promotion 无法生效 |
| M9-06 | ⏳ | Obsidian 只读生成 vault | 离线可视化和可移植文档 | 支持多种人类观察前端 | M9-01 | 人工修改通过治理入口回流 |
| M9-07 | ⏳ | Docmost Relationship / Impact force-directed view | 探索依赖、影响范围和关系簇 | 补充确定性 Project DAG | M6-02/M9-02 | 同 revision 投影；可筛选/聚焦；治理动作经 State MCP/CAS/validator |

### M10 跨项目发布

| ID | 状态 | 内容 | 效果 | 目的 | 依赖 | 完成门 |
|---|---|---|---|---|---|---|
| M10-01 | ⏳ | AlkaidLab 三仓 shadow pilot | 用真实超大型项目验证 | 取得生产证据 | M7-05/M8-04 | E0-E9 全门通过 |
| M10-02 | ⏳ | 第二个跨领域项目接入 | 验证核心协议的项目中立性 | 验证可移植性 | M10-01 | 核心代码 fork 数为 0 |
| M10-03 | ⏳ | 多协作者 pilot | 验证 claim、lease、权限和交接 | 支持未来团队 | M8-05/M9-05 | 静默覆盖 0，handoff 100% |
| M10-04 | ⏳ | backup/export/import/disaster recovery | 状态可迁移和恢复 | 长期可持续 | M8-03 | 新实例完整 replay 且 hash 一致 |
| M10-05 | ⏳ | 版本化发布、升级和回滚 | 新技术通过兼容层与迁移协议接入 | 可持续演进 | M0-07/M10-04 | N-1 compatibility + rollback 通过 |
| M10-06 | ⏳ | 跨项目 adaptation/profile migration | 项目升级和 provider 变化保留已验证的个性化配置 | 长期可移植演进 | M2-07/M8-07/M10-02 | 两个项目 profile replay；迁移/回滚 hash 一致；opt-out/reset 可验证 |
| M10-07 | ⏳ | 默认生成 MASTER、STATUS、目标态架构全表和最小 Project Profile | 人类与不同 Agent 使用一致的项目入口 | 让个人、协作和公开项目开箱获得完整治理投影 | M0-10/M2-07/M9-01/M10-02 | 两类项目初始化/升级/卸载 replay；三文档字段恢复 100%；公开 Git admission 泄漏 0 |

## 8. E0-E9 实验链路

| 实验 | 验证问题 | 生产门 |
|---|---|---|
| E0 | 当前流程真实 token、恢复、Skill、返工基线是什么 | 数据可复现并可对账 |
| E1 | Typed State 是否优于自由摘要 | 最新决定/约束/open work 100%，旧决定复活 0 |
| E2 | 是否能智能切任务而不误写 | return point 100%，误切副作用 0 |
| E3 | 分层 Skill、resolver 和初始化 proposal 是否真省 token且不改变权限 | 重复 Skill 输入下降 >=60%，规则遵循不退化；未经批准的 proposal 不得激活 |
| E4 | Skill 漂移、冲突和外部来源风险能否被发现 | missing path/hash/version/digest、license/provenance 缺失和冲突故障 100% quarantine |
| E5 | bounded retrieval 是否省 token 且不漏证据 | 承重引用 100%，重复检索下降 >=30% |
| E6 | 旧 memory、伪路径、回滚优化是否会造成幻觉 | stale decision、伪路径、无证据完成声明均为 0 |
| E7 | 代码质量是否真实提高 | build/test/mutation 不退化，scope violation 0，返工低于 E0 |
| E8 | 多协作者是否会静默覆盖 | CAS 冲突可见，静默丢写 0 |
| E9 | 崩溃、503、checkpoint 损坏能否恢复 | 重复副作用 0，损坏漏检 0，restore p95 达标 |

E1、E2、E4、E6、E8、E9 具有 veto 权限；平均得分、token 降幅和主观体验属于辅助指标。

合成 canary 证据见 [`docs/research/context-compression-benchmark-2026-08-09.md`](docs/research/context-compression-benchmark-2026-08-09.md)：4 个脱敏场景在 768 字符预算下由 E1 Execution Packet 达到关键状态恢复 100%、旧决定复活 0、Skill 输入下降 75%、token proxy 下降 20.7031%。真实 replay 证据见 [`docs/research/context-compression-real-replay-benchmark-2026-08-09.md`](docs/research/context-compression-real-replay-benchmark-2026-08-09.md)：40 个场景在 768 字符预算下恢复 100%、旧决定复活 0、Skill 输入下降 74.7903%、token proxy 下降 12.6042%；512 字符触发 capacity veto。provider tokenizer、live compaction telemetry 和双 provider 同模型同预算 A/B 仍待 M4、M5、M7 和 M8。

## 9. 生产验收

| 指标 | 门槛 |
|---|---:|
| 最新决定恢复 | 100% |
| 不可违反约束恢复 | 100% |
| 未完成工作与 return point 恢复 | 100% |
| 旧决定复活 | 0 |
| 误切导致写入/提交/部署 | 0 |
| 重复外部副作用 | 0 |
| 并发静默丢写 | 0 |
| stale Skill rule 激活 | 0 |
| 未批准或越权 Skill proposal 激活 | 0 |
| Skill 选择可追溯（manifest hash、rule IDs、适用信号） | 100% |
| Skill license/provenance/依赖/冲突准入记录 | 100% |
| Harness Run 与 task/effect/evidence/trace 关联 | 100% |
| 承重 ReferenceSource freshness/validity | 100% |
| Project dogfood compaction/input-routing/Skill/plan event coverage | 100% |
| Execution Packet capacity canary | 关键字段恢复 100%；不足预算具有 veto 权限 |
| checkpoint/artifact 损坏漏检 | 0 |
| 承重断言 current provenance | 100% |
| PreCompact 增量提交 p95 | < 500 ms |
| state-only restore p95 | < 2 s |
| 重复 Skill 输入 | 相对 E0 下降 >=60% |
| 重复检索 token | 相对 E0 下降 >=30% |
| 直接读取 bytes | 相对 E0 下降 >=30%，承重 bytes 通过 receipt 可追溯 |
| 未经批准的 ProjectAdaptation 激活 | 0 |
| adaptation rollback 后安全指标 | E1/E2/E4/E6/E8/E9 veto 全部恢复 |
| 文档动态状态归位 | 100% 进入 STATUS/event/state；MASTER 无会话态漂移 |
| 未授权文档治理变更 | 0 |
| affected build/test wall time | 相对全量下降 >=30%，漏测 0 |
| 代码质量 | build/test/mutation 不退化，scope violation 0，返工率低于 E0 |

## 10. 当前执行路由

M1-05 是当前 active leaf。先建立 E0-E9 fixture coverage matrix，复用 M1-04 的最小事件、opaque provenance、sanitizer 和独立 validator 合同，补齐 task switch、stale Skill、SIGKILL/commit boundary、503、checkpoint corruption 和 concurrent CAS 场景。故障 fixture 可以先定义 expected state/effect/gate；runtime 注入证据仍由 M2/M5/M8 对应实现提供。

M1-04 已完成 40 个 fixture admission，验收见 `docs/migrations/m1-04-replay-fixture-acceptance-2026-08-09.md`。受控 archive 继续采用流式、区间化读取；原始 JSONL 和 source namespace key material 禁止进入 Git。M1-06 archive retention/export/delete 的离线验收见 `docs/migrations/m1-06-archive-governance-acceptance-2026-08-09.md`。

## 11. 完成后的最终效果

受支持项目通过统一的 Execution Packet 向 Codex、Claude、其他 agent 和协作者提供工作入口。验收状态包括：active leaf 恢复率 100%，reverted decision 复活率 0，重复副作用 0，承重证据 provenance 100%，任务依赖与回流关系可视化，直接读取 bytes 和重复检索量相对基线下降，ProjectAdaptation 可回放、可审批、可回滚，治理文档保持可定位和可收敛。恢复成本以当前任务规模和有界信息访问范围为主要变量。
