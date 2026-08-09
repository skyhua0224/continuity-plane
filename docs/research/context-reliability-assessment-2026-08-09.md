# AlkaidLab 大项目上下文可靠性评估与试点方案

版本：revision 3  
日期：2026-08-09  
状态：研究结论，尚未改变 MASTER、项目源码、Skill 或 Codex 配置  
适用范围：`alkaidlab-platform`、`moonlight-macos`、`foundation-sunshine` 及未来 AlkaidLab 生态项目和协作者
来源快照 SHA-256：`bb5a578272eb06a25eea062cda7f8ce9d82b02844355ac44788bbcc821b99182`  
编辑修订：正式文档语言规范化；研究数据与采用结论保持不变

## 0. 评估状态

- Canonical 计划：`goals/goal-MASTER.md`。本报告权限为研究与设计评估，任务状态与执行授权保持在 canonical 治理流程。
- 问题定义：当前状态、参考知识、实验推理和历史结论共享同一压缩上下文，缺少稳定的类型与权限边界。
- 评估目标：建立 shadow mode，并验证压缩、模型切换和协作者交接后的主线、active leaf 与最新判决恢复一致性。

评估读取快照：

| 对象 | 大小/状态 | SHA-256 |
|---|---:|---|
| `goal-MASTER.md` | 566,077 bytes | `250aa0cdd4167b871ba8106207a4bf1c8a3b6cee887beb448ff4a4732947da4a` |
| `platform-target-architecture-2026-07-20.md` | 70,544 bytes | `87856be8468c6261773f3591eca058100fc8937c01118444bd05735d6f84ff0f` |
| `platform-full-inventory-2026-07-20.md` | 41,323 bytes | `adf58b5992fb9abb3b1bd15482f0f19b5ee9da93fd20ee8a23d377ce5823c8d6` |
| `alkaidlab-current-work` | 指向两个不存在的旧 Goal 文件 | `679f0c81bcda5df6b7e880d749c7f8e2d54cbf613f853488a0b4d807b2df34f1` |
| `alkaidlab-architecture` | 含已被目标态取代的架构事实 | `4e1c40537c6ef0e8aee712e6589829ac17707433b218c78797637ec979acf3a1` |
| `alkaidlab-progress-report` | 报告形式强，但要求模型反复重算全量状态 | `aa16ac38e69e8cbc0b5ad7ca3de501c10ee5ffcc46474ef23fb2ab171545a82a` |

证据分级：

- 当前证据：评估期间直接读取的 MASTER、目标态表、全表、Skill 和文件存在性。
- 历史背景：Claude 原始会话中的定向检索，以及由 Claude 会话生成的 project memory。历史背景用于复盘过程；当前代码状态须由 fresh evidence 确认。
- 组件实测：隔离临时目录中的测试、故障注入、索引和压缩 canary。测试结果证明对应组件行为；三仓端到端状态仍为 planned。

## 1. 现有体系的目标态

现有 MASTER、目标态架构全表和报告体系继续承担人类治理界面。模型执行入口调整为权威状态生成的当前叶 Execution Packet；原文通过稳定引用按需展开。

| 层 | 目标态 | 状态权限 |
|---|---|---|
| MASTER | 人类可读的唯一主线计划和完整历史 | 经治理流程更新，不由摘要更新 |
| Platform 目标态全表 | 人类可读的架构画布 | 经用户确认或已验事实更新 |
| 报告体系 | 从机器状态生成目标态表、动作台账和证据视图 | 只投影，不自行推断状态 |
| Typed State | 当前 active work、依赖、判决、门禁和证据的权威机器状态 | 提交方式为 CAS |
| Event Log | 追加式记录每个状态变化及 supersedes 关系 | 不可覆写历史 |
| Checkpoint | 压缩、换模型、交接前的不可变恢复点 | checksum 校验后读取 |
| Execution Packet | 当前 agent 的小型、定界执行包 | 从 Typed State 生成，无独立写权 |
| Reference Corpus | RFC、OS/软件官方文档、本地参考文档 | 证据权限 |
| Recall Memory | Mem0/Hindsight/Graphiti/Claude memory | 历史候选权限 |
| Obsidian | 生成的架构、ADR、checkpoint 和 replay 视图 | 人类投影视图 |

## 2. 已核实的真实问题

### 2.1 Skill 漂移已经发生

