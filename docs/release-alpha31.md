# Alpha31 · 可靠 inbox 契约与结构化 MCP 错误

Reliable inbox contract and structured MCP errors

包 `0.1.0a31`；tag／插件 `v0.1.0-alpha.31`／`0.1.0-alpha.31`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- `continuity_inbox` 与 `collaboration inbox send` 的 `message_id` 改为可选。缺省时生成并返回稳定格式的最终 ID；显式传入仍是幂等键，相同内容重发不会重复投递。
- MCP 工具输入错误统一返回 MCP tool result：`{"status":"error","failed_gate":...,"message":...,"error_type":...}` 与 `isError:true`，不再把 Python `KeyError` 或裸异常字符串直接暴露给调用方。
- inbox `ack` 支持单条 `--message-id`、按接收方和时间的 `--before`、或受 `--limit` 限制的 `--all` 批量确认；`archive` 继续只移动已确认消息并可附加时间截止点。
- handoff 的 unread 摘要给出单条 ack、批量 ack 和 archive-after-ack 命令。root／project 注册不一致时会同时列出请求值与两边注册值，并给出纠正命令。
- 快速 handoff write 记录当前 change cursor；后续 show 只读取该边界之后的事件，不再每次重放到日志起点。旧 handoff 没有 cursor 时，执行一次新的 `handoff write` 即可建立快速边界。
- MCP schema、CLI argparse 与回归合同共同验证 `send` 的可选 ID、条件必填的 sender/body，以及 `ack` 的单条或批量确认入口。

## 推荐用法

```bash
# message_id 可省略；返回值包含自动生成的最终 ID
continuity collaboration inbox send --project-id demo --sender worker-a --recipient controller --body 'Review complete'

# 审阅时间窗后批量确认，再归档已确认消息
continuity collaboration inbox ack --project-id demo --recipient controller --before 2026-10-20T00:00:00Z --limit 1000
continuity collaboration inbox archive --project-id demo --recipient controller --limit 1000
```

MCP 可同样调用 `continuity_inbox`：`send` 使用 action／root／recipient／sender／body，可省略 message_id；`ack` 读取返回 ID，或用 before／all 与 limit 批量确认。所有错误应读取 `isError` 和 `structuredContent.failed_gate`，不要再解析裸异常文本。

## 安装与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a31'
continuity --version
```

pipx 环境使用 `pipx upgrade continuity-plane --pip-args=--pre`。升级三个 Codex 插件与 Claude 插件到同一版本，刷新插件并重连 MCP。已有消息 ID、归档文件和未确认消息不迁移、不重写；本版不自动替使用者确认历史消息。

## 验证与限制

本版回归覆盖 CLI 与 MCP 无 ID 发送、返回 ID 后 ack、显式 ID 重发幂等、按时间／all 的批量 ack、ack 后归档、缺失 sender 的结构化错误、planning 工具异常包装及 schema／argparse 条件字段一致性。归档仍不是删除，消息确认仍不是宿主唤醒。批量 ack 最多 1000 条；更大积压需重复显式批次或缩小时间窗。

## English

Alpha31 makes the inbox send message_id optional and returns the generated final ID. Explicit IDs remain idempotent. MCP tool failures now return a structured result with status, failed_gate, message, error_type and isError instead of leaking Python exceptions. Inbox acknowledgement supports one ID, a recipient/time cutoff, or a bounded all operation; archive continues to move only acknowledged messages to recoverable storage.

Handoff unread summaries expose single ack, bulk ack and archive-after-ack commands. Fast handoff writes record the current change cursor, so later reads inspect only events after that boundary; an older handoff without a cursor establishes one on its next write. Root/project registration mismatches report requested values and both registered identities. Schema and CLI regressions enforce optional send IDs, conditionally required sender/body, and single/bulk ack modes.

Install the exact pin above and reconnect matching host plugins. Existing message IDs and archives are not rewritten. Acknowledgement is not host wake-up, archive is not deletion, and bulk acknowledgement is bounded to 1000 messages per call.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
