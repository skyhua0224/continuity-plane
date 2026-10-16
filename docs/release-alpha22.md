# Alpha22 · 有界检索与协作视图

Bounded retrieval and collaboration views

包版本：`0.1.0a22`；tag：`v0.1.0-alpha.22`。历史版本功能范围按本页，不由当前文档反推。

## 这一版带来什么

- 扩展 C/C++/Objective-C 词法符号、配置键与多词 AND 查询，减少定位代码时的无关输出。
- 任务列表提供摘要、筛选与预算；补充 dispatched/completed/merged、branch/commit/verification 字段。
- 恢复入口标注 stale；packet 包含有界在途卡与 Todo 提示，检索计量记录返回字节。
- 提供宿主 lifecycle bridge 示例与直接 status --root 渲染。

## 升级前注意

该历史版本不包含后来 Alpha24/25 的全部派发闭环、主控租约与自动接续能力。usage 驱动 provider 自动移交没有在此版本启用。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a22'
continuity --version
```

已有 pipx 用户见[部署与升级](deployment.md)；插件选择同一 tag，并在宿主重连。普通 GitHub Release 分类不改变 alpha 包的兼容性阶段。

## 验证与限制

验证范围以当时发行的回归与方法为准；不为历史版本补造新的测试数或 token 节省率。新采用者请使用当前版本与当前验证矩阵。

## English

- Extend C/C++/Objective-C lexical symbols, config keys and multi-term AND retrieval.
- Add bounded task summaries/filters and dispatched/completed/merged evidence fields.
- Mark stale recovery and include bounded in-flight/Todo hints; measure retrieval response bytes.
- Provide host lifecycle bridge examples and direct status rendering.

**Before upgrading:** This historical version does not include all later Alpha24/25 dispatch/controller features. Usage-driven automatic provider handoff was not enabled.

Use the exact package pin above and matching plugin tag, then reconnect host/MCP sessions. GitHub's regular Release category does not change the alpha compatibility stage.

**Evidence and limits:** Retain the original regression/method scope; do not fabricate new historical test counts or savings. New adopters should use the current release/validation guide.

[README](../README.md) · [Deployment](deployment.en.md) · [Usage](../USAGE.en.md) · [Current validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