`alkaidlab-current-work` 仍要求读取：

- `goals/goal-MM-modularization.md`
- `goals/goal-AU-audio.md`

两者当前均不存在。MASTER 已明确自己是 2026-07-10 后的唯一 canonical 计划。若压缩摘要保留旧 Skill 而漏掉 MASTER 的新规则，恢复后的 agent 会沿不存在的计划继续工作。

`alkaidlab-architecture` 仍把 `gamestream-rtsp` 写为 `session.handshake` 的 peer Module，把 `network.transport` 写为统一 Slot；当前 MASTER/目标态已经把 GameStream 定为外部 Adapter，并要求撤销旧 transport root、按能力拆分 transport。Skill 内容范围应限定为稳定方法、权威入口和恢复协议；动态架构事实由 revisioned state 提供。

### 2.2 MASTER 很强，但同时承担了太多数据类型

MASTER 中同时存在：

- 当前状态；
- 目标态；
- 已完成证据；
- 被证伪假设；
- 回滚实验；
- 下一步；
- 外部标准引用；
- 例外与修订；
- 长篇测量日志；
- 一行内多次 supersede 的历史。

对人类来说这些信息非常有价值；对压缩器来说，它们没有稳定的类型边界。普通摘要无法可靠区分：

- `N-68 已执行并回滚` 与 `N-68 是可继续实施的优化`；
- `N-42 待做` 与 `N-42 被 N-67 硬阻塞`；
- `实验 Product 发现架构缺口` 与 `实验 Product 已成为主线 Product`；
- `假设待证伪` 与 `结论已定`；
- `🧑‍💻 只缺 live proof` 与 `✅ 全部完成`。

### 2.3 ALTP/10Gbps 历史复现了用户描述的返工模式

Claude 会话的历史背景表明，近期吞吐战役取得了真实成果，同时产生了可量化的 scope drift。下表用于构建复盘 fixture；性能数字的复用条件为当前代码校验与新一轮测量。

| 案例 | 最初判断 | 后续证据 | 造成的浪费/风险 |
|---|---|---|---|
| 单次 ping | 190 us 被当作物理下限 | 五组最小值为 92-181 us | 形成错误残余 bug，派出约 21 万 token 的追查 |
| pacer 对比 | 误认为 NewReno 和 BBR 使用相同 pacer | Product 默认只对 BBR 强制 pacing | 比较框架错误，后续才重建 A/B |
| 接收批量密度 | 高密度被理解为批得更好 | 密度实际是到达速率和循环周期的下游结果 | 两次解释方向相反，继续追错组件 |
| RTT 指标 | 最终 `srtt` 被当作整段传输概括 | `avg_rtt` 才与吞吐公式自洽，最终 srtt 跨运行散布 5.8 倍 | 连续追查接收端、ACK 路径和 ack_delay |
| 收侧墙 | 通过排除法收敛到 Linux 接收路径 | 逐段探针显示接收端多数时间在等数据 | 假设被正面推翻 |
| 被测对象 | 把吞吐瓶颈当 ALTP/CC 问题 | profiler 发现 `altp-filecopy` 的同步 pwrite/SHA 串行化 | 调协议但测到的是量具/Product 天花板 |
| ACK delay | 25 ms 语义错误，尝试改为 1 ms | 预注册回滚判据命中：PTO 假触发增加，改动回滚 | 如果只保留“1 ms 更符合 RFC”，压缩后会复活已否决改动 |

预注册判据、主控复核、真实 profiler、双向 A/B 和变异验证多次纠正了错误。当前缺口是将“已证伪”“已回滚”“实验 Product 范围”“主线晋升权限”编码为可在压缩后确定性恢复的状态。

### 2.4 LocalSend 式实验缺少主线晋升边界

Claude 原始用户消息确认：LocalSend/文件传输由用户批准为最小 Native 双端验证和 ALTP 的真实量具，验证范围包括配对、加密、可靠传输、跨平台和极限带宽。

问题出在实验启动之后。MASTER 记录了两种治理风险：

1. CL 叶族曾在 EX 授权队列之外创建并执行，后续纳入 EX-18 管理。
2. LocalSend Remix/`altp-filecopy` 倒逼出 MM-R58、MM-R59、MM-R60 等真实架构问题，但实验载体、最小 Native 验证目标和 Platform 主线优先级之间没有独立状态对象。

