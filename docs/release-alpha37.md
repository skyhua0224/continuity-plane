# Continuity Plane Alpha37 · Work survives model changes, and the queue actually runs

Alpha37 fixes the problems a coordinator hits when many workers run for hours. Work is no longer lost when a card moves to another model, a worker can no longer start on outdated instructions, and the persistent queue finally starts workers. New versions now announce themselves.

Alpha37 修复了主控长时间带多个执行者时遇到的问题：卡片换模型不再丢进度，执行者不会再按过期指令开工，持久化队列终于能真正启动任务。新版本发布后也会主动提示。

> **升级后请重连 MCP 或新开会话。** 从这一版起，MCP 服务器发现升级后会自动退出，由宿主重连到新版本。
> **Reconnect MCP or start a new session after upgrading.** From this version, MCP servers exit when they detect an upgrade, and the host reconnects to the new version.

> **主控同时负责多个项目时，需要明确配置。** 其他项目的结束提醒不再按主控名字匹配：请设置 `CONTINUITY_WATCH_PROJECTS`，或在 SSH 桥接配置的项目映射里加 `watch_projects`，否则收不到这些项目的提醒。
> **If one coordinator leads several projects, configure it.** Notices from other projects no longer follow the controller name: set `CONTINUITY_WATCH_PROJECTS`, or add `watch_projects` to the project mapping in the SSH bridge configuration, or those notices stop.

> **已有工作的卡片不能再直接重新派发。** 卡片有提交、会话或报告时，用 `worker resume` 或 `worker handoff` 继续；确实要从头开始时加 `--fresh`。
> **A card with existing work can no longer be dispatched afresh by accident.** Continue with `worker resume` or `worker handoff`, or pass `--fresh` to start over on purpose.

### 🧰 任务不丢 · Work is not lost

- 修复卡片因限流自动转到另一个模型时，新执行者只拿到最初的卡片正文、从头重做的问题。自动回退和手动交接现在都带上工作目录、分支、HEAD、最新 next_action、未提交改动和上一份报告。
  Fixed a card moved to another model after a rate limit restarting from its original body. Automatic fallback and manual handoff now both carry the worktree, branch, HEAD, latest next_action, uncommitted changes and last report.
- 修复卡片更新失败后，派发仍按旧指令启动执行者的问题。这种情况下 dispatch 现在会拒绝启动；每次派发结果都显示实际使用的 next_action 及其时长。
  Fixed workers starting on old instructions after a failed card update. Dispatch now refuses in that case, and every dispatch result shows the next_action it used and how old it is.
- 修复 `worker queue run` 从引入起就无法启动任何执行者的问题：每个排队请求都会失败。同一模型的排队重试现在继续原会话。
  Fixed `worker queue run`, which could not start any worker since it was added: every queued request failed. A queued retry on the same model now continues its session.
- MCP 派发预览不再改变卡片状态，并明确返回 `preview=true`。
  The MCP dispatch preview no longer changes the card and returns `preview=true`.

### 🔔 通知与报告 · Notices and reports

- 其他项目的事件不再因为共用主控名字而跑到不相关的会话里；同一个结果只提醒一次，先丢失后完成的任务只报告最终结果。
  Other projects' events no longer reach unrelated sessions through a shared controller name; each outcome is announced once, and a job that was lost and later completed reports only its final result.
- 报告里的分支、HEAD 和基线由系统从 git 写入，并标出与执行者所述不一致的地方。
  Branch, HEAD and base in reports come from git, and differences from what the worker claimed are flagged.
- 内容策略拒绝时显示供应商的原始信息；评审卡可以选择自动换到另一个模型家族。
  Content-policy refusals show the provider's message; review cards can opt in to moving to another model family.

### ⬆️ 版本更新 · Updates

- 新增更新检测：每 24 小时最多在后台查询一次，不拖慢命令，只请求 PyPI 的公开版本信息。终端里显示一行提示，`continuity_start` 和会话开始时告诉 Agent。`CONTINUITY_UPDATE_CHECK=0` 关闭。
  Added update detection: at most once every 24 hours in the background, without slowing commands, requesting only PyPI's public version data. A terminal shows one line, and agents learn through `continuity_start` and session start. `CONTINUITY_UPDATE_CHECK=0` turns it off.
- 新增 `continuity self-update`：识别 pipx、uv 和 venv 安装；有执行者运行时默认等待。`CONTINUITY_AUTO_UPDATE=1` 可选择在会话开始时自动更新。
  Added `continuity self-update`, which recognises pipx, uv and venv installs and waits while workers run. `CONTINUITY_AUTO_UPDATE=1` opts in to updating at session start.
- `continuity doctor` 显示 CLI、MCP 和插件之间的版本差异。
  `continuity doctor` shows version differences between the CLI, MCP and plugins.

### 🐛 其他修复 · Other fixes

- 错误信息写明字段、实际大小和上限；`next_action` 上限提高到 4 KB；`task update` 新增 `--dry-run`，缺少 `--actor` 时给出明确错误。
  Errors state the field, actual size and limit; the `next_action` limit is now 4 KB; `task update` gains `--dry-run` and a clear error for a missing `--actor`.
- MCP 建卡的 `exit_criterion` 可以写字符串或列表，并支持 `body_file`；Claude 原生 MCP 可以通过服务端令牌文件启动执行者。
  MCP card creation accepts `exit_criterion` as a string or list and supports `body_file`; native Claude MCP can launch workers through a server-side token file.
- Gitea 不可用时，评审请求的错误写明后端和替代办法；队列命令会说明有没有 runner 在消费。
  With Gitea down, review-request errors name the backend and the alternative; queue commands say whether a runner is consuming.
- 修复 `collaboration reconcile` 未传 `--root` 时崩溃的问题；`continuity_events_wait` 每个结果只返回一次。
  Fixed `collaboration reconcile` crashing without `--root`; `continuity_events_wait` returns each outcome once.

### 📊 实测 · Measurements

- 更新检测对 CLI 启动的影响：约 +1 ms（`collaboration doctor`，每轮 40 次，共两轮，中位数 56.9 → 57.9 ms）。
  Update detection adds about 1 ms to CLI start-up (`collaboration doctor`, 40 runs per round, two rounds, median 56.9 → 57.9 ms).
- 默认 MCP 工具定义加使用说明不超过 18,419 字节，由测试保证。
  Default MCP tool definitions plus instructions stay within 18,419 bytes, enforced by a test.

### ⚠️ 已知限制 · Known limits

- `continuity self-update` 只做过预演，没有在真实安装上执行过升级。
  `continuity self-update` has only been dry-run; no real upgrade has been executed.
- 同模型重试和换模型交接用模拟执行者测试，没有在真实供应商上验证。
  Same-model retry and model handoff were tested with stand-in executors, not real providers.
- Codex 宿主的会话开始提醒还没有接入更新提示，Codex Agent 通过 `continuity_start` 得知。
  The Codex session-start hook does not carry the update line yet; Codex agents learn through `continuity_start`.

### 升级 · Upgrade

```bash
pipx install --force 'continuity-plane==0.1.0a37'
```

完整变更：[CHANGELOG](../CHANGELOG.md) · [命令与工具参考](commands.md) · [部署说明](deployment.md)
