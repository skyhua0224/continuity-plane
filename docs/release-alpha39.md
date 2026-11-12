# Continuity Plane Alpha39 · 手机上一屏看清进度，工作目录按卡清理

Alpha39 redraws the owner channel with the newest Telegram features: a pinned panel, rich reports with tables, coloured buttons and receipts. It also starts managing the directories that workers leave behind.

Alpha39 用 Telegram 最新的显示能力重做了所有者通知通道：置顶的进度面板、带表格的富文本报告、按用途着色的按钮和已读回执。同时开始管理 worker 留下的工作目录。

> **不需要改配置。** 新显示在 Telegram 支持时自动生效，不支持时退回 Alpha38 的样式；webhook 仍收到纯文本。存储清理默认只预览，不会删除任何东西。
> **No configuration change is needed.** The new display applies when Telegram accepts it and falls back to the Alpha38 style otherwise; webhooks still get plain text. Storage cleanup previews by default and deletes nothing.

### 📱 一屏看清 · At a glance

- `/panel` 发送一个置顶的小组件面板：每个项目一个标签页，进行中、排队、阻塞、待审四个数字和比例条，下面是需要看一眼的任务，按“等你处理、阻塞、进行中、待审、排队”排序。点任务打开卡片，点刷新立即取最新数据；面板每 5 分钟检查一次，内容变化才更新。图标是内置的线条图标，跟随 Telegram 的深浅色主题。
  `/panel` sends a pinned widget: one tab per project with the active, queued, blocked and review counts and a proportion bar, then the tasks worth a glance in the order owner, blocked, active, review, queued. Tap a task to open its card, tap refresh for fresh data; the panel is checked every 5 minutes and updated only when it changes. Icons are built-in line icons that follow Telegram's light and dark themes.
- `/status` 可以置顶，之后原地刷新，不再刷屏。
  `/status` can be pinned and then refreshes in place instead of adding messages.
- 阶段报告、每小时的完成汇总、进度报告和故障提醒改为富文本消息：标题、实测数据表格、可展开的详细结论和任务清单。进度报告用一张表并列各项目的完成、待审、进行中、新阻塞和等你处理。
  Stage reports, hourly completion digests, progress reports and alerts are now rich messages: headings, measurement tables, expandable conclusions and task lists. The progress report puts every project's finished, review, active, newly blocked and waiting counts in one table.

### 🎛 更顺手的操作 · Smoother interaction

- 按钮按用途着色：批准为绿色，拒绝和故障为红色，主要操作为蓝色。处理过的按钮变灰但保留，聊天记录里能看到当时批准了哪几项。
  Buttons are coloured by purpose: approve green, reject and failures red, the main action blue. Handled buttons turn grey but stay, so the chat history shows which items were approved.
- 时间按你手机的时区和语言显示，“几分钟前”会自动更新；长内容默认折叠。
  Times follow your phone's time zone and language, "minutes ago" keeps itself current, and long text is collapsed.
- 中风险审批的二次确认改为只有你看得到的临时消息，显示在原卡片的位置。它只用于确认：消失或取消都不会改变任何决定。
  The second confirmation of a medium-risk approval is an ephemeral message only you see, shown in place of the card. It only confirms: if it disappears or is cancelled, nothing is decided.
- 你回复后，消息上出现 👀 表示已转达，接收方读到后变成 👌，不再另发一条“已转给”。
  After you reply, 👀 on your message means it was forwarded and 👌 means the recipient read it; no separate "forwarded" message.
- 在 @BotFather 为机器人打开话题模式后，每个项目一个话题，卡片、提问、审批和报告按项目分开；在话题里直接发消息就转给该项目。
  With topic mode turned on for the bot in @BotFather, each project gets its own topic for its cards, questions, approvals and reports; a message written in a topic goes to that project.

### 🧹 工作目录按卡清理 · Workspaces cleaned up per card

- 新增 `continuity storage register/report/gc`。项目在配置里声明机器和存储层，worker 和子任务创建的工作目录、临时目录和派发任务目录登记到对应卡片。
  New `continuity storage register/report/gc`. The project declares its machines and storage tiers, and the workspaces, scratch and worker-job directories that workers and delegates create are registered against their cards.
