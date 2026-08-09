# Skill Orchestration Contract

版本：2  
日期：2026-08-09  
状态：planned  
适用范围：Context Control Plane 的 Skill registry、resolver、provider adapter、项目初始化和协作 workflow

## 目标

Skill 为协作者提供稳定、可复用、可验证的规则资产。编排系统必须在压缩、任务切换、模型切换和协作者交接后保持同一规则合同，并控制装载范围。Skill 不能成为动态任务状态、历史记忆或权威证据的替代存储。

## Skill 来源

| 来源 | 用途 | 默认状态 | 额外门 |
|---|---|---|---|
| Built-in core | 状态提交、证据、恢复、敏感信息和 provider-neutral 基础规则 | active（随版本发布） | 兼容性、回放、故障测试 |
| External | 第三方或组织提供的领域流程 | proposed | license、来源、hash、依赖、冲突和安全审查 |
| Project Profile | 项目目录、构建测试、官方来源和发布约束 | proposed/approved | 项目 owner 审批、实仓验证 |
| User customization | 用户表达、工具偏好和非安全流程习惯 | proposed | 用户确认；不能放宽安全、权限、证据和验收门 |
| Workflow | Thinker、Executor、Verifier、review、handoff 的角色合同 | approved | role/operation 权限测试；无直接状态写权 |

项目初始化时可扫描仓库结构、构建与测试入口、Project Profile、已有治理文档和用户显式偏好，生成可复现的 proposal。Proposal 必须保存输入 artifact refs、生成器版本、候选 manifest hash、变更差异和适用范围；用户或项目治理 owner 批准后才能发布为 active。生成器只能提出候选，不能自动修改 MASTER、Typed State 或产品代码。

## Manifest 合同

每个 Skill manifest 至少包含：`skill_id`、semver `version`、`content_sha256`、`source_kind`、`license_ref`、`applicability`、`rule_ids`、`dependencies`、`conflicts`、`expires_at`、`compatibility`、`provenance_refs` 和 `status`。`rule_ids` 必须稳定；删除或改变已有规则需要新版本、migration 或 replay 说明。manifest、正文和 references 的 hash 必须能从 artifact store 或 Git 复核。

动态字段 `active_task_id`、`idea_id`、`owner`、`claim_id`、`revision`、`blocker`、`checkpoint_id` 和当前 evidence 不得写入 Skill。它们由 State MCP 在组装 Execution Packet 时注入。Skill 的权限表达只能约束允许动作和所需门，不得直接提交权威状态。

## 选择与装载

Resolver 对每次装载输出选择原因、输入信号、manifest hash、rule IDs、冲突处理结果和 resolver 版本。确定性优先级为：

1. 当前 `Task/Goal/Experiment`、active claim 和 path owner 的显式绑定；
2. Project Profile 的 repo、path、operation 和验证配置；
3. role（Thinker/Executor/Verifier/reviewer）与 provider adapter；
4. manifest applicability、依赖、冲突、schema/provider compatibility 和 expiry 校验；
5. 相似度、历史 memory 或模型生成仅用于候选排序，并保留为 candidate。

任何候选都不能绕过 authorization、validator、expected revision/CAS、evidence gate、promotion gate 或任务 claim。无匹配、冲突未解决、过期、缺失、hash/version/digest 不一致时，resolver 返回 quarantine；执行权限降为只读，并要求 replay 或显式 migration。

采用四级渐进披露：

| 级别 | 内容 | 进入上下文的条件 |
|---|---|---|
| S0 | 安全、状态提交、恢复 canary、敏感信息和停止门 | 每次启动或恢复 |
| S1 | manifest、适用性、依赖、冲突、版本和 rule IDs | resolver 选择阶段 |
| S2 | 当前 active leaf 所需的 compiled rules 和短流程 | Execution Packet 组装阶段 |
| S3 | 完整 Skill、references、脚本和示例 | 当前操作确实需要时 |

压缩恢复只加载 S0 与当前 S2；S1/S3 按 resolver 和 bounded lookup 展开。记录每级 token、时延、命中、quarantine 和规则遵循指标，供 E3/E4/E9 验收。

## 外部 catalog 与智能发现

外部来源按 `official -> standard -> verified organization -> marketplace/community` 分级发现。Official 包括 Codex/OpenAI、Claude/Anthropic、GitHub 和 MCP 官方 registry；Agent Skills specification 定义 provider-neutral 最低格式；Skills.sh、MCP 聚合站和 GitHub 社区仓库提供市场候选。市场安装量、stars、相似度和 memory 只影响候选排序，不提升执行信任级别。

Catalog adapter 在项目初始化或显式 rescan 时抓取元数据并固定 `source_url`、`source_revision`、`source_path`、`content_sha256`、`publisher`、`license_ref`、capabilities、applicability 和 verification refs。运行时不从未固定 revision 的远端地址读取 Skill 正文、脚本或 MCP 配置。任何 license 缺失、hash 漂移、publisher ownership 不明、远端副作用未声明或安全审查失败的条目均进入 quarantine。

Resolver 的工作顺序是：预筛选 project/repo/path/operation/role/provider 适用性；输出小型 manifest 摘要和直接来源 URL；通过 authorization、license、provenance、dependency/conflict、hash 和 provider contract 检查；由用户或治理 owner 批准；锁定 revision/hash 后编译 S2 packet。OpenAI 当前文档给出的初始 Skill metadata 列表上限（上下文 2% 或 8,000 字符，以较小有效限制为准）属于 host 事实，控制面必须在 host 装载前完成目录裁剪，不能依赖自动截断。

## 协作工作流

Thinker 产生候选方案、风险和 evidence request；Executor 只能使用已锁定的 S0-S2 packet，在有效 claim、path owner 和 expected revision 下执行；Verifier 使用独立的验证 Skill 和 Verification Profile 检查 evidence、scope、测试和完成门。三者通过同一 revisioned Typed State 协作，任何角色都不能凭 Skill 或聊天摘要直接写权威状态。

任务开始时锁定 `skill_set_digest`、manifest hash、rule IDs、resolver version、provider contract 和 schema version。任务进行中 Skill 变更进入 proposal；若影响已执行步骤或权限，必须建立 checkpoint 并 replay，或走显式 migration。handoff、task switch、model change、compaction 和 provider 故障均需保留可回放的 Skill selection event。

## 生命周期与验收

生命周期为 `discover -> propose -> approve -> lock -> compile -> load -> verify -> quarantine/deprecate -> replay/migrate`。准入与验收至少覆盖：

- 同一输入和 resolver 版本得到相同 manifest/rule 集合；
- 外部 Skill 的 license、provenance、依赖和冲突缺失时 100% quarantine；
- missing path、content hash、version、digest 和 expiry 漂移时 100% quarantine；
- 未经用户或治理 owner 批准的项目初始化或用户定制 proposal 激活率为 0；
- 用户定制不能绕过状态、权限、证据、敏感信息或 promotion gate；
- 进行中任务的 breaking change 被拒绝或可审计地迁移，旧 rule set 可 replay；
- Thinker/Executor/Verifier 的动作权限和最小装载 packet 有 contract、golden、mutation、故障注入和双 provider replay 证据；
- 相对 E0，重复 Skill 输入下降至少 60%，规则遵循、build/test/mutation 和 scope violation 不退化。

详细任务拆分、依赖和生产门位于 [`MASTER.md`](../../MASTER.md) 的 M4、E3/E4 和生产验收章节。
