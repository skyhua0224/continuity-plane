# Agent Harness Assessment

版本：4  
日期：2026-08-14  
状态：research / adoption candidates  
范围：OpenAI Codex、Anthropic Claude Code/Agent SDK、DeepSeek Harness、Pi、Cordis、长任务、Skill、工具、checkpoint、协作、验证与可观察性

```yaml
document_id: context.agent-harness-assessment
document_revision: 4
change_type: evidence
authority_ref: current-source-research-and-software-official-snapshots-2026-08-14
supersedes: context.agent-harness-assessment@3
affected_tasks: [M0-08, M1-05, M2-01, M4-05, M4-07, M5-02, M5-03, M5-05, M5-08, M8-01, M8-06]
next_review: null
```

## 结论

Agent harness 是包围模型的运行、状态、工具、反馈和治理系统。它至少包含任务入口、上下文组装、Skill/工具选择、权限与隔离、checkpoint/replay、副作用处理、验证反馈、多 Agent 编排、可观察性和人类治理。模型与 provider host 属于可替换执行组件；Harness Run 必须绑定同一份 revisioned Typed State、Execution Packet、evidence 和 effect ledger。

OpenAI 与 Anthropic 的公开实践共同支持以下方向：长任务应拆成有完成门的增量叶；仓库需要短入口和渐进披露；应用、日志、指标和测试必须对 Agent 可读；压缩摘要和自由 progress file 无法独立承担权威恢复；宿主 checkpoint、session resume、goal、Skill matching 和多 Agent 功能需要经过 provider adapter 接入控制面。

DeepSeek Harness、Pi 和 Cordis 补充了三类可执行细节。DeepSeek 提供动态 plugin/Skill、session event、compaction、sandbox/approval 和 SQLite 的集成候选；Pi 现行 Coding Agent 提供 cut point、split-turn、hook 和 cache 成本基线；Pi durable AgentHarness 规范提供 operation program counter、effect sandwich 和 crash/race oracle；Cordis 提供动态依赖与组件生命周期模型。上述来源不改变项目级 authority：State MCP 继续独占 revision/CAS、claim/lease、evidence 和 external effect 提交权限。

## 研究来源与快照

结构化来源元数据、检索 hash 和 refresh trigger 位于 [`profiles/reference-catalog.example.yaml`](../../profiles/reference-catalog.example.yaml)。Git 只保存本评估的释义和来源元数据，不复制外部全文。