MM-R58 的 Adapter 反向依赖、MM-R59 的 `session.trust`、MM-R60 的 file sink/source 槽化仍属于候选架构缺口。当前系统缺少三个独立判定：实验完成必要性、跨 Product 通用性、主线优先级。候选缺口需经过以下流程：

```text
实验发现 -> 证据复核 -> 对照北极星 -> 主线影响分析 -> 用户/治理批准 -> 排入 canonical queue
```

于是“为了测 ALTP 临时需要的能力”可能在压缩后被解释为“Platform 当前最高优先级”。

## 3. AlkaidLab 专用状态合同

### 3.1 Project State

```yaml
project_id: moonlight-sunshine
revision: 1
canonical_plan:
  path: goals/goal-MASTER.md
  sha256: 250aa0c...
target_architecture:
  path: goals/platform-target-architecture-2026-07-20.md
  sha256: 87856be8...
north_star:
  - Product mechanism LOC must not increase
  - Product mechanism -> 0
  - GameStream compatibility remains in Adapter
  - complete Module only; no accepted half Module
layer_contract:
  id: platform-contract-v0.3
active_campaign: string
active_work_item: string
queue_position: string
```

`canonical_plan.sha256` 变化时重新解析并生成新 revision。该字段的写入权限归属于 canonical plan ingestion 流程。

### 3.2 Work Item

```yaml
id: N-67
status: pending | in_progress | blocked | done | reverted
phase: N
owner: actor-id
objective: string
depends_on: [work-id]
blocked_by: [work-id]
supersedes: [decision-id]
affected_repos: [platform, moonlight, sunshine]
affected_paths: [string]
allowed_product_delta: non_positive
required_dual_end_evidence: true
required_references: [assertion-id]
verification_commands: [string]
live_proof_status: pending | passed | not_required
next_action: string
```

`N-42` 使用 `blocked_by: [N-67]` 结构化边。`N-68` 使用 `status: reverted`，并关联 supersedes/rejected decision；验收指标为压缩后 TODO 误恢复率 0。

### 3.3 Experiment

实验 Product 采用独立对象，其状态权限排除 active campaign 直接变更：

```yaml
experiment_id: EXP-ALTP-10G-001
kind: measurement_harness
product: altp-filecopy
origin_work_item: N-42
mainline_authority: false
hypothesis: string
pre_registered_criteria:
  accept: [string]
  reject: [string]
controlled_variables: [string]
measurement_direction: A | B | both
environment_digest: sha256
result: supported | rejected | inconclusive
promotion_status: not_requested | proposed | approved | rejected
promotion_target: work-id | null
supersedes: [experiment-id, decision-id]
```

权限规则：`mainline_authority: false` 的实验可修改隔离分支和测试载体；主线叶、active work 与架构状态的变更须通过 promotion proposal 和治理批准。

### 3.4 Evidence 与 Reference Assertion

官方文档引用升级为稳定 assertion：

```yaml
assertion_id: RFC9002-5.3-ACK-DELAY
source_kind: industry-standard
source_uri: https://www.rfc-editor.org/rfc/rfc9002#section-5.3
source_version: RFC9002
content_sha256: sha256
statement: string
applies_to: [N-68]
validity: active | superseded | withdrawn
supersedes: [assertion-id]
```

行业标准、OS 官方文档、软件官方文档和本地参考文档归类为 evidence。Thinker 可新增 assertion candidate；active assertion 晋升由 verifier 核验。

## 4. 压缩前后协议

### 4.1 PreCompact

压缩前协议采用增量提交，MASTER 全文读取位于常规热路径之外：

1. 冻结新的外部副作用。
2. 把大日志、diff、测试输出写入 content-addressed artifact store。
3. 记录当前 Project revision、三仓 branch/HEAD/dirty digest。
4. 提交 active work、blocked_by、latest decision、reverted decision、next action。
5. 生成 canary manifest。
6. 用 `expected_revision` CAS 提交 checkpoint。
7. checksum、fsync 或 CAS 任一失败则不允许压缩。

### 4.2 PostCompact

新上下文通过以下结构化字段完成恢复声明：

```yaml
canonical_plan_digest: string
active_campaign: string
active_work_item: string
hard_blockers: [work-id]
latest_active_decisions: [decision-id]
reverted_or_rejected_decisions: [decision-id]
experiment_boundary: string
next_action: string
forbidden_actions: [string]
```

