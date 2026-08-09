# Project Work Governance

版本：2  
日期：2026-08-09  
状态：current architecture contract

```yaml
document_id: context.project-work-governance
document_revision: 2
change_type: schema
authority_ref: M2-07-project-governance-profile-acceptance
supersedes: 1
affected_tasks: [M1-05, M2-01, M2-07, M3-04, M8-02, M8-07, M9-02, M10-01]
next_review: null
```

## 权威对象

`MASTER.md` 只有一个项目级治理实例。它保存项目使命、范围边界、长期 Workstream DAG、架构归属、决策权限和 promotion gate。个人视图不得使用 `MASTER` 命名。

| 对象 | 内容 | 权威边界 | Git 默认 |
|---|---|---|---|
| Project MASTER | 使命、边界、Workstream、约束、完成门 | governance authority | tracked |
| Project Work Graph | Work、依赖、readiness、claim、lease、owner、revision | Typed State runtime authority | generated projection only |
| Project STATUS | active work set、blocker、冲突、next actions、state revision | 共享恢复投影 | tracked at durable checkpoints |
| Workstream Plan | 一个长期领域或 Campaign 的共享详细计划 | MASTER 引用的治理子合同 | tracked when shared and durable |
| `My Work` | 当前 actor 的 claim、Execution Packet、return point 和消息 | 个人派生视图；提交权限为 0 | local/UI only |
| Issue、PR、外部 PM task | task source、讨论和 Git 集成事实 | source adapter authority；不能直接授予运行时副作用权限 | external system |

Project MASTER 不枚举所有个人步骤、会话待办和 Issue。共享 Workstream Plan 使用稳定 ID 由 MASTER 引用；短期执行步骤进入 Typed State、Issue provider 或 Execution Packet。

## 独立配置轴

项目 Profile 分别声明以下字段：

```yaml
direction_state: discovery | governed | operational
governance_owner_mode: single-owner | multi-owner
execution_worker_mode: single-worker | multi-worker
repository_topology: modular | monolith | mixed
task_sources: [master-workstream | issue-backed | state-native | external-pm]
requested_runtime_profile: local-embedded | forge-coordinated | local-coordinator | shared-strong
```

`governance_owner_mode` 决定 MASTER、promotion 和高风险决定的批准策略。`execution_worker_mode` 决定 claim、lease、path ownership 和并发冲突门。两个字段独立：单人治理项目可以同时运行多个 AI 或临时协作者；多人治理项目也可以在某个阶段只允许一个执行 worker。

`repository_topology` 只选择 scope resolver，不改变协作协议或权限。模块化仓库可以优先使用 module 与 capability ownership；非模块化或混合仓库使用 repo、directory、file、symbol、capability 与 effect scope。模块边界不构成准入条件。初始化器从当前目录、build graph、符号索引和项目 Profile 生成候选 scope，owner 批准后才能进入 claim。

单人到团队的迁移保持同一 Project、Work、Event、Claim 和 Evidence schema。迁移只增加 owner、approval、lease、tenant/project isolation 和 task-source adapter 配置。

`requested_runtime_profile` 只声明部署意图，不授予一致性、共享、claim、lease 或离线写保证。实际运行保证只能来自 M2-08 StateStore capability manifest，并由 State MCP receipt 暴露；M10-09 在 profile 激活时校验请求模式与 adapter manifest。默认值为 `local-embedded`；Git forge、PostgreSQL 和本地协调器按项目条件显式启用，不改变 Project、Work、Event 或 Evidence 的 core schema。

Project Profile、Project Charter 和 Work obligation 的 `revision` 分别属于各对象的 monotonic revision domain，不构成父子全局序号。只有字段合同显式引用 `profile.revision` 的 proposal、activation、CAS 或 claim 才与 Profile revision 比较。跨对象一致性由 State MCP expected revision、Event 和 validator 提交门建立。

## Direction Lifecycle

`direction_state: discovery` 不要求完整路线图。初始化器生成最小 Project Charter，字段包括：