| 来源 | 当前证据 | 采用边界 |
|---|---|---|
| [Codex Manual](https://developers.openai.com/codex/codex-manual.md) | 2026-08-11 官方 direct snapshot；SHA-256 `633d406edbe14526cb7d1e113db188a7ec3bee188e2b28e4290912d15d214989`；含 long-running goal、AGENTS、Skills、MCP、hooks、subagents、SDK/App Server 和非交互运行 | Provider capability contract；运行时权威仍归 State MCP |
| [Harness engineering](https://openai.com/index/harness-engineering/) | canonical OpenAI URL；当前 direct fetch 返回 403，研究文本经 proxy-render 获取并保持 candidate | 工程模式参考；承重产品事实需 Codex Manual、当前源码或实验补证 |
| [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | 2026-08-09 官方 HTML direct snapshot | 增量任务、初始化、progress 和端到端验证模式；不能直接升级为权威状态设计 |
| [Claude Code documentation index](https://code.claude.com/docs/llms.txt) | 2026-08-11 官方 direct snapshot；SHA-256 `d8174cb72c55130d0200c4448c96d8dcbcd008b93e63ca9655de9206a3471d29` | Claude provider adapter 的当前发现入口 |
| [Agent Skills in the SDK](https://code.claude.com/docs/en/agent-sdk/skills.md) | 2026-08-11 官方 direct snapshot；SHA-256 `81dd0e86f05cca1d91e758ccf0d5b26b1a72efd5eb6646c9a9f163e60723dcb7`；filesystem `SKILL.md`、`settingSources`/`setting_sources`、`skills` 过滤和 init 回执 | Claude adapter surface evidence；官方没有运行时注册 Skill API |
| [Claude Code best practices](https://code.claude.com/docs/en/best-practices.md) | context、verification、subagent、checkpoint、resume 和自动化 | Host 工作方式与项目 Verification Profile 对照 |
| [Claude Code checkpointing](https://code.claude.com/docs/en/checkpointing.md) | file edit rewind、conversation summarize 及 Bash/remote/subagent 边界 | 仅 host recovery candidate；外部 effect 由控制面幂等与 checkpoint 管理 |
| [Claude Code goal](https://code.claude.com/docs/en/goal.md) | 可重复 evaluator、resume 与 non-interactive goal | Goal adapter；完成状态必须由项目 evidence gate 裁决 |
| [Claude Code workflows](https://code.claude.com/docs/en/workflows.md) | 动态 subagent fan-out、pause/resume、saved workflow | M8 编排参考；claim/lease/CAS/path owner 仍由控制面管理 |
| [Claude Code agents](https://code.claude.com/docs/en/agents.md) | 并行 session、subagent、agent team 与 worktree 选择边界 | Provider worker/isolation adapter；项目级 active set 仍归 Typed State |
| [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams.md) | 共享 task list、依赖、self-claim、文件锁、mailbox、plan approval 和本地持久化 | M8 协作 adapter；无项目级 team config，不能替代跨 provider Work Ledger |
| [Claude Code subagents](https://code.claude.com/docs/en/sub-agents.md) | 独立 context、tool/permission 限制、可选 worktree isolation | Thinker/Verifier 与有界研究 adapter；状态提交权限保持 0 |
| [Claude Code advisor](https://code.claude.com/docs/en/advisor.md) | 决策点调用第二模型；每次读取完整 conversation | bounded reviewer candidate；调用时机模型驱动且上下文成本高 |
| [Claude Code worktrees](https://code.claude.com/docs/en/worktrees.md) | session/subagent 文件隔离、resume 与 cleanup | workspace adapter；Git 隔离不能替代 claim、effect 和 completion gate |
| [Agent SDK hosting](https://code.claude.com/docs/en/agent-sdk/hosting.md) | subprocess/local state、session pattern、tenant isolation、OTel 和生产边界 | Provider runtime adapter 与 isolation fixture |
| [Agent SDK session storage](https://code.claude.com/docs/en/agent-sdk/session-storage.md) | transcript mirror、best-effort write、post-compaction chain 和 retention | Transcript continuity；不能作为 typed state/event log |
| [Agent SDK tool search](https://code.claude.com/docs/en/agent-sdk/tool-search.md) | 大工具集按需发现、上下文与准确率边界 | M4/M6 resolver 对照；工具授权独立校验 |
| [Agent SDK observability](https://code.claude.com/docs/en/agent-sdk/observability.md) | model/tool/hook OTel、token/cost 与 trace propagation | 映射到 `context.*` trace；beta 字段需 provider version 约束 |
| [Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions.md) | allow/deny、mode 与 hook 权限判定 | Provider permission adapter；不能放宽 State MCP authorization |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | commit `47f943859bef60e4160492346772ded9b24f765a`、tree `f904efab9ef435201d6ba4da88a34d6366568272`、`0.1.0-rc.5`、MIT；developer preview；该 commit 的 sandbox checks 通过且 `e2e` check 失败 | M4/M5 implementation candidate 与 M8 runnable fixture；采用前固定版本并通过本项目 replay、authorization 和 failure gates |
| [How Compaction Works in Pi](https://earendil.com/posts/compaction-in-pi/) | 2026-08-14 direct snapshot；SHA-256 `af529b36af20560837631b3c4c3681ee5d409d849474a380860690b08d448bc4`；文章源码 commit `47610217098d9ba8f22d223fa7c1413f9f5fd759` | M5 cut point、split-turn、hook、usage/cache baseline；plain-text summary 只作 host provenance |
| [Pi source](https://github.com/earendil-works/pi) | current commit `9d2ec7ffabe927bfad2214c1cee25b6632a78dcf`、tree `9108e8903e1ba009dac694ff8ad6289b8673b1eb`、MIT；文章 commit 到当前 main 的选定 compaction 源码与测试无差异 | 现行 compaction 可作 runnable baseline；durable AgentHarness 作为规范/test oracle，公开核心操作仍抛 `HarnessNotImplemented` |
| [Cordis paper](https://github.com/cordiverse/paper/blob/main/paper.pdf) | commit `948a07b369c62adb3b12e102458be5c18dfb69b9`、tree `9843926bd597bf184536fe9b2961bcc77f245bb6`；2026-08-13 draft、88 页；PDF SHA-256 `4d48478dc0b6222d9f74d7db10ee776449b1209eb112632336544d32a49db97f`；repository 无 license 文件 | M4 dynamic composition 与 lifecycle research；license 未决时只保留 citation 和最小释义，不复制论文或实现 |

## Harness 能力模型

| 能力 | 输入 | 输出 | 本项目权威对象 |
|---|---|---|---|
| Intent 与 task routing | 用户消息、active task、Idea、blocker | continue/child/interrupt/switch/correction proposal | Task/Idea/Event revision |
| Context composition | task、decision、constraint、evidence、Skill refs | 有界 Execution Packet | Packet hash + Checkpoint |
| Skill 与工具 resolution | operation、role、provider、path、claim | 最小 S0-S2 rule/tool set | Skill manifest、rule IDs、authorization |
| Execution 与 isolation | packet、workspace、credentials、sandbox | tool calls、diff、artifacts | Harness Run + path owner |
| Durable workflow | checkpoint、lease、retry、effect key | replayable step/result | Event Log、Effect、DBOS/Temporal |
| Verification feedback | completion gate、fixture、build/test/live profile | pass/fail、evidence、review findings | Evidence/Assertion/Verification Gate |
| Collaboration | DAG、claim、lease、owner、revision | handoff、conflict、promotion | Typed State + CAS |
| Observability | model/tool/hook/workflow events | token、latency、cost、failure、rework trace | OTel `context.*` + state revision |
| Human governance | MASTER、approval、correction、promotion | governance decision | Git governance + controlled State MCP action |

完整 harness 是上述能力的组合，不对应单一 SDK、聊天会话、Skill 集合或 workflow engine。Codex、Claude Code、Claude Agent SDK、未来 provider 与本地模型通过 adapter 提供模型调用、host tool 和 session 能力。

## OpenAI 实践映射

OpenAI Harness Engineering 的候选结论包括：

- 短 `AGENTS.md` 作为导航入口，结构化 `docs/`、执行计划、质量文档和代码提供渐进披露；
- UI、DOM、截图、日志、指标和 trace 对 Agent 可访问，使验证条件可以由 Agent 直接执行；
- 架构依赖、日志、命名、文件大小和平台规则通过 lint/structural test 机械执行；
- review feedback、失败与人类判断持续转化为文档、工具或可执行门；
- recurring documentation/quality maintenance 检测知识和代码漂移；
- Agent 运行可以持续数小时，但其可靠性依赖仓库结构、反馈回路和可验证环境。

Context Control Plane 采用短入口、知识索引、可执行计划、机械门、应用可观察性和持续 freshness review。该实践中以 repository knowledge 作为系统记录的表述在本项目中需要权限拆分：MASTER 与 versioned docs 具有治理权威，Typed State/Event/Checkpoint 具有运行时机器权威，外部文档和 memory 提供 evidence/candidate。

Codex Manual 的当前 host 能力进一步支持：

- Goal 由 outcome、constraints 和 verification 构成，可在同一 task 中持续 steering；
- 并行 task 应隔离上下文与写入目录，避免多个会话修改同一来源；
- AGENTS、Skill、Plugin、MCP、Hook、SDK/App Server 和非交互运行具有不同作用域；
- Skill 和工具的渐进发现降低初始上下文成本；
- Subagent 可隔离研究上下文，但仍继承宿主权限和沙箱边界。

这些能力通过 Codex adapter 使用。Goal 和聊天连续性不能替代 Task revision；host Skill 触发不能替代 M4 resolver；subagent 状态不能替代共享 claim/lease；host approval 不能替代 effect authorization。

## Anthropic 实践映射

Anthropic 的 long-running harness 实验观察到压缩仍可能导致中途实现和早停。其 initializer/coding-agent 方法采用结构化 feature list、单 feature 增量工作、progress file、Git 历史、启动脚本和端到端浏览器验证。Context Control Plane 保留这些可复用属性，并做以下转换：

| Anthropic 实验对象 | 控制面转换 |
|---|---|
| feature list JSON | Campaign/Goal/Work DAG + project Verification Profile |
| progress text | Typed State + append-only events + current STATUS projection |
| Git commit recovery | immutable checkpoint + artifact hash + effect ledger；Git 仍保存代码边界 |
| initializer prompt | Project Profile bootstrap + source/Skill proposal + S0 canary |
| one feature per session | claimed active leaf + attempt budget + completion gate |
| browser self-test | project-specific live/golden/loopback evidence |
| next agent reads logs/progress | state-only restore + bounded artifact expansion |

Claude Code 当前文档暴露了关键限制：context compaction 可能丢失早期详细指令；file checkpoint 不覆盖 Bash、远端 API、数据库、部署、外部修改和部分 subagent edit；Agent SDK SessionStore 采用 local-first transcript mirror，失败重试后可以丢弃 batch 并继续运行；返回链以 post-compaction transcript 为主。这些限制支持本项目的独立 typed state、append-only effect、PreCompact/PostCompact canary 和 503/SIGKILL 故障注入。

Claude 的 goal、hooks、dynamic workflows、tool search、permissions 与 OTel 可以分别映射到 task adapter、lifecycle hook、M8 orchestration、M4/M6 resolver、authorization adapter 和 `context.*` trace。Provider beta 字段、model threshold 和 host default 必须带版本并可 quarantine。

Claude agent teams 的共享 task list 支持 pending/in-progress/completed、依赖、自领任务与文件锁，mailbox 支持 Agent 间消息；task list 在本机保留以供 resume。当前官方文档同时明确：不存在项目级 team config，teammate 不继承 lead conversation，队友默认继承 lead 权限，plan approval 可由 lead 自主授予，同文件编辑仍可能覆盖。Agent teams 因此只提供单个 provider host 内的协作候选状态。控制面 adapter 必须把 provider task 映射到项目级 `active_work[]`、Work Ledger、claim/lease、scope owner、expected revision、effect key 和 evidence gate；未成功映射的 host task 没有副作用权限。

Subagent 适合把检索、日志和复核隔离在独立 context，并通过 tool allowlist、permission mode 和可选 worktree 约束动作。Advisor 适合高风险决策点复核，但每次调用重新读取完整 conversation，调用时机由模型决定。两者均不得直接提交决定、完成状态或 promotion。

## DeepSeek、Pi 与 Cordis 映射

DeepSeek Harness 的 `everything is a plugin` 架构通过 Cordis plugin tree 组合模型 adapter、工具、session log、agent loop、sandbox、approval、persistence 和 telemetry。Durable `SessionEvent` 与 live `agent/*`/capability events 分离，profile/bundle/patch 提供分层组合，Skill catalog 支持热刷新。该结构适合 M4-07 的动态候选 catalog、M5 compaction adapter 和 M8 runnable failure fixture。其 session log 是 model-visible context 的重建来源，在本项目中映射为 provider provenance；Task、Decision、Effect 和 completion 仍提交到 State MCP。

Pi 现行 Coding Agent 在 turn 结束后检查自动压缩，也能在 overflow 时 mid-turn 压缩。它按 token budget 选择 cut point，保留近期消息，以独立模型调用生成结构化 summary，并通过 compaction extension 暴露替换点。压缩改变 prompt prefix，首次 post-compact request 会失去大部分既有 KV cache 命中。M5-02/M5-03 采用其 hook、cut point 和 split-turn fixture，M5-05 对照 token、cache invalidation 和阈值；summary 不承担 decision、constraint、work 或 effect watermark 的权威恢复。

Pi 的 durable AgentHarness 文档定义 total `op.state/{operationId}` program counter、intent/effect/settlement 两侧提交、预留 entry/usage/result IDs、工具 `safe|never` replay、terminal register cleanup 和分层 crash/race matrix。这些合同适合作为 M5-08 与 M8-01/M8-06 的 protocol/test oracle。当前 `packages/agent/src/harness/agent-harness.ts` 只完成类型、配置和 session scaffold，`prompt`、`compact`、`resume`、hook/event 注册等核心公开操作仍返回 `HarnessNotImplemented`；计划不能把规范状态标记为可运行依赖。

Cordis 论文将 temporal composability 建模为 context transformation 与 runtime-tracked inverse，将 spatial composability 建模为 reactive coeffect，并组合为动态 component lifecycle。M4 使用这些概念描述依赖满足、注册、卸载和热替换。论文的正确性前提要求 effect 可表示为 context transformation 且 inverse 由实现提供；数据库写入、网络请求、部署和其他外部 effect 继续使用授权、幂等键、effect ledger 与显式 compensating action。Cordis repository 缺少 license 文件，代码和论文全文均不进入项目分发物。

## 当前项目角色实证

AlkaidLab Platform 当前 `HEAD` 为 `d50bfe6f22efcad528134baa8b8d612ea07ec147`；`docs/authority/network/weak-network-injection.md` blob 为 `b7d0b7ceaafb67b1e30c690458d5258d61b74347`。该文件第 3 行记录 `netem-thinker` 为 read-only、无写权限，由主控落盘。此证据证明项目内已经使用 Thinker 只产出候选、主控承担写入与验证的角色分权。

该先例通过 Harness Role adapter 复用：Thinker、Executor、Verifier 是 tool grant、claim 和提交权限的组合。Thinker 保持只读；Executor 需要 valid claim 与 scope owner；Verifier 检查 evidence 与 completion gate且权威状态写权限为 0。单个仓库文件中的角色说明不构成跨项目或跨 provider 的共享状态协议。

## 项目级协作与交付速度

项目级 `active_work[]` 是同一 state revision 下所有 active claim 的集合。Provider task list、Issue、PR、分支、个人 `My Work` 和聊天摘要都是 source/projection；它们不能单独证明某项工作无人认领、已经完成或可以产生副作用。新 claim 前必须查询 proposed、active、verifying、completed、rejected、reverted 和 superseded Work，避免协作者因看不到他人状态而重复实现。

并行 worker 数量不等于交付速度。Agent teams 的 token 成本随活跃 teammate 增加，同文件和强依赖任务会增加协调与返工。控制面仅统计通过 completion gates 且 safety veto 为 0 的 accepted work，按相同 task class 与 measurement source 比较 lead time、cycle time、blocked time、rework、time-to-first-durable-artifact 和 accepted throughput。少于三个可比较样本时保持 baseline，不声明改善。

## Adopt、Adapt 与 Quarantine

| 状态 | 模式 | 理由 |
|---|---|---|
| adopt | 可验证完成门、单叶增量、短导航入口、渐进披露、应用/日志/指标可读 | 与 E0-E9 和 Project Profile 一致 |
| adopt | 独立 reviewer、隔离 workspace、结构化输出、OTel trace、持续 doc/quality freshness | 可形成机械证据和恢复数据 |
| adapt | progress file、Git log、provider goal、session resume、host checkpoint | 转换为 Typed State/Event/Checkpoint 后使用 |
| adapt | host implicit Skill/tool search 和 subagent workflow | 由 applicability、authorization、claim、hash 和 CAS 包裹 |
| adapt | DeepSeek plugin/Skill/session/compaction/sandbox capability | 固定 preview revision 后进入 shadow adapter；provider session event 无 State authority |
| adapt | Pi compaction hook、cut point、split-turn 与 cache accounting | 作为 M5 runnable baseline；结构化 summary 不能独立通过 canary |
| adapt | Pi durable `op.state`、effect sandwich、reserved ID、replay 和 cleanup contract | 作为 M8 protocol/test oracle；实现完成度由当前源码与 fixture 重新判定 |
| adapt | Cordis reactive dependency 与 reversible registration | 仅用于受 runtime 管理的 component context；license 未决时 reference-only |
| quarantine | mutable URL runtime loading、auto memory 当前事实、未经审查的 marketplace Skill/MCP | provenance、漂移和权限风险未通过 |
| quarantine | Cordis disposer/inverse 直接声明外部 effect 已撤销 | inverse 前提不覆盖未纳入 context transformation 的外部系统 |
| reject | compaction summary 作为唯一 handoff、多个 Agent 共享写目录、无 effect key 的 retry | 无法满足 E1/E8/E9 veto 门 |
| reject | 仅凭 PR/分支/progress text 判断 owner、active task 和完成状态 | 无共享 revision、lease 和 evidence gate |

## Provider-neutral Harness Run

每次执行至少绑定：

```yaml
run_id: stable-id
project_id: stable-id
task_id: stable-id
task_revision: uint64
claim_id: stable-id
provider: codex | claude | other
provider_contract_version: string
execution_packet_sha256: sha256
skill_set_digest: sha256
tool_grants: [capability-id]
checkpoint_id: stable-id
effect_high_watermark: uint64
verification_profile_id: stable-id
reference_validity_watermark: uint64
trace_id: otel-trace-id
status: proposed | running | waiting | verifying | completed | failed | quarantined
```

Run 只能读取与 active claim 匹配的 packet，并在 expected revision 下提交 event/effect。Provider session ID、transcript 和 host checkpoint 保留为 provenance；它们不拥有 task completion、latest decision 或 side-effect authority。

## 对持续工作与突发 Idea 的影响

持续工作由 goal/task completion condition 驱动，每个回合都从同一 active leaf、checkpoint 和唯一 next action 恢复。用户新输入先按 [`idea-continuity.md`](../architecture/idea-continuity.md) 分类：普通 Idea capture-and-continue；correction 触发 supersedes 与写保护；明确 interrupt 先 checkpoint 再切换。参考资料扫描和新框架发现使用相同规则，默认形成 research/adoption candidate，不抢占 active task。

压缩后恢复顺序固定为：验证 MASTER digest 与 task revision、恢复 active leaf/return point/effect watermark、验证 current evidence 和 Skill digest、运行 canary、开放副作用、最后展开相关 Idea 或新参考候选。历史 memory 和 provider summary 只辅助定位来源。

## 计划映射与实验

| 计划 | Harness 交付 |
|---|---|
| M0-07/M0-08 | schema/version governance、reference lifecycle 和候选 catalog |
| M2 | Task/Idea/Event/Effect/Checkpoint/Harness Run typed schema 与 State MCP |
| M3/M5 | sticky routing、Idea return、Execution Packet、Pi/DeepSeek compaction adapter、Continuation Cursor 和 canary |
| M4/M6 | Skill/MCP/tool resolver、DeepSeek/Cordis dynamic composition candidate、provider adapter 和 bounded discovery |
| M7 | assertion validity、Verification Profile、同预算 A/B 与 freshness watcher |
| M8 | DBOS/Temporal、claim/lease、DeepSeek runnable fixture、Pi durable protocol oracle、provider-neutral Harness Run 和 OTel |
| M9 | Project Graph、Evidence Matrix、Context/Reference/Harness Health 与治理入口 |
| M10 | AlkaidLab 与第二项目的 Codex/Claude shadow pilot |

Harness 评估使用 E0-E9 veto 门。新增对照至少覆盖：no-harness/free-summary、provider-native-only、control-plane packet、control-plane + provider host optimizations。指标包括关键状态恢复、旧决定复活、return point、重复 effect、CAS conflict、reference freshness、Skill/tool 输入、token、restore p95、build/test/mutation、scope violation 和返工。

## 当前采用边界与下一验证

M0-07 schema governance、M1-04 replay corpus、M1-05 E0-E9 contract coverage、M2 State MCP/StateStore 和 M4-05 本地双 provider delivery-plan replay 已提供版本化证据。DeepSeek、Pi 和 Cordis 当前只进入 reference catalog 与主计划完成门；真实 provider 进程、token/cache、规则遵循、压缩恢复以及 M8 workflow 故障注入仍未实现。

1. M4-07/M4-09 建立五类 Skill catalog，并以 DeepSeek/Cordis candidate 验证动态依赖、license、provenance、quarantine 和 resolver。
2. M3/M5 建立 sticky routing、Execution Packet、PreCompact/PostCompact canary 和 context return，并运行 Pi/DeepSeek shadow compaction 对照。
3. M7/M8 运行真实 provider tokenizer/live A/B、accepted delivery speed、Pi effect-sandwich crash matrix、DeepSeek checkpoint、SIGKILL/503、checkpoint 损坏和并发写故障注入。
4. M8/M9 验证项目级 `active_work[]`、Work Ledger、provider task mapping、冲突恢复和同 revision 人类视图。