确定性 validator 比对 checkpoint。关键字段缺失时，agent 权限降级为 Execution Packet 重新加载；代码修改、提交、部署和新工作派发保持锁定。

### 4.3 Execution Packet

每个 thinker/executor/verifier 只接收当前叶的小包，建议控制在 4-12 KB：

- 项目 revision 和 canonical digest；
- 当前叶目标、状态和队列位置；
- 直接依赖、硬阻塞和 supersedes；
- affected paths 和文件 ownership；
- 允许/禁止的 Product 变更；
- 需要展开的 assertion IDs；
- 验证命令和 live proof 缺口；
- 已证伪假设；
- 唯一 next action；
- artifact refs 与必要片段。

该协议使恢复成本主要由当前叶决定，并解除其与项目总历史规模的线性关系。

### 4.4 任务实体与文档投影

任务采用 revisioned state entity。Markdown 文档用于需要人类阅读的治理与设计对象，边界如下：

- Campaign、Goal、Decision、Experiment、ADR 和阶段报告可以有独立人类文档；
- Work Item、Blocker、Evidence、Claim、Checkpoint 主要是 typed rows；
- Docmost、Obsidian、MASTER 和报告表是这些实体的投影；
- 任务与文档为多对多关系，通过稳定 ID 表达。

```yaml
task:
  task_id: stable-id
  project_id: alkaidlab
  kind: campaign | goal | work_item | experiment | review
  parent_id: stable-id | null
  revision: uint64
  objective: string
  scope:
    repos: [platform, moonlight, sunshine]
    included_paths: [string]
    excluded_paths: [string]
  branch_or_worktree: string | null
  status: pending | active | blocked | completed | reverted
  return_point: stable-id | null
  exit_criteria: [string]
  skill_profile_ref: skill-profile-id@sha256
  execution_packet_ref: artifact://sha256
```

任务识别采用 sticky active leaf 路由器。向量相似度和历史 memory 的权限限定为候选排序：

| 信号 | 权重/权限 | 规则 |
|---|---|---|
| 用户明确给出 Task/Goal/Experiment ID | 最高 | 可生成 switch proposal；写操作前仍做 revision 校验 |
| 当前 claim/lease 与 active task | 默认锚点 | 没有足够反证时保持当前任务 |
| worktree、branch、repo、changed paths | 强证据 | 必须与任务 scope 和 ownership 同时匹配 |
| 当前打开/操作的文件和测试 target | 中强证据 | 用于区分同一 Goal 的子叶 |
| 用户话语中的目标、实体和约束 | 中等证据 | 用于候选排序，不单独授权切换 |
| 代码/文档检索相似度 | 弱证据 | 只能补充理由 |
| Mem0/Hindsight/Graphiti 历史召回 | 最弱 | 生成候选；active task 变更权限为 0 |

切换协议：

1. Router 先判定 `continue / child / interrupt / switch / correction`，并给出可审计的 signal IDs。
2. `continue` 沿用当前 revision；`child` 必须有 parent、return point 和 exit criteria。
3. `interrupt` 或 `switch` 先对原任务滚动 checkpoint，再用 CAS 写入 `task_suspended` 和 `task_activated` 两个事件。
4. 新任务只加载自己的 Execution Packet 和 Skill Profile；旧任务正文、日志和 Skill 不随切换复制。
5. 信号冲突时保持原任务并授予只读检索权限；代码写入、状态更新、提交和部署要求 `active_task == claimed_task == path_owner_task`。
6. 旧任务从 return point 和 checkpoint 恢复。

高置信切换自动完成；低置信切换保持只读。任务误切按生产事故指标管理，副作用数量门槛为 0。

### 4.5 Skill 恢复协议与分层装载

当前 Skill 装载基线：五份触发规则共 47,855 bytes、851 行，统计范围未包含 MASTER、目标态表、代码和官方文档。

| Skill | bytes | 当前问题 |
|---|---:|---|
| `alkaidlab-architecture` | 18,269 | 复制动态架构事实，部分已被目标态取代 |
| `alkaidlab-current-work` | 3,351 | 引用缺失的 MM/AU Goal，并在每次 compact 装载长文档 |
| `alkaidlab-progress-report` | 8,886 | 格式有价值，但要求模型反复读取和重算全量状态 |
| `session-memory-hygiene` | 1,361 | 规则短小稳定，适合保留为协议 |
| `human-first-writing` | 15,988 | 始终启用且示例很多，重复装载成本高 |

