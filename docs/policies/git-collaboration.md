# Git Collaboration Policy

版本：2  
日期：2026-08-09  
状态：implemented offline admission contract

## 目标

Git 是协议、schema、代码、脱敏 replay fixture、项目 Profile 和治理文档的审计与集成边界。运行时 Typed State、Event Log、claim、lease、checkpoint、effect watermark 和 State MCP revision 保持机器权威；commit、branch 和 PR 记录这些对象的变更证据，不替代运行时授权或恢复状态。

本策略吸收 AlkaidLab Platform 的分层归属、契约与质量门要求，以及 ProjectCompute 的 `main` 分支、英文 Conventional Commit、PR 前质量门和 regular merge 约束。控制面增加 provider-neutral provenance、claim/ownership、return point、evidence 和 promotion gate。

## 分支合同

canonical 分支为 `main`。新分支从最新 `main` 创建，并绑定以下受控元数据：

```yaml
branch_id: stable-git-ref
parent_id: work-or-idea-id
scope: repository-path-or-contract
return_point: stable-id
exit_criteria: [gate-id]
attempt_budget: uint32
expiry: rfc3339 | null
promotion_target: mainline-queue-id | null
mainline_authority: false
```

推荐分支名：

```text
work/<work-id>/<slug>
experiment/<work-id>/<slug>
fix/<work-id>/<slug>
docs/<work-id>/<slug>
```

`<slug>` 使用 ASCII lowercase kebab-case。并行 patch 发生命名冲突时追加短 opaque actor ref，例如 `work/M1-05/e9-faults--a7k`；禁止放入 provider 原始 thread ID、本机路径、密钥或用户隐私。分支名不授予 claim，也不能证明任务归属；副作用必须同时满足 active task、有效 claim、path owner 和 expected revision。

实验分支默认 `mainline_authority: false`。实验发现必须绑定 parent、scope、return point、exit criteria、attempt budget、expiry 和 promotion target；promotion gate 通过后才可进入 canonical queue。

## Commit 合同

提交标题使用英文 Conventional Commit，首行保持 50-72 个字符：

```text
<type>(<scope>): <imperative lowercase description>
```

允许的 type 为 `feat`、`fix`、`docs`、`test`、`refactor`、`perf`、`chore`、`build`、`ci`。跨模块变更可省略 scope；局部变更使用 `replay`、`schema`、`state`、`routing`、`skills`、`retrieval`、`harness`、`governance`、`docs` 或 `ci` 等稳定 scope。标题不使用句号，不写 provider attribution 或模型生成水印。

需要解释动机或验收边界时使用 body：

```text
feat(replay): admit sanitized M1-04 corpus

Why: Preserve real compaction and task-drift failures as replayable regressions.

Task: M1-04
State-Revision: 12
Evidence: docs/migrations/m1-04-replay-fixture-acceptance-2026-08-09.md
Tests: python -m unittest discover -s tests
```

一个 commit 表示一个可验证的原子变更：契约字段、实现、对应测试和必要文档应保持同一语义闭环。Idea capture、临时状态心跳、纯 checkpoint 和未完成实验不单独制造 commit；它们进入 event/checkpoint，或与最近的可验收 artifact 一起提交。每个完成叶通常产生 1-3 个 commit；拆分依据依赖、验证门和回滚边界，不依据聊天消息数量。长时间工作至少在每个可恢复的 durable artifact 完成后形成一个本地 commit，避免无界未提交窗口。

commit 前必须执行：

1. 当前 work/claim/path-owner 与变更范围一致；
2. `git diff --cached --check` 无 whitespace error；
3. raw transcript、secret、private path 和未脱敏 fixture admission 为 0；
4. 变更所属 Verification Profile、schema/hash、compile/test、文档 style 和引用检查通过；
5. commit body 的 Task、State-Revision、Evidence 和 Tests 可从当前文件或状态证据复核。

## PR 合同

PR 从 `main` 的 work/fix/docs/experiment 分支提交，标题与 commit subject 相同。PR 正文第一段必须说明问题和影响，再提供以下可审计字段：

