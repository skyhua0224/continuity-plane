# Alpha34 · 区分内容拒绝与 HTTP 拦截

Separate provider policy refusals from HTTP interception

包 `0.1.0a34`；tag／插件 `v0.1.0-alpha.34`／`0.1.0-alpha.34`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- HTTP 401／403、Cloudflare／`cf-ray`、代理拦截、`Forbidden` 和 upstream/gateway 错误不再因为页面里出现 “policy violation” 之类的通用词而被当成模型内容拒绝。
- 分类优先级改为：明确的模型安全标记优先；若没有强标记但存在 HTTP／传输拦截，则归为 `authentication` 并允许原会话 resume；不得从头重跑。
- 强标记仍包括 `cyber_policy`、`content_policy`、`content_filter`、`safety_refusal` 和已有的明确安全拒绝措辞。因此“模型明确拒绝 + HTTP 403”不会被误判为普通传输故障。
- 已有旧 job 若仍被标记为 `policy-review`，resume 时会用原始 provider 证据重新分类；HTTP 拦截证据可自动纠正为 `authentication`。
- 对证据仍不充分或历史记录已混合的 job，主控可在复核后执行带精确 job ID、理由和审计身份的显式解除。

## 复核解除

```bash
continuity worker clear-policy-review \
  --project-id example \
  --task-id parser-fix \
  --job-id job-0123456789abcdef \
  --actor controller \
  --reason 'Reviewed the captured HTTP 403 interception page; no model safety refusal'
```

命令要求 job 属于指定项目和任务、具备 scope binding，并记录不可覆盖的 `dispatch.classification-corrected` 事件。结果是 `operator-cleared`：允许同一 recorded session 显式 resume，但不是 provider 鉴权，也不会放宽后续新 attempt 的新拒绝。重复执行是幂等的。若项目已有 controller lease，同时遵循该项目的 controller token fence。

`worker clear-policy-review` 只处理已经记录为 `policy-review` 的精确 job，不会清除新的策略拒绝、不会删除结果或日志，也不会自动向 provider 重放。

## 安装与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a34'
continuity --version
```

已有 job 与 result 格式兼容。升级后重连宿主 MCP；实际 worker dispatch／resume 仍由 CLI 执行。

## English

Alpha34 prevents generic words in HTTP 401/403, Cloudflare, `cf-ray`, proxy and gateway diagnostics from being promoted into content-policy refusals. Explicit model safety markers still win, including when the transport status is 403.

Legacy false `policy-review` results can be corrected from preserved provider evidence on resume when the failure is clearly HTTP/authentication. Ambiguous records can be cleared only through the exact-job, reason-required `worker clear-policy-review` command, which records an auditable correction event. The resulting `operator-cleared` status permits an explicit same-session resume but is not provider authentication and does not weaken later attempts.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