47,855 bytes 为可重复测量的原始输入基线。精确 token 数由各模型 tokenizer 和运行 trace 记录。Prompt cache 作用于重复 prefill 费用与延迟；逻辑上下文长度和 stale Skill 风险由分层装载与 validator 控制。

目标态采用四级 Skill 装载：

| 级别 | 内容 | 何时加载 | 目标大小 |
|---|---|---|---:|
| S0 Bootstrap | 状态读取、CAS、副作用门禁、证据优先级、恢复失败行为 | 每次新上下文 | 1-2 KB |
| S1 Skill Manifest | ID、版本、hash、适用 task/repo/path/operation、依赖、冲突、失效条件 | 路由阶段读取元数据 | 每项数百 bytes |
| S2 Compiled Skill Packet | 当前任务真正适用的稳定 rule IDs 和必要原文 | 激活任务、Skill hash 改变时 | 全包 2-6 KB |
| S3 Full Skill/Examples | 完整说明、范例、迁移资料 | 首次进入领域、审计 Skill、packet 校验失败时 | 按需 |

```yaml
skill_manifest:
  skill_id: alkaidlab-architecture
  version: semver
  content_sha256: sha256
  applies_to:
    task_kinds: [architecture, implementation, review]
    repos: [platform, moonlight, sunshine]
    path_globs: [string]
    operations: [design, edit, report]
  stable_rule_ids: [ARCH-LAYER-001]
  state_dependencies: [canonical_plan_digest, architecture_revision]
  requires: [skill-id@version-range]
  conflicts_with: [skill-id@version-range]
  invalid_if:
    - referenced_path_missing
    - canonical_digest_mismatch
    - expired
  compiled_packet_sha256: sha256
```

硬边界：

- Skill 保存稳定方法、门禁、词汇和选择规则；当前 Goal、active leaf、动态架构行和测量结论进入 Typed State/Assertion。
- 每条承重规则有稳定 rule ID、来源、版本和 hash。Checkpoint 同时记录生效 rule IDs 和 Skill 文件标识。
- 路径缺失、版本冲突、canonical digest 不符或失效日期命中时，Skill 进入 quarantine，stale rule 激活权限为 0。
- Skill 修改触发同一组 replay fixtures；进行中任务保持已记录 rule set。
- 当前 Codex 机制要求被选择的 Skill 全文读取。降本措施包括压缩 `SKILL.md` 稳定协议、缩窄触发范围、迁移动态事实，以及按当前任务选择 Skill。

### 4.6 token、一致性、幻觉和代码质量作用机制

各目标采用独立机制和验收指标：

| 目标 | 直接机制 | 可验证结果 | 适用边界 |
|---|---|---|---|
| 减少 token | 小型 Execution Packet、S0-S2 分层 Skill、artifact ref、bounded retrieval、稳定前缀缓存 | 重复输入、重复检索和大日志 prompt 占用下降 | 节省比例由真实 trace 验证 |
| 提高一致性 | revision/CAS、typed reducer、supersedes、sticky task、PostCompact canary | 同一 checkpoint 恢复同一 active leaf/decision/constraint | 新问题推理质量由独立指标验证 |
| 降低幻觉 | 当前证据优先、authority/freshness 过滤、assertion provenance、claim-evidence validator、旧 memory 降权 | 无来源断言、旧事实复活、臆测路径和错误完成声明减少 | 目标为检测、阻断和量化生成错误 |
| 提高代码质量 | scope/ownership、受影响依赖图、项目化 Verification Profile、TDD/build/contract/golden/mutation/live evidence、独立 verifier | 漏测、重复实现、越界修改、回归和返工率下降 | 算法与架构质量由 E7 独立验证 |
| 提高推进效率 | DAG、blocker/return point、attempt budget、promotion gate、唯一 next action | 长期 in-progress、scope drift 和重复追查数量下降 | 真实设备、弱网和性能实验继续按 profile 执行 |

写代码前的 claim-evidence gate 至少检查：

```text
任务是否仍 active
-> affected path 是否在 scope/ownership 内
-> 当前代码/构建是否与 checkpoint digest 一致
-> 承重判断是否有 current code 或官方 assertion 引用
-> 旧 decision 是否已 superseded/reverted
-> verification profile 是否完整
-> 才允许产生副作用
```

