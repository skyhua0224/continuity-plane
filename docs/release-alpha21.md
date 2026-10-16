# Alpha21 · 计划录入与消息交接

Plan intake and durable handoffs

包版本：`0.1.0a21`；tag：`v0.1.0-alpha.21`。历史版本功能范围按本页，不由当前文档反推。

## 这一版带来什么

- Work、决定与约束支持来源绑定的预览／revision apply，保留 claim、scope 与历史证据。
- task update 可指定执行者；同执行方按 lane 认领，子身份可区分实例。
- 持久 inbox 支持幂等发送、读取与确认；恢复包／hook 给出有界消息提示。
- 改进中英混排 alias、预期 CLI 错误与接入文档。

## 升级前注意

消息不会保证唤醒任意会话或抢占 writer，也不授予外部操作权限。新增 MCP 工具需要重连／重载。后续版本的 supervisor 与主控租约不属于此历史版本。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a21'
continuity --version
```

已有 pipx 用户见[部署与升级](deployment.md)；插件选择同一 tag，并在宿主重连。普通 GitHub Release 分类不改变 alpha 包的兼容性阶段。

## 验证与限制

包含双语接入说明与回归；未测量的 token 节省不作声明。历史发布范围保留，当前接入以最新部署指南为准。

## English

- Preview/apply source-bound Work, decisions and constraints while retaining claims/scopes/evidence.
- Update assignees and coordinate per-lane claims with child identities.
- Send/read/ack durable idempotent inbox messages with bounded packet/hook hints.
- Improve mixed-language aliases, expected CLI diagnostics and onboarding.

**Before upgrading:** Messages do not guarantee wake-up, preempt writers or authorize external actions. Reconnect for new MCP tools. Later supervisor/controller features are not part of this historical release.

Use the exact package pin above and matching plugin tag, then reconnect host/MCP sessions. GitHub's regular Release category does not change the alpha compatibility stage.

**Evidence and limits:** Bilingual onboarding/regressions were included, without an unmeasured savings claim. Preserve the historical scope; use current deployment guidance for new adoption.

[README](../README.md) · [Deployment](deployment.en.md) · [Usage](../USAGE.en.md) · [Current validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
