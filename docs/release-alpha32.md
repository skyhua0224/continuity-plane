# Alpha32 · 主机恢复、依赖与可解释调度

Host recovery, dependencies and explainable dispatch

包 `0.1.0a32`；tag／插件 `v0.1.0-alpha.32`／`0.1.0-alpha.32`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- 新 reservation 记录主机与 boot 指纹；旧 reservation 可用当前 boot 时间判定是否早于重启。`controller reservations` 列出 live／stale／unknown、job ID、attempt/result 状态和下一步。
- `controller reconcile-host` 默认 dry-run；取得控制器租约后 `--apply --reason …` 只清理重启或主机身份变化造成的 stale reservation。stale 不再占容量，容量不足前会先报告 stale 计数。
- 容量语义明确为“当前项目 + host + profile + client-family”。它不宣称能证明跨项目供应商账号的总并发。
- 本地任务支持 `--dependency` 与 `--dependencies-ready`；未完成依赖会阻止 claim、active 更新和 dispatch，并出现在 worker prompt。
- 本地与 Gitea 的 `task list` 都返回有界 envelope：`items`、`total_items`、`next_cursor`、`has_more`，不再因后端不同而返回 list／object 两种形状。
- 非代码决定允许不带 commit；如果给出 commit，仍严格校验。`dispatch-health show` 增加原因、task、attempt、观察时间和范围说明。
- 普通任务事件追加不再重读完整 JSONL 历史；大项目下 task add/update 不再为无 event-id 的事件做 O(history) 扫描。
- MCP 是调用宿主管理的 stdio 进程，主机重启后不会由 Continuity 状态库自行拉起；新宿主会话或 MCP 重连前可继续使用 CLI／显式 SSH remote CLI。

## 恢复命令

```bash
continuity controller reservations --project-id demo --state stale
continuity controller reconcile-host --project-id demo
continuity controller reconcile-host --project-id demo --apply --owner controller --token TOKEN --reason 'verified host reboot'
```

`clear-reservation` 现在返回明确的 `cleared`／`not-found`、task、job 和清扫前记录；精确 job ID 不匹配时不会清掉新 reservation。

## 安装与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a32'
continuity --version
```

已有卡片缺少 dependencies 时按空列表兼容读取。已有 reservation 缺少 boot 指纹时，结合 reserved_at 与当前 boot 时间判断；仍无法判断的保持 unknown，不自动清理。升级后需重连现有 MCP 会话。

## English

Alpha32 records host and boot fingerprints on new reservations and can identify legacy reservations older than the current boot. Controller reservation inventory distinguishes live, stale and unknown jobs, and an explicit reconcile command clears only verified host-restart leftovers after checking the controller lease.

Capacity is defined as current project + host + profile + client family; it does not claim to prove a cross-project provider-account limit. Local cards now support dependencies and dependency-ready filtering. Local and Gitea task lists share a bounded envelope. Non-code decisions may omit a commit, dispatch-health records explain pauses, and ordinary event appends no longer reread the full history.

The stdio MCP process is owned by its Codex/Claude host and does not survive a host reboot by itself. Reconnect the host MCP, or use the CLI/SSH route until it is available. Existing cards without dependencies remain compatible. Unknown legacy reservations are never auto-cleared.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