该门禁降低生成错误进入代码的概率。代码质量通过盲评补丁、测试/变异分数、返工次数和缺陷逃逸率验证。

### 4.7 Context Control Plane 目标态表

下表使用 `Slot | Module | 说明 | Backend` 视图，命名空间归属独立 Context Control Plane。Platform canonical 架构的变更须经过试点与架构治理。

| Slot | Module | 说明 | Backend |
|---|---|---|---|
| `context.state` | `typed-state-store` | Task/Decision/Evidence/Blocker/Checkpoint 权威机器状态，revision/CAS | PostgreSQL |
| `context.task-routing` | `sticky-task-router` | 识别 continue/child/interrupt/switch/correction；误切时禁止副作用 | deterministic rules + bounded model classifier |
| `context.skill-resolution` | `versioned-skill-registry` | Skill manifest、rule IDs、hash、依赖、冲突、quarantine | Git + PostgreSQL metadata + artifact store |
| `context.composition` | `execution-packet-composer` | 组装当前任务、当前 Skill、当前 evidence 的有界包 | State MCP + RTFM/LSP/SCIP |
| `context.recall` | `candidate-memory-provider` | 提供用户偏好和历史候选 | Hindsight / Mem0 / Graphiti 可插拔 |
| `context.replay` | `checkpoint-canary-validator` | 压缩、切任务、换模型、崩溃后的确定性恢复门 | DBOS；团队阶段 Temporal |
| `context.evidence` | `assertion-resolver` | 行业标准、OS/软件官方文档、当前源码的版本与 provenance | RTFM + content-addressed artifacts |
| `context.observability` | `context-otel` | 记录 token、Skill 装载、检索、切换、恢复、返工和质量指标 | OTel Collector + 可替换后端 |
| `context.presentation` | `docmost-project-graph` | MASTER DAG、Decision Timeline、Evidence Matrix、Context Health | 魔改 Docmost；Obsidian 为生成视图 |

## 5. Thinker / Executor / Verifier 重新分工

| 角色 | 可以做 | 不可以做 |
|---|---|---|
| Thinker | 检索官方来源、提出假设、预注册判据、生成 promotion proposal | 改 active work、把候选当结论、直接提交状态 |
| Executor | claim 一个 work item，按 packet 改代码和运行验证 | 自创新叶、跨 ownership 修改、改预注册判据 |
| Verifier | 独立复核证据、变异测试、判定 criteria 是否命中 | 为保住实现而事后放宽判据 |
| Main controller | CAS 提交状态、处理冲突、更新 MASTER 投影 | 仅凭子 agent 摘要改状态 |
| External model | 第二意见、冲突检查、PostCompact canary 复核 | 进入每次压缩同步热路径、提交权威状态 |

子 Agent 的价值仍然存在，但它解决的是注意力隔离和并行，不解决长期一致性。真正的长期边界是 work claim、revision、ownership、idempotency key 和 checkpoint。

## 6. 组件采用结论

组件采用结论基于源码审查、测试执行和故障注入：

| 组件 | 实测 | AlkaidLab 定位 |
|---|---|---|
| DBOS | 27 个 PostgreSQL 恢复、重试、并发、去重测试通过 | 本地耐久执行首选 |
| Temporal | 20 个 replay、patch、Continue-As-New 测试通过 | 多协作者/跨服务首选 |
| Mem0 OSS 2.0.17 | 152 passed、12 skipped；官方大规模矛盾/时间推理仍弱 | 用户偏好和历史候选；状态提交权限为 0 |
| Hindsight | 345 passed | 带时间的历史召回；状态提交权限为 0 |
| PlugMem | 87 passed、5 failed；故障注入出现静默丢写和 503 后候选永久丢失 | 暂不进入生产门禁 |
| CodeGraph | 2,901 passed；三仓统一索引 10.3s/2.19GB，可串跨仓调用但有同名污染 | 影响线索；完整性由精确检索复核 |
| RTFM 0.26.4 | 609 passed、30 skipped；Alkaid corpus 2.1s/5.5MB | 代码+ADR+官方文档检索；必须加 authority/revision/validity 过滤 |
| LLMLingua-2 | Alkaid canary 480 -> 244 tokens 时仅保留 2/12 | 权威状态压缩权限为 0 |
| Obsidian | 适合 Bases、ADR 和生成视图 | 人类投影视图 |

