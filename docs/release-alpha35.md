# Alpha35 · 可观察派发与可恢复等待

Observable dispatch and resumable waits

包 `0.1.0a35`；tag／插件 `v0.1.0-alpha.35`／`0.1.0-alpha.35`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- **等待不再依赖时间窗。** `worker events --follow` 输出 started／progress／completed／failed／rate-limited／blocked，并从单调 cursor 续读；`worker events --all-projects --follow` 使用 `project:cursor` 对继续多个项目。
- **收件箱可阻塞等待。** `collaboration inbox wait --recipient ... --follow` 等待新消息，`inbox unread --count` 只读未确认计数，不展开消息正文。
- **worker 终态只有一个读法。** `job.json`、`worker-jobs.json` 和 `result.json` 都记录 status、退出码、原因和完成时间；`worker jobs --status running|ended` 提供有界列表。
- **现场证据更接近实际。** 进程启动后记录 PID、host、进程身份、实际 cwd 和 Git HEAD；卡片的 planned worktree 不会被伪写成实际路径。心跳附带无输出时长，并把明确的 build-lock 等待标为诊断提示，不自动杀进程。
- **派发不再为历史买单。** supervisor 先读当前 execution bindings；没有运行中 attempt 时不重放 change feed，有绑定时从该 attempt 的启动 cursor 增量核对。旧绑定只有有限尾窗回退。
- **容量与额度边界更清楚。** 项目容量仍是“项目 + host + profile + client family”；新增可选 host 级声明容量，跨项目执行本地 gate。额度 reset 到点后视为 expired，`dispatch-health clear` 可按模型查找真实 provider 记录，不要求猜 provider。
- **fallback 可以成为项目本机策略。** `worker fallback-policy set` 只使用已配置 adapter；仅在同会话 transient retry 耗尽后启动候选，认证失败和内容策略拒绝不会自动换渠道。所有权切换记录来源、目标、理由和 actor。
- **卡片更新更稳。** `status=queued` 保留 assignee，只有显式 `--unassign` 才清空；活动 controller lease 持有者可凭 `--reason` 改派。`superseded` 与 completed／merged 一样满足依赖。
- **大任务集有明确边界。** `task show` 默认预算提高到 64 KiB；list 继续使用临时快照 cursor 分页。“Depends on:”只产生迁移提示，Continuity 不从自由正文猜依赖。
- **可选的仓库写保护。** `collaboration context-set --worker-write-policy canonical-read-only --canonical-repository-root ...` 给 worker PATH 加 Continuity Git wrapper，拦截对指定 canonical repository 的 push／update-ref／receive-pack。
- **可选完成钩子不拖住 worker。** `worker completion-hook set --argv-json ...` 使用 local argv（不经 shell）异步启动并记录 PID、日志和幂等 receipt；钩子失败只警告。

## 升级与配置

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a35'
continuity --version
```

升级后重连宿主 MCP。已有卡片、事件、队列和 job 继续可读；旧 worker job 没有新终态或 cwd 证据时显示 unknown／缺失，不补造事实。

可按需配置明确的本地策略：

```bash
continuity controller host-capacity --project-id demo \
  --owner controller --token "$CONTROLLER_TOKEN" \
  --host-client-limit total=8

continuity worker fallback-policy set --project-id demo \
  --model primary --candidate secondary

continuity collaboration context-set --project-id demo \
  --integration-root /workspace/demo --integration-base main \
  --worker-write-policy canonical-read-only \
  --canonical-repository-root /workspace/demo
```

host capacity 是 operator 对当前 host 的声明，不是 provider 账号总额度。Git wrapper 不是安全沙箱：worker 若使用绝对 `git` 路径仍可绕过；应配合运行账号、挂载或宿主隔离。wrapper 只在本机持久数据目录生成，不把项目状态或凭据复制到另一台机器。

## 验证与限制

本版回归覆盖：cursor 尾流与断线续读、inbox wait／unread、无历史 supervisor 快路径、实际 cwd／HEAD、lock idle 诊断、job 终态、跨项目 host capacity、quota expiry、项目 fallback 改派、queued assignee 语义、controller lease 改派、Git guard 和异步 completion hook。Windows 原生 hook、真实 provider 账号容量和真实双机等待链路仍需对应宿主现场验收。

本机匿名现场的只读派发预览两次为 3.84 秒、3.73 秒，未启动 worker。约 52.9 MB／19.8 万条的事件历史建立派生索引后，最近五条定位两次为 14.2 ms、14.3 ms，只需读取约 1.2 KiB 尾部。首次索引约 2.03 秒；这些数字是当前主机负载下的现场样本。

较广源码检查中，1518 项运行有 31 项按环境跳过；唯一失败是既有 M7 实验 receipt 的环境绑定。该证据仍绑定当时的 Python 3.14.6 与源码哈希，未把旧实验记录改写为新版本收益。

## English

Alpha35 adds resumable worker-event and inbox waits, terminal state in both job documents and the worker index, actual process cwd/HEAD observations, lock-idle diagnostics, an indexed supervisor hot path, optional host-wide capacity, expiring health records, explicit project fallback policy, safer queued/reassignment semantics, bounded task reads, an opt-in canonical-repository Git wrapper, and a non-blocking completion hook.

The event stream uses cursors rather than time windows. Capacity declarations remain local operator policy, not native account quota. The Git wrapper is bypassable by absolute-path invocation and is not an OS sandbox. Native Windows hook delivery, real provider quota and two-machine stream continuity remain unverified in this release.

Two read-only field dispatch previews took 3.84 s and 3.73 s without starting a worker. After indexing a roughly 52.9 MB journal, two recent-tail lookups took 14.2 ms and 14.3 ms; the initial index took about 2.03 s. These are current-load samples. The broader 1518-test source group skipped 31 environment-dependent checks; its sole failure was an unchanged historical M7 receipt tied to an older Python/tree environment. Historical evidence was retained.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
