# Alpha25 · 主控交接与可靠派发

Controller continuity and reliable dispatch

包版本：`0.1.0a25`；tag：`v0.1.0-alpha.25`。历史版本功能范围按本页，不由当前文档反推。

## 这一版带来什么

- 派发、prepare、后台与续跑可传总时长／空闲超时；新任务默认 24 小时／90 分钟，effort 按适配器支持原样透传。
- 共享主控交接带计划、批次、在途 worker 与待决事项；集成 ref 与检出 HEAD 分开，session 可绑定任务／批次。
- 完整规则导出、规则镜像检查、项目 lesson 和有界代码路标补齐执行方开场。
- 显式主控租约、原子派发容量与串行集成操作；修复批次 fence、接替预留空窗与迟到结果覆盖确认的问题。

## 升级前注意

首次 controller acquire 会启用该项目的 guarded ledger v4。先升级所有派发 CLI/MCP；安装本身不启用租约。已有连接要重连。提前改卡的报告挂载仍有已知限制，见当前验证矩阵。

## 安装与升级

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a25'
continuity --version
```

已有 pipx 用户见[部署与升级](deployment.md)；插件选择同一 tag，并在宿主重连。普通 GitHub Release 分类不改变 alpha 包的兼容性阶段。

## 验证与限制

发版源码回归 2,404 项（31 跳过），公开测试 268 项；独立复审关闭已确认问题。合成／受控 hook 不代替所有宿主的自然生命周期验证；无统一省 token／费用承诺。

## English

- Pass total/idle limits through dispatch and resume; new attempts default to 24 hours / 90 minutes. Adapter-supported effort is preserved.
- Recover a shared controller plan, batch, workers and pending decisions; distinguish integration refs from checkout HEAD and bind sessions to tasks/batches.
- Export complete rules, check mirrors, reuse lessons and attach bounded code routes.
- Opt into controller leases, atomic reservations and serialized integration; fix stale-fence writes, fallback gaps and late acknowledgement overwrites.

**Before upgrading:** First controller acquire enables guarded ledger v4 for that project: upgrade every dispatcher first. Installation does not enable it. Reconnect existing MCP clients. Early card updates can still skip report attachment; see current validation.

Use the exact package pin above and matching plugin tag, then reconnect host/MCP sessions. GitHub's regular Release category does not change the alpha compatibility stage.

**Evidence and limits:** Release source regression: 2,404 tests (31 skipped); public suite: 268. Independent follow-up closed the confirmed findings. Controlled tests do not prove every native lifecycle or universal savings.

[README](../README.md) · [Deployment](deployment.en.md) · [Usage](../USAGE.en.md) · [Current validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
