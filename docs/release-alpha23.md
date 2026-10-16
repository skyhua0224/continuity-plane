# Alpha23 · 双端接续与执行建议

Shared continuation and executor guidance

包版本：`0.1.0a23`；tag：`v0.1.0-alpha.23`。历史版本功能范围按本页，不由当前文档反推。

## 这一版带来什么

- Codex 与 Claude 采用有界、会话隔离的压缩交接，重复恢复去重。
- 提供 Claude 原生插件、marketplace、生命周期 hook 与 MCP 入口；CLI 与 MCP 握手版本统一。
- 计量包含工具返回与 hook 注入成本，净字节估算允许为负。
- 加入场景预设、思考档位、拥挤／不可用提示与任务拆分建议；当时的均分依据是保存过的计划，不是实际费用。

## 升级前注意

插件安装与 hook 信任是两件事。检查实际工具表，重载／新开会话，不要通过覆盖 provider 配置来修复接入。后续实际运行均分属于新版本改进，不倒写到此版本。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a23'
continuity --version
```

已有 pipx 用户见[部署与升级](deployment.md)；插件选择同一 tag，并在宿主重连。普通 GitHub Release 分类不改变 alpha 包的兼容性阶段。

## 验证与限制

公开套件 87 项；源码 lifecycle/release 套件 89 项，相关 routing/handoff 套件 55 项；安装与事件 fixture 有验证。不宣称在每名用户宿主完成了自然压缩实测。

## English

- Share bounded, session-isolated, deduplicated compaction handoffs across Codex and Claude.
- Ship the Claude plugin/marketplace/hooks/MCP entrypoints and align CLI/MCP identity.
- Include output and hook-injection overhead; signed net-byte estimates may be negative.
- Add scenario/effort/congestion/split guidance. This historical version balances saved plans, not measured cost.

**Before upgrading:** Plugin installation is separate from hook trust. Inspect tools and reconnect without replacing provider configuration. Later actual-run balancing is not retroactively claimed for this version.

Use the exact package pin above and matching plugin tag, then reconnect host/MCP sessions. GitHub's regular Release category does not change the alpha compatibility stage.

**Evidence and limits:** Public suite: 87; source lifecycle/release: 89; related routing/handoff: 55. Installation/event fixtures were checked, not natural compaction on every user's host.

[README](../README.md) · [Deployment](deployment.en.md) · [Usage](../USAGE.en.md) · [Current validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