```yaml
work_id: M1-05
parent_id: M1-04
scope: replay/fixtures tests
return_point: M1-05.coverage-matrix
exit_criteria: [m1-05-fixture-coverage, repository-verification]
attempt_budget: 1
promotion_target: canonical-queue
mainline_authority: false
state_revision: uint64
claim_ref: claim-id
path_owner: owner-id
evidence_refs: [artifact-ref]
verification_profile: profile-id
```

正文至少包含 `Why`、`Scope`、`State and ownership`、`Evidence`、`Validation`、`Risk and rollback`、`Open questions`。API、schema、CLI 或配置变更附真实调用或 wire example；实验 PR 明确 parent、return point、expiry、attempt budget、promotion gate 和 `mainline_authority`。PR 不复制完整报告、transcript 或大日志，只引用 artifact range、receipt 和 current provenance。

PR 合并门：required CI 全绿、独立 Verifier review、无未解决 CAS/ownership 冲突、scope violation 为 0、承重 provenance 完整、completion gate 已满足。合并前分支同步最新 `main` 并重跑受影响质量门。使用 regular merge commit 保留原子 commit 与审计链；禁止 squash 丢失验证分层。合并后删除短期工作分支，保留 migration、acceptance、replay 和 supersedes 记录。

## Commit 与 PR 频率

| 情况 | Git 动作 | 状态动作 |
|---|---|---|
| 用户 Idea、status query、短暂中断 | 不单独 commit | capture-and-continue 或 checkpoint event |
| 一个测试/合同切片完成 | 1 个原子 commit | 写 verification/evidence event |
| 一个计划叶完成 | 1-3 个逻辑 commit，合并为一个 PR | 通过 completion gate 后晋升 active leaf |
| 长实验或跨日工作 | 每个可恢复 durable artifact 至少一个本地 commit | checkpoint + attempt budget + expiry |
| 失败、回滚或 correction | 保留失败证据；修复用新 commit | append correction/supersedes/rollback event |
| 纯生成投影 | 由生成流程 commit 或发布 | 记录 source revision、template/version、content hash |

频率由可恢复性和验收边界决定。commit 数量、PR 数量和分支数量不能作为协作完成度指标。

## 首次提交准入

首次 commit 的准入条件是：当前 artifact 集合完成一次 Git admission audit；raw transcript 和 key material 不在 staged set；适用测试及 compile/data/schema/style 检查通过；MASTER、STATUS、schema registry hash 一致。首次提交建议按以下三个原子边界组织：

```text
chore(repo): establish provider-neutral repository boundary
feat(replay): admit sanitized M1-04 corpus
docs(governance): record git and lifecycle contracts
```

没有明确提交授权时只保留本地工作树，不自动创建 commit 或 PR。提交前必须验证 Git author identity；配置 remote、保护 `main`、required CI 和审阅者属于仓库托管设置，必须在首次 PR 前完成。

## 验收

- branch metadata 可从 State/PR packet 重建，原始身份和密钥不进入公开 Git ref；
- commit subject/body、Task、revision、evidence 和 tests 可机器解析；
- staged Git admission 拒绝 transcript、secret、private path 和未绑定 provenance；
- PR 的 why、scope、ownership、evidence、verification、rollback 和 promotion 字段完整率 100%；
- regular merge 后 commit、PR、state revision、checkpoint 和 evidence refs 可回放；
- branch/commit/PR 频率不改变 E1-E9 veto 门或权威状态权限。

## 实施合同

`context.git-collaboration-packet/v1alpha1` 与 `context.git-admission-receipt/v1alpha1` 已登记。离线审计读取 Git index、commit object 和 tree blob；审计 receipt 的 `runtime_state_authority` 固定为 `false`。staged replay fixture 必须与独立 validation receipt 同时进入 index，并在 admission 时重新验证。验收结果见 [`m0-11-git-admission-acceptance-2026-08-12.md`](../migrations/m0-11-git-admission-acceptance-2026-08-12.md)。
