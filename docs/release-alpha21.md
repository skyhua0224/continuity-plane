# Continuity Plane 0.1.0-alpha.21

## 中文

本次更新收口计划录入和多执行者协作的接入问题：

- Work、决定和约束可显式录入类型化 State：先预览，再按 revision 应用；不自动把文档中的完成标记当成验收。
- 可在同一 State 事件中刷新 canonical 来源证据；保留现有 claim、scope 和历史证据。
- task update 可直接指定执行者，同一执行者可按 lane 并行领取。
- “Admin 方案”等中英混排别名可匹配空格、大小写及全角变体。
- CLI/MCP 持久收件箱支持幂等发送、读取和确认；恢复包及 hook 可注入有界提示。
- CLI 预期错误不再直接输出 Python 堆栈；支持直接 status 和统一根参数。

收件箱不保证唤醒被宿主占用的会话，不抢占 writer，不授予部署权限。运行中的 MCP
客户端仍可能需要重连/重载；请检查实际工具列表。HTML 原型和 usage 自动移交不在本次发行中。

安装核心包：

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a21'
continuity --version
```

Codex 插件按 [README](../README.md) 中的 marketplace 安装方式更新；核心、Search、State
三个插件均为 `0.1.0-alpha.21`。GitHub 与 Python 包使用各自的标准版本格式。
已包含双语接入文档和回归测试；本次不宣称未经测量的 token 节省。

## English

This release closes typed-planning and multi-executor onboarding gaps:

- Explicit typed Work/decision/constraint additions: preview, then revision-bound apply. Document completion markers are not acceptance evidence.
- Canonical source evidence can be refreshed in the same State event, preserving claims, scope and historical evidence.
- Task update accepts an assignee, and active assignment is counted per lane.
- Mixed-language phrase aliases support whitespace, case and full-width normalization.
- A durable CLI/MCP inbox provides idempotent delivery, retrieval and acknowledgement, with bounded packet/hook hints.
- Expected CLI errors produce concise diagnostics; direct status and consistent root arguments are supported.

The inbox does not guarantee wake-up of host-owned sessions, take over writers or grant deployment
authority. Running MCP clients may need reconnect/reload; inspect their actual tool catalog.
The HTML prototype and usage-driven automatic handoff are excluded.

Install using the command above. Update the three Codex plugins through the marketplace steps in
[README](../README.md); all are `0.1.0-alpha.21`, while the Python distribution is `0.1.0a21`.
Bilingual onboarding documentation and regression tests are included. No unmeasured token savings are claimed.
