# Continuity Plane Alpha38 · 在手机上管进度，Windows 原生可用

Alpha38 adds an optional owner channel: progress, questions and approvals reach your phone through Telegram, and copies can go to Feishu, DingTalk, WeCom and other webhooks. Native Windows works again.

Alpha38 新增可选的所有者通知通道：进度、提问和审批通过 Telegram 发到手机，也可以同时发到飞书、钉钉、企业微信等 webhook。Windows 原生环境重新可以安装和运行。

> **Windows 原生用户请升级。** 从 Alpha22 起，原生 Windows 上连 `continuity --version` 都会因为 `No module named 'fcntl'` 失败。本版修复，并在公开 CI 里加了 Windows 任务。
> **Native Windows users should upgrade.** Since Alpha22, even `continuity --version` failed on native Windows with `No module named 'fcntl'`. This release fixes it and adds a Windows job to the public CI.

> **通知通道默认关闭。** 不配置就没有任何变化。Telegram 机器人对话不是端到端加密，消息只包含摘要，不含代码、路径、日志或密钥。
> **The owner channel is off by default.** Nothing changes until you configure it. Telegram bot chats are not end-to-end encrypted; messages carry summaries only, never code, paths, logs or secrets.

### 📱 所有者通知通道 · Owner channel

- `continuity notify bind` 打印一次性链接，在手机上打开并发送即可绑定，不需要事先知道自己的 Telegram ID；终端确认后才保存。`notify service install` 让通道在登录后自动运行：Linux 用 systemd 用户服务，macOS 用 launchd，Windows 用当前用户的登录启动项，不需要管理员权限。
  `continuity notify bind` prints a one-time link; open it on the phone and send, with no need to know your Telegram ID, and the terminal confirms before anything is saved. `notify service install` runs the channel after login: a systemd user unit on Linux, launchd on macOS and a per-user login entry on Windows, with no administrator rights.
- 在 Telegram 里用 `/status`、`/list`、`/waiting`、`/report` 查看项目、任务和等你处理的事，任务标题可以点开看近况。`/every 09:00`、`/every 2h`、`/in 30m` 添加定时报告。
  In Telegram, `/status`, `/list`, `/waiting` and `/report` show projects, tasks and what waits for you, and task titles open their recent progress. `/every 09:00`, `/every 2h` and `/in 30m` add scheduled reports.
- Agent 完成一个阶段时发总结卡：结论、实测数据（例如 token 前后对比）、截图、下一阶段建议，以及正在进行和排队的任务。需要你动手的事发成待办卡，可以标记完成、稍后或提问。
  When an agent finishes a stage it sends a summary card: conclusions, measurements such as token use before and after, screenshots, next-stage recommendations, and the tasks in progress and queued. Things you must do arrive as action cards you can mark done, postpone or question.
- Agent 需要你决定时用 `continuity_ask_owner` 或 `notify ask` 提问：问题持久保存，手机和命令行看到同一份，第一个回答生效，到期未答会通知提问方。回复任何一条消息都会带着原文回到发送方。
  Agents ask with `continuity_ask_owner` or `notify ask`: questions persist, the phone and the CLI see the same list, the first answer wins and the asker hears when a question expires. A reply to any message returns to its sender with the original text.
- 审批只记录决定，由申请方执行：低风险点一次，中风险需要二次确认，高风险不能在通道内批准；一个申请里可以只批准其中几项。Agent 可以自定义按钮。
  Approvals record decisions and the requester acts on them: low risk takes one tap, medium risk asks again, high risk cannot be approved in the channel, and you may approve only some items of a request. Agents can define their own buttons.
- 任务失败、中断、进程丢失，以及会话撞到限额或额度用完时立即提醒；完成的任务按项目每小时汇总一次；23 点到 8 点只推失败和需要决定的事。
  Failed, interrupted and lost jobs, and sessions that hit a rate limit or run out of quota, alert at once; finished tasks are summarised hourly per project; from 23:00 to 08:00 only failures and decisions are sent.
- 标记为“回答后继续”的问题和审批会叫醒对应会话，其他回复排队，可以点按钮叫醒。叫醒命令不经过 shell，消息从 stdin 传入。
  Questions and approvals marked resume-on-answer wake their session; other replies queue, with a button to wake it. Wake commands run without a shell and receive the message on stdin.

### 🔗 Webhook 通知 · Webhook notifications