- `report` 显示每个存储层的已用、剩余和配额，用量超过阈值时把最大的可清理目录排在前面；未登记的目录只报告，并按卡片的工作树和分支建议归属。
  `report` shows used, free and quota bytes per tier and, above the threshold, lists the largest removable directories first; unregistered directories are only reported, with a suggested card from card worktrees and branches.
- `gc` 默认只预览，`--apply` 才删除，而且只删已结束卡片的干净临时目录：Git 工作树用 `git worktree remove` 删除并保留分支；有未提交改动、未跟踪文件、进行中的 rebase/merge、被锁定，或独立克隆里有源仓库没有的提交时一律跳过并说明原因；仓库里的子目录不删。派发任务目录只截短已结束任务的输出日志。
  `gc` previews by default and deletes only with `--apply`, and then only clean scratch of finished cards: Git worktrees go through `git worktree remove` and keep their branch; uncommitted or untracked files, an in-progress rebase or merge, a locked worktree or a standalone clone with commits its origin lacks all block removal with a reason; subdirectories inside a repository are never deleted. Worker-job directories only have the output log of finished jobs trimmed.

### 📊 实测 · Measurements

- 每个新接口（按钮颜色、失效按钮、`tg-time`、可折叠引用、表情回执、置顶、富文本消息、小组件、小组件数据更新）都先单独发到线上 Bot API 确认可用；回执 👀 → 👌、小组件面板和富文本报告在所有者的 Telegram 上实际使用过。
  Each new API (button styles, disabled buttons, `tg-time`, expandable quotes, reactions, pinning, rich messages, widgets, widget data edits) was first sent to the production Bot API on its own; the 👀 → 👌 receipt, the widget panel and the rich reports were used in the owner's Telegram.
- 小组件 HTML 压缩后 2,438 字节（上限 4,096），面板数据不超过 1,024 字节，超出时先删任务行。
  The widget HTML is 2,438 bytes compressed (limit 4,096) and the panel state stays within 1,024 bytes by dropping task rows first.
- 通知通道测试 128 项，存储清理测试 25 项；删除只在测试的临时目录里执行过。
  128 owner-channel tests and 25 storage tests; deletion ran only inside test temporary directories.
- 原生 Windows 11 上 157 项相关测试通过；这一轮在 Windows 上发现并修复了文件路径的盘符解析，以及用户目录本身是 Git 仓库时临时目录无法清理的问题。完整源码套件 3,054 项。
  157 related tests pass on native Windows 11; this round found and fixed, on Windows, drive letters in file paths and scratch that could not be cleaned when the user profile is itself a Git repository. The full source suite has 3,054 tests.

### ⚠️ 已知限制 · Known limits

- 富文本消息和小组件需要较新的 Telegram 客户端；旧客户端上小组件显示为附带说明文字的不支持内容。没有逐一验证 iOS、Android 和桌面版。
  Rich messages and widgets need a recent Telegram app; older apps show a widget as unsupported media with its caption. iOS, Android and desktop were not each verified.
- 中风险审批的临时确认、话题模式只有自动测试；话题模式需要你在 @BotFather 打开。
  The ephemeral approval confirmation and topic mode are covered by automated tests only; topic mode needs you to turn it on in @BotFather.
- 消息里的自定义图标需要机器人创建者有 Telegram Premium，本版没有使用。
  Custom emoji icons in messages need the bot's creator to have Telegram Premium and are not used.
- 存储清理只管理本机。证据归档、跨机器搬运、派发时自动登记和按计划自动清理还没有实现，配置里的 `automation` 目前不会触发任何动作。
  Storage cleanup manages the local machine only. Evidence archiving, cross-machine moves, registration at dispatch and scheduled cleanup are not implemented; the `automation` section does not trigger anything yet.

### 升级 · Upgrade

```bash
pipx install --force 'continuity-plane==0.1.0a39'
# 需要 webhook 时 · with webhooks
pipx install --force 'continuity-plane[webhooks]==0.1.0a39'
```

升级后重启通知通道（`continuity notify service install` 或重新运行 `continuity notify serve`），`/panel` 命令会出现在 Telegram 的命令菜单里。
After upgrading, restart the owner channel (`continuity notify service install` or run `continuity notify serve` again); `/panel` then appears in Telegram's command menu.

完整变更：[CHANGELOG](../CHANGELOG.md) · [命令与工具参考](commands.md#所有者通知通道) · [工作目录与存储清理](commands.md#工作目录与存储清理)
