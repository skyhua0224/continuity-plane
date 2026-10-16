# Alpha24 · Worker 闭环与规则交付

Worker lifecycle and complete rules

包版本：`0.1.0a24`；tag：`v0.1.0-alpha.24`。历史版本功能范围按本页，不由当前文档反推。

## 这一版带来什么

- 正常 worker 开始／结束、退出码和报告进入记录，任务转为待评审或阻塞；支持持久补写与迟到结果保护。
- 从卡片与规则组装提示词，提供 dispatch、原会话 resume、report、分页 changes 和阻塞 wait。
- 记录 commit 绑定验证、集成批次、批准来源与卡片驱动 Todo；规则按全局／项目、受众、版本和附件交付。
- 显式 SSH hook／CLI bridge 与原机 token/cache 计量；推荐参考实际运行和 cooldown，策略拒绝不自动换渠道。

## 升级前注意

升级核心包后重连 MCP／新开宿主会话。不要复制账号、私有配置或会话库；可恢复产物使用持久存储。原生 Claude 自动额度查询计划撤销；不安装账号轮询服务。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a24'
continuity --version
```

已有 pipx 用户见[部署与升级](deployment.md)；插件选择同一 tag，并在宿主重连。普通 GitHub Release 分类不改变 alpha 包的兼容性阶段。

## 验证与限制

源码 2,348 项（31 跳过），公开测试 212 项；独立复审无阻塞，历史一次原机手动压缩通过。自动阈值与长期业务效果未验证。使用反馈指出提前改卡可能漏挂报告，当前验证矩阵明确保留该限制。

## English

- Record normal worker starts/finishes, exit codes and reports, project review/blocked status, replay pending events and protect against late results.
- Assemble prompts from cards/rules; dispatch, resume native threads, read reports and wait on paginated changes.
- Keep commit-bound verification, batches, attributable decisions, card-backed Todo and complete audience/versioned rules.
- Use explicit SSH hook/CLI bridging and owning-host token/cache ingestion; route from actual runs/cooldowns without policy-refusal failover.

**Before upgrading:** Reconnect MCP/start a new host session after upgrading. Do not migrate credentials/private profiles/native sessions. Keep recoverable artifacts persistent. Native Claude automatic quota polling is withdrawn.

Use the exact package pin above and matching plugin tag, then reconnect host/MCP sessions. GitHub's regular Release category does not change the alpha compatibility stage.

**Evidence and limits:** Source: 2,348 tests (31 skipped); public: 212; independent review found no blocker. A historical owning-host manual compaction passed, not automatic-threshold/longitudinal acceptance. Early card/report gaps remain documented.

[README](../README.md) · [Deployment](deployment.en.md) · [Usage](../USAGE.en.md) · [Current validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
