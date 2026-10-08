# 计划录入与执行者交接

[English](onboarding.en.md)

## 类型化计划记录

MASTER 定义目标、顺序、决定和约束；State 记录类型化对象与事件。编辑 Markdown
不会自动改 State。使用以下命令显式录入，避免把旧文档中的完成标记当作验收证据。

```bash
continuity work add --root . --work-id stage-a --title "阶段 A" \
  --source-file .continuity/MASTER.md --scope testing
```

默认只返回预览和 `expected_revision`。确认后，使用相同参数并增加
`--apply --expected-revision REVISION`。写入通过 State MCP 的授权、CAS 和校验器，
保留现有 Work、claim 和证据，新增事件后更新 checkpoint 和 STATUS 投影。
相同记录重复录入不会产生重复事件；同 ID 不同内容被拒绝，不会覆盖原记录。

录入前已更新 canonical MASTER/STATUS 时，可显式加 `--refresh-source`：
预览会列出变化来源，应用将新来源证据与记录放入同一 State 事件。
有活动 claim 时须传其 owner 的 `--actor-ref`；保留 claim、scope、其他来源内容及旧证据。
命令不续租、不换发 claim，也不替其他执行者接管。

```bash
continuity decision add --root . --decision-id choice-a --work-id stage-a \
  --statement "采用本地执行" --source-file .continuity/MASTER.md
continuity constraint add --root . --constraint-id boundary-a --work-id stage-a \
  --statement "不改变生产配置" --source-file .continuity/MASTER.md
```

Work 默认是 `proposed`，不领取 claim。决定默认是 `proposed`；已有明确批准的决定
可显式指定 `--record-status accepted`。来源文件 hash 只证明记录来源，不证明测试
通过或 Work 已完成。依赖和父 Work 必须先录入。激活已录入 Work 使用现有
`work activate` 流程；标题和 scope 必须一致，未完成依赖不会被跳过。

如果返回 `committed-repair-required`，State 事件已经保存，不能假定回滚。
按 receipt 中的 `next_action` 修复 checkpoint，再运行 `checkpoint verify`。

## 任务卡与工作线

```bash
continuity collaboration task update --root . --task-id narrow-fix \
  --status active --assignee worker/subtask
```

注册的项目根可推导 `project_id`；也可显式传入 `--project-id`。
同一执行者可在不同 lane 各领取一张 active 卡，同一 lane 仍只允许一张。
子身份可使用 `worker/subtask`。任务卡只是协作元数据，不授予 State 或远端操作权限，
不阻断普通开发，也不替代项目的验收。

## 持久收件箱

```bash
continuity collaboration inbox send --root . --message-id handoff-a \
  --sender reviewer --recipient worker --body "请核对阶段 A 的测试证据"
continuity collaboration inbox list --root . --recipient worker
continuity collaboration inbox ack --root . --recipient worker --message-id handoff-a
```

MCP 提供 `continuity_inbox`，action 为 `send`、`list` 或 `ack`。
消息按项目和收件人隔离，同 ID 重发幂等。正文上限 2048 UTF-8 字节；长内容使用文件
引用，不发送密码或 token。处理完成后明确 ack；读取不会自动确认。

给 `continuity_collaboration_packet` 传入收件人身份时，返回最多三条有界消息提示。
插件生命周期 hook 也可在身份匹配时注入提示。消息是非权威交接资料，不是新的授权，
不会自动中断当前任务。使用 Session 身份收信时，收件人必须与实际 Session ID 一致。

本版本提供可靠投递和读取，**不保证唤醒休眠或被宿主占用的会话**，也不启动第二个
provider 进程抢占已有 writer。远端设备尚需显式同步协作数据或项目 handoff 文件。

## MCP 安装后验收

已运行的 MCP 客户端可能不会热加载新增 server 或新版工具表。安装包成功不代表当前
会话已加载：先检查工具列表，必要时按宿主支持的流程重连/重载；没有重载入口时启动
新会话。无需为此重建项目 State 或修改 provider key。工具不可见时普通开发继续，
可临时使用 CLI，不进入安装重试循环。

`continuity status --root .` 与 `continuity status render --root .` 等价。
支持根参数的命令也可使用 `continuity --root PROJECT_ROOT COMMAND ...`；
两处都传 root 时必须指向同一项目。
预期的 CLI 参数、文件和协作错误返回结构化错误与非零退出码，不输出 Python 堆栈。