- problem space 与预期用户；
- 已确认约束、权限边界和禁止副作用；
- current code、Issue、PR、文档和外部来源证据；
- 明确未知项、待验证假设和决策 owner；
- discovery campaign、attempt budget、expiry、return point 和 exit criteria。

AI 根据仓库、Issue、PR、近期提交和用户输入生成 Workstream proposal。proposal 在批准前保持 candidate；bounded experiment 使用 `mainline_authority: false`。证据与治理决定通过 promotion gate 后将 direction state 推进到 `governed`。构建、发布、协作和恢复门具备生产证据后进入 `operational`。

初始化器不得根据 commit 频率、Issue 标题或聊天摘要自行生成已批准路线图。来源冲突时创建 correction proposal，并保留当前治理状态。

## Task Sources

每个 Work 保留来源映射：

```yaml
work_id: stable-id
source_kind: master-workstream | issue-backed | state-native | external-pm
source_ref: opaque-ref
source_revision: string
governance_parent_id: stable-id
dependencies: [work-id]
readiness: proposed | blocked | ready | active | verifying | completed | rejected | superseded
```

Issue provider 负责同步 Issue 标题、正文、标签、外部状态和 source revision。Typed State 负责 active、claim、lease、path owner、effect authorization、verification 和 completion。禁止把完整 Issue backlog 复制进 MASTER；MASTER 只引用相关 Workstream、治理决定和完成门。

外部来源更新使用 expected source revision。Issue 关闭、PR merge 或 commit 出现只形成 completion candidate；项目 Verification Profile 和 evidence gate 通过后才能提交 completed state。

Work obligation 的 terminal status 必须保留裁决依据：`satisfied` 至少引用一个 `evidence://`，`waived` 至少引用一个 `approval://`，`expired` 绑定已到期的 `expires_at`。Typed State 或 Event 中缺少这些依据时，unattended dispatcher 不能把 Work 判定为完成。

## Project Adaptation

ProjectAdaptation 保存项目、用户或 provider adapter 范围的候选优化。可变生命周期字段与内容字段分离；内容 hash 绑定 profile、SemVer 2.0.0 version、proposal revision、scope、inputs、applicability 和 changes。path、repo、operation、user 与 provider applicability 使用对应 typed ref；候选只能修改 retrieval order、typed path/command/verification refs、Skill applicability 和允许的 presentation preference。authority、active task、claim、gate、retention 和副作用权限不在适应范围内。

`proposal_revision` 记录候选产生时已存在的 profile revision。`activation_revision` 记录 promotion 提交生效的 profile revision；active 和 superseded snapshot 必须满足 `proposal_revision < activation_revision <= profile.revision`。后续 profile revision 不改变已激活对象的 activation provenance。

`rollback_to` 引用同一 normalized scope/applicability lane 中更早且曾激活的 SemVer。该引用只提供历史内容来源，不直接授予激活权限。每次 rollback 都生成新的 candidate，并重新执行 replay receipt、current safety veto、approval、expiry 和 activation revision gate。过期或已 quarantine 的历史版本不能绕过新 candidate 的 current gates。

Liveness validator 使用调用方注入的 `observed_at` 检查 profile、charter、obligation、approval 和 adaptation expiry。只有 State MCP 或授权 host adapter 提供的受信时钟可形成完成门证据；provider、Skill、聊天输入和 ProjectAdaptation 自带时间的直接调用结果保持 candidate，状态提交权限为 0。

## 并行激活

Work 的编号和文档位置不构成执行顺序。满足以下条件时，后续 Work 可以并行激活：

1. dependencies 和 required decisions 已满足；
2. scope、capability ownership 和 path ownership 与 active work 不冲突；
3. actor 取得有效 claim/lease；
4. expected state revision 与 source revision 当前；
5. Verification Profile、资源预算和 return point 已绑定。

依赖未满足但探索有价值时创建 Experiment。Experiment 绑定 parent、scope、return point、attempt budget、expiry、exit criteria 和 promotion target，默认无 canonical queue 修改权限。

## 重复工作检测

Typed State 中的共享 Work Ledger 保存 proposed、active、verifying、completed、rejected、reverted 和 superseded Work，以及 owner、claim、scope、source revision、effect watermark 和 evidence refs。只存在于个人聊天、个人文档或本地分支的工作对其他协作者不可见，不满足共享协作完成门。