- 安装 `continuity-plane[webhooks]` 后，主动发出的通知、提问、阶段报告和提醒会以纯文本同时发到 webhook，通过成熟的 [Apprise](https://github.com/caronc/apprise)（BSD-2-Clause）支持飞书、钉钉、企业微信、Lark、Slack、Discord、Teams、ntfy、Bark、邮件和通用 JSON。飞书和钉钉可以直接粘贴后台复制的地址。
  With `continuity-plane[webhooks]`, proactive notifications, questions, stage reports and alerts are also sent as plain text to webhooks through the mature [Apprise](https://github.com/caronc/apprise) library (BSD-2-Clause): Feishu, DingTalk, WeCom, Lark, Slack, Discord, Teams, ntfy, Bark, e-mail and generic JSON. Feishu and DingTalk accept the address copied from their admin pages.
- 可以只用 webhook、不用 Telegram。webhook 地址保存在只有当前用户能读的文件里，配置只记录文件路径；某个 webhook 失败不影响其他通道。
  Webhooks work without Telegram. The address stays in a file only the current user can read and the config stores only its path; one failing webhook does not stop the others.

### 🪟 Windows

- 修复原生 Windows 无法导入 CLI 的问题：协作注册表改用 Windows 文件锁；替换文件遇到其他进程正在读取时短暂重试。
  Fixed the CLI failing to import on native Windows: the collaboration registry uses Windows file locks, and replacing a file retries briefly while another process is reading it.
- 修复 Windows 上读取变更记录时 `OverflowError: Python int too large to convert to SQLite INTEGER`：Windows 的 64 位文件 ID 不再溢出；变更索引出错时回退到原始记录。
  Fixed `OverflowError: Python int too large to convert to SQLite INTEGER` when reading changes on Windows: 64-bit Windows file IDs no longer overflow, and index errors fall back to the journal.
- 通知通道、令牌文件检查和叫醒命令在 Windows 上可用。
  The owner channel, token file checks and wake commands work on Windows.

### 📊 实测 · Measurements

- 原生 Windows 11（Python 3.12，无 WSL）：149 项相关测试通过；8 个进程并发写 80 张卡片无丢失；后台服务、单实例锁和 webhook 投递实测通过；从 Windows 直接发出 Telegram 消息成功；hook 入口处理 6 种 Claude Code 事件全部正常，`StopFailure` 被记录。
  Native Windows 11 (Python 3.12, no WSL): 149 related tests pass; 8 concurrent processes wrote 80 cards with none lost; the background service, single-instance lock and webhook delivery were exercised; a Telegram message was sent directly from Windows; the hook entry point handled 6 Claude Code event types and recorded `StopFailure`.
- Telegram 实测：状态与列表视图、按钮、待办卡、阶段总结、提问与回答、审批和叫醒。Webhook 用 Apprise 2.2.0 发到本地接收端实测。
  Live Telegram: status and list views, buttons, action cards, stage summaries, questions and answers, approvals and wake. Webhooks were exercised with Apprise 2.2.0 against a local receiver.

### ⚠️ 已知限制 · Known limits

- Webhook 是单向的：按钮显示为可选项列表，回答要在 Telegram 或 `notify answer` 里完成。飞书机器人不支持签名校验，请改用关键词或 IP 白名单。
  Webhooks are one-way: buttons appear as a list of choices, and answers go through Telegram or `notify answer`. Feishu bots cannot use signature verification; use a keyword or an IP allowlist.
- 没有在真实的飞书、钉钉、企业微信群里验证，只验证了地址转换和 Apprise 投递。macOS 的 launchd 服务没有实机验证。
  Not verified in real Feishu, DingTalk or WeCom groups, only the address conversion and Apprise delivery. The macOS launchd service is not verified on a real Mac.
- Windows 上的 Claude Code 会话没有实机验证；hook 入口和 CLI 已在原生 Windows 验证。
  Claude Code sessions on Windows are not verified on a real machine; the hook entry point and the CLI are verified on native Windows.
- 高风险审批暂不能在通道内完成；只批准部分项目和自由文本回答只有自动测试。
  High-risk approvals cannot be completed in the channel yet; partial approval and free-text answers are covered by automated tests only.

### 升级 · Upgrade

```bash
pipx install --force 'continuity-plane==0.1.0a38'
# 需要 webhook 时 · with webhooks
pipx install --force 'continuity-plane[webhooks]==0.1.0a38'
```

Windows PowerShell：`py -3 -m venv .venv`，然后 `.venv\Scripts\python.exe -m pip install "continuity-plane[webhooks]==0.1.0a38"`。插件请在 Claude Code 或 Codex 的插件管理器中更新，`StopFailure` 等新 hook 在更新后生效。

Windows PowerShell: `py -3 -m venv .venv`, then `.venv\Scripts\python.exe -m pip install "continuity-plane[webhooks]==0.1.0a38"`. Update the plugins in the Claude Code or Codex plugin manager; new hooks such as `StopFailure` take effect after the update.

完整变更：[CHANGELOG](../CHANGELOG.md) · [命令与工具参考](commands.md#所有者通知通道) · [部署说明](deployment.md)
