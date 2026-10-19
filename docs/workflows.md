# 按工作方式使用

[English](workflows.en.md) · [使用说明](../USAGE.md) · [参数参考](field-workflows.md)

下面的路径在安装后使用。`demo`、路径、模型 ID 与测试命令均需替换。卡片与共享交接已足够时，不要求再维护 Todo、claim 或 inbox；CLI、SSH 包装和 MCP 是可替换入口。

## 一个人对抗压缩

在新项目中，6 条命令形成可读取的接续点：

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
continuity handoff write --project-id demo --plan '修复当前回归后再做下一项' --next-action '运行回归测试'
continuity handoff show --project-id demo
continuity collaboration packet --project-id demo --assignee controller
```

之后启用真实宿主 hook；仅运行上述命令不会让宿主自动压缩或注入。压缩前保留下一步、硬约束、验证入口与待决事项。新会话先读 packet／handoff，不要求重读整个历史或先生成报告。

## 一个主控与两个执行方

前提是已获授权的原生客户端与两个已准备好的独立 checkout。替换路径、模型与项目测试要求。下面 10 条命令从初始化走到两份报告读取：

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity collaboration task add --project-id demo --task-id task-a --lane-id a --mode patch --priority p1 --title '修复 A' --objective '完成有界修复' --next-action '运行测试' --exit-criterion '证据可核对' --assignee worker-a --worktree /worktrees/task-a --allowed-file src/a.c --body '检查相关符号、修复 A 并记录实际测试'
continuity collaboration task add --project-id demo --task-id task-b --lane-id b --mode patch --priority p1 --title '验证 B' --objective '验证边界情形' --next-action '运行测试' --exit-criterion '报告可复现' --assignee worker-b --worktree /worktrees/task-b --allowed-file tests/ --body '只在授权范围内验证 B 并记录证据'
continuity worker configure --model primary --provider codex --provider-model YOUR_MODEL_ID
continuity dispatch --project-id demo --task-id task-a --model primary --controller controller --effort high --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity dispatch --project-id demo --task-id task-b --model primary --controller controller --effort high --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --event dispatch.policy-stopped --timeout-seconds 30
continuity report task-a --project-id demo
continuity report task-b --project-id demo
```

这条基础路径使用前台顺序 dispatch，读取时两份报告都已完成。需要并行时，为两条 dispatch 加 --background，并在读报告前分别等待所需 task-id。单个匹配事件不表示全部 worker 结束；超时不是失败，过早读取可返回未挂载。这里不执行 Git 合入或推送。

正文必须写明项目实际的构建／测试／审查要求。`--worktree` 要在执行主机真实存在，并行改动前先准备隔离 checkout；allowed-file 是范围声明，不是 OS 强制隔离。

### 可选开场与现有脚本

已有 SSH launch/resume/report 脚本可以继续作主入口，不要求迁移到 MCP。可检查上下文，而不用手工拼接：

```bash
continuity rules export --project-id demo --audience worker --format text
continuity collaboration prompt --project-id demo --task-id task-a --assignee worker-a
continuity collaboration packet --project-id demo --assignee worker-a
```

`dispatch --dry-run` 是计划而不是存活声明。`resume task-a '继续已验证的进度'` 使用已记录原生会话引用；不是每种自定义适配器都有原生续跑能力。effort 按适配器透传；原生登录与会话仍在所属机器/profile。

收尾协议可直接放进简报：

> 开场确认自己的身份、packet、规则和卡片正文；按需读 inbox 与代码路标。结束保存最终报告、commit 和实际验证结果，列出未验证项；向主控交接。卡片状态不是推送或合入授权。

普通成功 attempt 由 supervisor 投影为待评审，失败／中断为阻塞。Alpha28 在 worker 提前改卡后仍挂载当前 attempt 的最终报告，不回退手工状态；旧 attempt 或换执行者后的迟到报告不能覆盖新附件。证据范围见[验证矩阵](validation.md)。

### 合批与批准

使用 `task update --verification-json` 把验证绑定到实际 commit；通过项与未验证项分开。`batch-add` 保存卡片、基线、结果 commit、验证、可选 metrics；`decision-add` 写实际批准来源与更正。`task merged TASK --batch BATCH` 使用已记录批次，不替你执行 Git。

参数较多的记录先查 `continuity help batch-add`；不要把示例假 commit 或“未执行”的检查记成通过。缺证据应可见，override 需要理由，真正的外部操作仍需用户授权。

## 一个团队

在共同选择的权威宿主注册项目，升级所有派发客户端后再选择互斥。5 条操作展示这个边界；将 `FENCE` 替换为 acquire 返回的项目协调 token，不是 provider key。

```bash
continuity controller acquire --project-id demo --owner controller --ttl-seconds 10800 --client-limit codex=5
continuity controller status --project-id demo --owner observer
continuity controller renew --project-id demo --owner controller --token FENCE --ttl-seconds 10800
continuity handoff show --project-id demo
continuity collaboration task list --project-id demo --format brief
```

第二个主控可以读取，但受保护的派发、批次与集成操作需要当前租约。用 `controller run` 串行执行明确授权的集成命令；不要把任意原生 Agent 启动或绕过工具的 Git 当作已保护。

首个 acquire 启用 guarded ledger v4。租约到期不终止已有 worker，也不自动把新派发权限交给它。未知集成操作不会因换人就重放；清除预留／操作需要精确 ID 和理由。远端方法见[部署](deployment.md)。

## 规则资料与教训

规则维护一份，按项目／全局及 worker／reviewer／controller 受众交付。生成镜像后用 `rules check-mirror` 检查漂移；大政策放哈希附件，不让预算裁掉约束。

`reference list/add/record` 保存一手来源与采用依据；`lesson add/list` 保存经验及更正历史。`reference import-tsv --file docs/authority/source-index.tsv` 导入仓库索引；`reference export-tsv --file source-index.export.tsv` 生成新文件供比较，不覆盖已有文件。相同键内容冲突时整批拒绝；导入只声明候选，不代表已读。列格式与身份配置见[Alpha28](release-alpha26.md)。

Todo 是卡片视图时用 `todo from-cards`，默认只投影未关闭状态；独立的顺序清单可另用 Todo。inbox 适合需要明确投递与 ack 的交接；已有可靠共享交接不必强行加一轮消息。hook 应提供有用事件，而不是以是否使用每个功能衡量采用程度。