新 Work 或 claim 创建前，resolver 查询 Work Ledger，并联合检查 capability、path、symbol、Issue、PR、commit 和 evidence refs。语义检索产生 overlap candidate；稳定 ID、source revision、path/capability ownership 和 current code evidence 裁决冲突。

确认重叠后，系统返回现有 Work、owner、状态和证据，并阻止第二个未协调 claim。允许的后续动作包括加入现有 Work、创建 child Work、登记替代方案或等待 lease 释放。离线 outbox 在同步时通过 CAS 显式返回冲突，静默覆盖和重复外部副作用保持为 0。

## 投影与 Git 边界

共享 Project MASTER、Project STATUS、目标态架构全表、已采用 Workstream Plan 和最小 Project Profile 进入 Git。Project STATUS 由同一 state revision 生成，可以包含多个 active work；单 worker 界面可以突出 primary work，但不得丢失其他 active claim 和 blocker。

`My Work`、个人偏好、Provider session、Execution Packet cache、本地 checkpoint、临时 worktree 和未脱敏证据保留在本地或受控状态服务。默认 Git ignore 包括 `STATUS.local.md`、`MY_WORK.md`、`.context-control-plane/local/` 和 Provider worktree 目录。

## Project Profiles

| 项目形态 | Profile | MASTER 使用方式 | Task source |
|---|---|---|---|
| 单人、单 Agent | single-owner + single-worker | 最小 Charter 与 Workstream | state-native 或 Issue |
| 单人、多个 AI/临时协作者 | single-owner + multi-worker | 共享方向和门；每个 worker 独立 claim | MASTER、Issue、state-native |
| 多人团队 | multi-owner + multi-worker | 审批策略、领域 owner 和 promotion | Issue、外部 PM、state-native |
| 方向探索项目 | discovery + 任意 owner/worker 模式 | Charter、约束、未知项和 discovery gate | proposal、Issue、experiment |

AlkaidLab Platform 是未来的模块化替代项目，可采用 single-owner + multi-worker。ProjectCompute 可采用 issue-backed task source，个人 current work 迁移为 `My Work`。Foundation Sunshine 当前为非模块化仓库，预计收敛为 Platform 下的一个 Product；在 Platform 完工前仍使用相同的 Work Ledger、claim/lease、CAS、evidence 和 verification 合同，并通过 repo、directory、file、symbol、capability 与 effect scope 管理并行协作。方向未确认时采用 discovery，协作者先执行受约束的 discovery Work 和 Experiment。

## 交付映射

| 阶段 | 交付 |
|---|---|
| M1 | issue-backed、并行 ready work、path conflict 和 duplicate-work fixtures |
| M2 | WorkSource、Project Charter、active work set、Claim 和 Project Profile schema |
| M3 | readiness、并行激活、duplicate resolver、Idea/Experiment promotion |
| M6 | capability/path/symbol/Issue/PR/commit overlap retrieval |
| M8 | lease、CAS、offline outbox、multi-owner approval 和 provider adapter |
| M9 | Project STATUS、My Work、Work Graph、冲突和 promotion 控制台 |
| M10 | solo、assisted-solo、team 和 issue-backed 项目初始化/升级 replay |

## 验收

- 单 owner 与多 owner Profile 使用同一 core schema，升级 replay hash 一致；
- Project MASTER 数量为 1，个人 `MASTER` 文件数量为 0；
- Issue-backed 项目 backlog 重复存储率为 0，source revision 映射完整率为 100%；
- 多个 ready Work 在无 ownership 冲突时可以并行 claim；
- duplicate Work 在第二个 claim 或 effect 前拦截率为 100%；
- modular、monolith 和 mixed fixture 使用同一 Work/Claim/Event core schema，scope overlap 结果可重复；
- 未批准 discovery proposal 改变 canonical queue 的次数为 0；
- Project STATUS active work、claim 和 blocker 与同一 state revision 一致；
- personal/local projection 的 Git admission 为 0。
