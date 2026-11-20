# Continuity Plane Alpha40 · Antigravity CLI 成为第三种执行器

Alpha40 adds the Antigravity CLI (`agy`) as a native executor next to Codex and Claude, with its own sign-in, models and quota, and brings the Continuity plugin, MCP servers and lifecycle hooks into it.

Alpha40 把 Antigravity CLI（`agy`）接成与 Codex、Claude 并列的原生执行器，使用 agy 自己的登录、模型和额度，并把 Continuity 的插件、MCP 服务和生命周期 hooks 带进 agy。

> **不需要改现有配置。** Codex 和 Claude 的插件、hooks 行为不变。agy 需要先在执行主机上登录，再注册执行器、安装插件。
> **No change to existing configuration.** Codex and Claude plugins and hooks behave as before. For agy, sign in on the execution host first, then register executors and install the plugin.

### 🤖 agy 执行器 · The agy executor

- `continuity worker configure --provider antigravity --provider-model <模型>` 注册 agy 执行器。注册时核对模型在 `agy models` 里，并记下该模型支持的 effort；派发时超出范围的 effort 降到它支持的最高一档（Claude 组模型最高是 high）。
  `continuity worker configure --provider antigravity --provider-model <model>` registers an agy executor. Configuration checks the model against `agy models` and records the efforts it offers; dispatch lowers an effort above that range to the highest offered one (Claude models stop at high).
- worker 在任务卡自己的工作树里运行；续跑沿用同一个 agy 会话。会话 ID、最终回复和错误原文都会记下，与 Codex、Claude 的派发记录一样。
  The worker runs in the card's own worktree, and resume continues the same agy conversation. The conversation ID, final reply and error text are recorded the same way as for Codex and Claude.
- `continuity agy inspect` 一次读出版本、模型列表、当前模型与 effort、设置、hooks、skills、自定义 agent、G1 余额和额度。这些斜杠命令在本地执行，不调用模型。
  `continuity agy inspect` reads the version, model list, current model and effort, settings, hooks, skills, custom agents, G1 credits and quota in one go. These slash commands run locally without a model call.

### 📊 按用量自动判断额度 · Quota judged from use

- 额度来自 `agy /usage`：按模型所在的组（以及单独计额的模型）取剩余最少的窗口。`/usage` 偶尔返回较旧的数据，所以连读三次取最紧的一次。
  Quota comes from `agy /usage`: the window with the least remaining share for the model's group (and for a separately metered model). `/usage` occasionally answers with older data, so it is read three times and the tightest reading kept.
- 自动选模型时，额度记录缺失或过期就当场刷新；每次 agy 任务结束后也会刷新一次。不需要设置并发上限，套餐变化后按新的额度判断。
  Automatic model choice refreshes a missing or stale record on the spot, and every finished agy run refreshes it too. No concurrency limit is needed; a plan change is reflected in the next reading.
- 同一轮检查的多个 agy 执行器共用一次读取，四个执行器约 30 秒。`continuity quota tick` 也能刷新 agy 执行器。
  Executors checked in one pass share one set of reads, about 30 seconds for four. `continuity quota tick` refreshes agy executors too.

### 🔌 插件、MCP 与 hooks · Plugin, MCP and hooks

- `continuity agy install` 安装 agy 插件 `continuity-plane`：协作 skill、`continuity` 与 `continuity-search` 两个 MCP 服务和生命周期 hooks。默认装到当前工作区，`--scope global` 装到本机全局，`--dry-run` 只预览，可重复安装；`--bridge-config` 让 hooks 通过 SSH 连到另一台主机上的状态。`continuity agy status` 检查插件、hooks 和 MCP 版本是否一致。
  `continuity agy install` installs the agy plugin `continuity-plane`: the collaboration skill, the `continuity` and `continuity-search` MCP servers and lifecycle hooks. It installs into the current workspace by default, `--scope global` installs for the machine, `--dry-run` previews, and reinstalling is safe; `--bridge-config` sends hooks over SSH to state on another host. `continuity agy status` checks that the plugin, hooks and MCP versions match.
