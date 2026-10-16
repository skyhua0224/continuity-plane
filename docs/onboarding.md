# 计划、身份与执行方交接

[English](onboarding.en.md) · [从零开始](../USAGE.md) · [按角色工作流](workflows.md)

## 选择最短路径

卡片、交接、wait 和批次已足够时，可继续使用现有 CLI／SSH 脚本，不强制改用 MCP、Todo、claim 或 inbox。MCP 是同一能力的另一入口；它不会天然知道你的远端 checkout。

## 身份与任务卡

先明确 project_id、稳定执行者身份和主控。whoami 显示本次声明，不是持久账号注册，也不是租约：

```bash
continuity whoami --root . --assignee worker-a --controller controller
continuity collaboration packet --project-id demo --assignee worker-a
continuity collaboration task list --project-id demo --assignee worker-a --format brief
```

任务正文、真实 worktree、允许文件、构建／测试要求和审查责任要在派发前可读。packet 不应把别人的 queued 卡伪装成派给你的任务。声明的 allowed-file 不是操作系统限制。

当前 MCP 重连身份与卡片归属仍有易用性缺口。先核对执行者名字与新连接；不要把每次 force 当正常成功路径，或借此抢占别人的卡。

## 开场与收尾

开场读取必要的 packet、规则、正文和代码路标；收件箱有消息时再取。完成后保留报告、commit、实际检查与未验证范围，由主控评审。supervisor 结束可投影待评审／阻塞；Alpha26 提前改卡后仍挂载当前 attempt 的报告并保留手工状态，详见[验证](validation.md)。

## 持久收件箱

```bash
continuity collaboration inbox send --project-id demo --message-id handoff-a --sender controller --recipient worker-a --body 'Please check the current report'
continuity collaboration inbox list --project-id demo --recipient worker-a
continuity collaboration inbox ack --project-id demo --recipient worker-a --message-id handoff-a
```

同 ID 相同内容幂等，不同内容被拒；读取不自动 ack。正文有界，长报告走产物引用。消息不保证唤醒任意宿主会话，也不授予新增授权。

## 类型化 State 计划

已有 MASTER／STATUS 不用模板覆盖。需要 Work／决定／约束的权威录入时，使用[State 接入](state-workflows.md)中的预览、显式 apply 与 revision/CAS 验证。普通协作命令不需要先调用 State resume。

## 远端与 MCP 验收

状态在另一台机器时，使用[SSH CLI／MCP 与 hook bridge](deployment.md#控制端与状态端分离)。不要把 data-root 视为远程 URL，也不要为查询而复制原生账号、profile 或会话库。

安装后核对实际工具表与版本。旧 MCP 会话不热加载新工具，需要宿主支持的重连／新会话；不要重建项目 State、改 provider key 或进入反复重装循环。检索 server 在远端运行时，root 应为远端仓库路径；bridge 的 hook 映射尚不是检索的自动路径翻译。
