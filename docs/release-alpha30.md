# Alpha30 · 可靠派发队列与可行动交接

Reliable dispatch queue and actionable handoff

包 `0.1.0a30`；tag／插件 `v0.1.0-alpha.30`／`0.1.0-alpha.30`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- Continuity 新增持久 `worker queue`。同一项目／数据根只允许一个非阻塞 runner 持有单实例锁；派发复用卡片校验、控制器租约、client-family 容量、observed quota／cooldown。容量门不满足时请求留在队列，临时 rate-limit／quota／gateway 结果带退避回队。`worker queue pause/resume` 是主机级开关，不终止已运行的 worker。
- MCP 暴露安全的 queue enqueue／status／pause／resume／cancel／requeue；会启动 provider 进程的 queue run 保持 CLI-only，避免普通 planning MCP 越权执行。
- 有界 `handoff show` 先保留待决事项、阻塞卡、失败／中断 attempt、报告已写但卡未更新和待评审项；普通 changes 先被裁剪。brief 明确 `unread` 是未确认 inbox 消息数，并给出当前 scope 的 list／ack 命令。
- `todo from-cards` 增加 `execution_state`：queued、assigned、running、blocked、review。仅分配但未派发的卡不再显示成 running；阻塞行附短原因。
- usage limit 归类为可恢复的 quota exhaustion，而不是 policy refusal。正式订阅 adapter 收到带 reset 的额度错误时自动写 quota 记录并暂停该 origin／model；未知 reset 保持 unknown。旧版可恢复地误标为 policy-review 的 job 会追加分类更正，之后允许原线程续跑。
- `task update --batch FILE`（或 `--batch -`）提供跨 bash／zsh／PowerShell 更稳定的顺序 JSON 更新；每批最多 50 项，首错停止，不宣称事务。reservation 错误明确 live、stale 或 unknown、attempt／result 状态和下一步。
- 项目级 `integration_base` 可由 `collaboration context-set --integration-base` 或锁定的 `.continuity/controller-profile.json` 提供，并自动进入 worker prompt；它是实现起点，不是 merge／push／deploy 授权。

## 快速使用

```bash
continuity dispatch --project-id demo --task-id parser-fix --model sol --enqueue --request-id parser-fix-a
continuity worker queue run --project-id demo --watch --controller controller
continuity worker queue status --project-id demo
continuity collaboration task update --project-id demo --batch updates.json
continuity handoff show --project-id demo --format brief
```

参数使用 `continuity help worker.queue.run`、`continuity help worker.dispatch`、`continuity help task.update` 和 `continuity help handoff.show` 查看；help 来自实际 argparse 合同，不维护第二份手写参数表。

## 升级与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a30'
continuity --version
```

pipx 环境使用 `pipx upgrade continuity-plane --pip-args=--pre`。升级三个 Codex 插件与 Claude 插件到同一版本，刷新插件并重连 MCP。运行中的旧 worker 不需要被终止；新队列和分类修复由新 CLI/MCP 使用。

Alpha30 不改变 Gitea Issue／PR 权威边界，不新增 Continuity 服务器，不迁移原生账号、Token、session 或 quota 数据。queue 是本地协作队列：单实例锁和 host pause 只覆盖同一数据根／主机；它不是跨机器强锁、进程沙箱或远端唤醒服务。临时失败自动回队不等于保证 provider 在某个时刻恢复。

## 验证范围

本版新增回归覆盖：低预算 handoff 保留关键项与 unread 语义；assigned／running／blocked 投影；usage-limit 与 policy 分流、自动 quota 记录及旧 job 更正；queue 单实例、优先级、去重、host pause、容量门和 transient backoff；task update batch；project integration base；live／stale／unknown reservation 诊断。完整公开回归、构建与 tarball／wheel 结果以 tag CI 为准；未进行新的 provider 原生额度轮询或跨机器强锁实测。

## English

Alpha30 adds a durable single-instance dispatch queue that reuses normal card, controller-lease, client capacity and observed quota/cooldown validation. MCP safely exposes queue enqueue/status/pause/resume/cancel/requeue; queue run remains CLI-only because it starts provider processes. Capacity gates keep requests queued and transient rate-limit/quota/gateway outcomes return with backoff. A host-level pause switch does not terminate running workers.

Bounded handoff output now retains pending decisions and blocked, failed, interrupted, report-without-card-update and review items before ordinary change history. The brief unread count names unacknowledged inbox messages and supplies the current scope's list/ack commands. Card Todo separates assigned from running and includes blocker reasons.

Usage limits are classified as recoverable quota exhaustion rather than policy refusal. An official-subscription adapter records a supplied reset and pauses its exact origin/model; unknown resets remain unknown. Recoverable legacy jobs mislabeled as policy-review receive an appended correction and can resume the recorded thread. Task batch updates use a JSON file or stdin and stop on the first failure without claiming a transaction. Reservation errors expose live, stale or unknown state with attempt/result evidence and next steps. Project integration_base reaches worker prompts as a starting ref, never as merge/push/deploy authority.

Install the exact package pin above, refresh matching host plugins and reconnect MCP. Alpha30 adds no Continuity server, native-account migration or cross-machine strong lock. The queue lock and host pause cover one project/data root and host; transient requeue does not promise provider recovery time.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