- agy 的 hook 事件比 Claude 少，对应关系如下：
  agy has fewer hook events than Claude; they map as follows:
  - 会话开始注入恢复入口；新一轮用户输入的第一次模型调用注入规划提示（对应 UserPromptSubmit）。
    Session start injects the recovery entry; the first model call after new user input injects the planning hint (UserPromptSubmit).
  - agy 自行压缩长对话但没有压缩 hook：每轮结束（Stop）保存与压缩前相同格式的交接，会话开始或续接后的第一次调用时校验并恢复。
    agy compacts long conversations itself but has no compaction hook: every turn end (Stop) saves the same handoff as before compaction, and session start or the first call after resume verifies and restores it.
  - 工具调用后的提醒在下一次模型调用前注入，因为 agy 的 PostToolUse 不能插入内容；工具调用前一律放行。
    Reminders after a tool call are injected before the next model call, because agy's PostToolUse cannot insert content; tool calls are always allowed.
  - 子代理、会话结束和通知事件在 agy 中没有对应 hook。
    Subagent, session-end and notification events have no agy hook.

### 📱 通知通道 · Owner channel

- 从命令行发出的 `notify send`、`notify stage`、`notify action` 现在也进入项目话题，不再落到主聊天。Telegram 拒绝某次请求时，日志写明接口和原因。
  `notify send`, `notify stage` and `notify action` from the command line now land in the project topic instead of the main chat. When Telegram refuses a request, the log names the method and the reason.

### 📊 实测 · Measurements

- agy 1.5.13（Linux）：用 Continuity 派发并续接一张任务卡，worker 在卡片工作树里写文件并通过 MCP 交回报告；全局安装插件后，会话开始的恢复提示、工具调用后的提醒和 MCP 调用都在真实会话里出现。额度读数与 `/usage` 一致。
  agy 1.5.13 (Linux): Continuity dispatched and resumed a card; the worker wrote a file in the card's worktree and reported through MCP. With the plugin installed globally, the session-start recovery hint, the post-tool reminder and MCP calls appeared in a real session. Quota readings match `/usage`.
- `/usage` 连读 6 次中有 1 次返回较旧的数据（5 小时窗口 1.27% 而不是 2.15%），这是连读三次的依据。
  One read in six of `/usage` returned older data (1.27 % of the 5-hour window instead of 2.15 %), which is why it is read three times.
- agy 相关 25 项测试，通知通道 131 项；完整源码套件 3,083 项。
  25 agy tests and 131 owner-channel tests; the full source suite has 3,083 tests.

### ⚠️ 已知限制 · Known limits

- 只在 Linux 上验证了 agy；macOS 和 Windows 上的 agy 未实机验证。
  agy is verified on Linux only; macOS and Windows were not tried on real machines.
- 没有收集到 agy 真实的额度耗尽或限流报错原文，这两类错误按通用规则识别。
  No real agy quota-exhausted or rate-limit error text was collected; these errors are recognized by the general rules.
- 跨主机的 hook 转发（`--bridge-config`）只有自动测试。
  Hook forwarding to another host (`--bridge-config`) is covered by automated tests only.

### 升级 · Upgrade

```bash
pipx install --force 'continuity-plane==0.1.0a40'
# 注册 agy 执行器并安装插件 · register an agy executor and install the plugin
continuity worker configure --model ag-hydrogen --provider antigravity --provider-model gemini-4-hydrogen --official-subscription
continuity agy install --scope global
continuity agy status
```

升级后重启通知通道（`continuity notify service install`），并重连 MCP 或新开会话。
After upgrading, restart the owner channel (`continuity notify service install`) and reconnect MCP or start a new session.

完整变更：[CHANGELOG](../CHANGELOG.md) · [命令与工具参考](commands.md#antigravity-cliagy) · [Hooks](commands.md#hooks)