Alkaid 三仓 CodeGraph 实测：

| 范围 | 文件 | 节点/边 | 冷启动 | 峰值 RSS |
|---|---:|---:|---:|---:|
| Platform | 2,182 | 30,553 / 83,980 | 6.2s | 1.42GB |
| Moonlight | 533 | 10,992 / 23,983 | 1.8s | 0.81GB |
| Sunshine | 468 | 10,638 / 22,482 | 2.5s | 0.80GB |
| 三仓统一父索引 | 3,183 | 52,183 / 135,650 | 10.3s | 2.19GB |

统一索引能把 `alk_rescue_control_evaluate`、Sunshine caller 和 Platform rescue test 串起来；单仓索引看不到外仓调用，统一索引又混入 Moonlight 的同名 `evaluate`/`Host`。固定检索梯队应为：

```text
rg -> Zoekt -> LSP/Serena -> SCIP/CodeGraph -> RTFM 按需展开文档
```

## 7. 动作台账

| 涉及系统 | 当前问题 | 目标 | 动作 | 阶段 | 完成门 |
|---|---|---|---|---|---|
| Skill 控制面 | 动态事实漂移且压缩后反复全文加载 | S0-S3 分层、version/hash/rule ID、quarantine | 重构 | P0 | 不存在路径引用为 0；stale rule 激活为 0；重复 Skill bytes/tokens 达标 |
| 任务路由 | 切换靠聊天语义，压缩后可能从 A 漂到 B | sticky task + signal IDs + switch event + 副作用前校验 | 补全 | P0 | 误切副作用为 0；真实切换恢复完整 |
| MASTER | 566 KB 混合多种状态类型 | 解析为 typed rows，MASTER 继续做人类主视图 | 补全 | P0/P1 | N-42/N-67/N-68 等关系可无损 round-trip |
| 目标态全表 | 与 MASTER 双写、可能漂移 | 从 state 生成状态标记和动作台账 | 重构 | P1 | 同 revision 下两视图一致 |
| ALTP/LocalSend 实验 | 实验发现可直接变成主线 | 独立 Experiment + promotion gate | 补全 | P0 | 未批准实验无法改变 active queue |
| 压缩 | 自动摘要不可审计 | PreCompact checkpoint + PostCompact canary | 补全 | P0/P1 | 关键 canary 100% |
| 多协作者 | 子 agent/人可能并发改同一叶 | claim/lease/CAS/文件 ownership | 补全 | P2 | 并发静默覆盖 0 |
| 参考文档 | 自然语言引用可过期 | assertion ID、版本、hash、validity、supersedes | 补全 | P1 | 每个承重结论有可解析 provenance |
| Obsidian | 投影与权威状态的边界待固化 | 生成式只读 vault | 改进 | P2 | 人工修改经治理入口回流 |
| 外置模型 | 容易进入同步关键路径 | 异步 reviewer 和冲突检查 | 改进 | P3 | 不增加主线提交权限 |
| 代码质量 | 状态恢复与补丁质量需要独立评估 | claim-evidence gate + Verification Profile + 独立 patch 评测 | 补全 | P0-P2 | 缺陷逃逸/返工下降，测试与 mutation 保持基线 |

## 8. Shadow Pilot

### Phase 0：E0-E9 Shadow 验证

首批从 ALTP/ECN/弱网/10Gbps 历史抽取至少 40 个 replay fixtures，固定包含：CL 未授权队列执行、LocalSend promotion、单次 ping 误判、pacer 比较框架错误、N-68 回滚、N-42/N-67 硬门、最终 srtt 误用、实验量具瓶颈。每次模型、prompt、Skill、schema、memory 或检索后端变更都复跑同一 corpus。

| 实验 | 链路 | 对照 | 必须记录/完成门 |
|---|---|---|---|
| E0 当前流程基线 | 复放真实任务；控制面关闭 | 现有自动压缩 + 当前 Skill 全文加载 | input/cache tokens、Skill bytes、恢复时延、重复搜索、旧决定复活、返工次数 |
| E1 Typed State A/B | MASTER 只读解析 -> checkpoint -> 压缩 -> restore | 自由摘要 | latest decision/constraint/open work 100%，旧决定复活 0 |
| E2 任务切换 | continue/child/interrupt/switch/correction 各至少 10 例 | 对话相似度路由 | 切换 precision/recall；误切写入/提交/部署为 0；return point 100% |
| E3 Skill 装载 A/B | 当前全文 Skill vs S0+S1+S2 packet | 47,855-byte 真实基线 | 指令遵循保持基线；stale rule 激活 0；Skill input bytes/tokens、p95 load time 显著下降 |
| E4 Skill 故障注入 | 删除引用路径、篡改 hash、制造版本冲突、换 canonical digest | validator 关闭 | 100% quarantine；旧 Goal/旧架构恢复率 0 |
| E5 检索/token A/B | bounded `rg/LSP/RTFM/SCIP` + artifact refs | 全量 MASTER/日志/文档 prompt | 引用 precision/recall、freshness、重复检索 token；承重断言引用率 100% |
| E6 幻觉对抗 | 注入旧 memory、伪文件名、已回滚优化、过期官方文档 | memory-first agent | 无证据完成声明 0；臆测路径 0；stale decision 0；不确定项进入 evidence request |
| E7 代码质量 A/B | 同一组真实叶，固定模型/预算，盲评 patch | packet/gate 关闭 | build/test/contract/golden/mutation、review defect、rework、scope violation；质量保持基线或提升 |
| E8 多协作者 | 两人/两 agent 同时 claim、切分支、更新同一 blocker | 文件/聊天协调 | 静默覆盖 0；CAS conflict 100% 可见；lease/ownership 可恢复 |
| E9 崩溃与后端故障 | Pre/PostCompact、模型返回后、提交前后 SIGKILL；memory/RTFM 503 | 正常链路 | 重复副作用 0；checkpoint 不丢；recall 故障不影响权威状态；restore p95 达标 |

E0-E9 首先在 shadow mode 记录。E1-E6 安全门与 E7 代码质量门通过后启用执行 veto；E8-E9 通过后开放协作者接入。

### Phase 1：本地生产门

- PostgreSQL + provider-neutral State MCP。
- DBOS 接管 checkpoint、重试和外部副作用幂等。
- 接入 `rg`、LSP、RTFM，CodeGraph 只补充。
- 大日志 content-addressed 落盘。
- PreCompact p95 < 500 ms；state-only restore p95 < 2 s。

### Phase 2：协作者

- Temporal 管理长工作流和 Continue-As-New。
- work claim + lease expiry + expected revision。
- 共享 Zoekt/SCIP/RTFM 服务。
- CI 从 state 生成 Obsidian 视图和报告表。

### 验收指标

| 指标 | 门槛 |
|---|---:|
| 最新决策恢复 | 100% |
| 关键约束恢复 | 100% |
| 未完成工作恢复 | 100% |
| 旧决定复活 | 0 |
| 重复外部副作用 | 0 |
| 并发静默丢写 | 0 |
| state-only restore p95 | < 2s |
| PreCompact 增量提交 p95 | < 500ms |
| 重复检索 token | 至少下降 30% |
| 重复 Skill 输入 tokens/bytes | 相对 E0 至少下降 60%，且规则遵循不退化 |
| 任务误切造成写入/提交/部署 | 0 |
| task return point 恢复 | 100% |
| stale Skill rule 激活 | 0 |
| 承重断言 current provenance | 100% |
| 无证据完成声明/臆测路径 | 0 |
| 代码质量 | build/test/mutation 不退化，scope violation 为 0，返工率低于 E0 |
| affected test 时间 | 至少下降 30%，且漏测为 0 |

## 9. 当前结论与下一步

采用结论：Mem0、CodeGraph、Obsidian 和外置模型均作为受限组件接入。AlkaidLab 的权威执行状态由包含 revision、supersedes、blocked_by、experiment boundary 和证据引用的状态协议管理。

首期范围包括 E0 基线、40 个 replay fixtures、只读 MASTER parser、sticky task router、versioned Skill manifest、shadow checkpoint 和 PostCompact canary。生产 veto 的准入条件为 latest decision/constraint/work/return point 100% 恢复、stale Skill 激活 0、误切副作用 0，以及 E7 代码质量保持基线或提升。

本报告的权限为研究评估。实施产物包括版本化 schema、replay fixtures 和可执行 validator；权威状态由 State MCP 管理。
