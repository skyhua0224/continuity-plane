# Alpha33 · 派发热路径与历史规模解耦

Dispatch hot path independent of history size

包 `0.1.0a33`；tag／插件 `v0.1.0-alpha.33`／`0.1.0-alpha.33`。普通 Release；保持 alpha 兼容阶段。

## 这一版带来什么

- `worker dispatch` / `worker resume` 不再为每次启动反复分页重放完整 `changes.jsonl`。A33 建立派发安全事件派生索引：首次升级做一次顺序回填，之后只消费新增尾部；索引损坏、日志替换或截断时从权威 JSONL 重建。
- 策略拒绝、待复核和分类更正仍以原 change log 为准；索引只加速，不另造一层可覆盖的任务权威。旧 worker result 在第一次建索引时顺序回填，之后不再逐次遍历全部 job 目录。
- task body 为空会在加载变更历史和 worker 目录前直接失败；仍需读取当前 `dispatch.json` 快照来确认卡片正文。
- `collaboration batch-list` 接受更大的 `--max-output-bytes` 请求，但实际输出最多 131072 字节；记录较多时返回 `next_cursor`、`has_more` 和 `budget_capped_to`，可继续读取同一不可变快照。
- 控制器租约区分过期、owner 不匹配、缺少 token 和 token 不匹配。误用其他项目 token 时会明确说明 token 是 project-scoped，且不会输出任一侧 token。

## 派发热路径

```bash
continuity worker resume --project-id example --task-id parser-fix --background
continuity collaboration batch-list --project-id example --max-output-bytes 200000 --format json
continuity controller reconcile-host --project-id example --apply --owner controller --token TOKEN --reason 'verified host reboot'
```

两个本机既有大项目的实测中，首次安全索引回填分别约 0.571 s 与 0.100 s，后续恢复约 0.001 s；其中一个约 34 MB 的变更历史对应的卡片快照解析约 0.046 s。首次数值取决于状态大小，日常派发不再随变更历史线性或二次增长。

`changes.jsonl` 仍是审计权威。本版没有自动删除或改写历史日志的命令；压缩或归档若未来提供，必须显式处理 consumer 游标。当前可使用 `changes --since` 或持久 `--consumer` 做有界增量读取。派生索引丢失或损坏只会触发一次安全重建，不应手工把缺失索引当作“没有拒绝”。

## 安装与兼容

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a33'
continuity --version
```

已有项目无需迁移任务卡。第一次派发会创建约一个受限派生索引文件；不会删除 change log、worker result、报告或 thread 文件。升级后需重连宿主中的 MCP 进程。

## English

Alpha33 removes repeated full-history paging from worker dispatch and resume. A derived dispatch-safety index performs one sequential backfill on first use, then consumes only appended journal bytes. Corruption, replacement or truncation rebuilds it from the authoritative change log. Empty task bodies fail before policy-history and worker-directory work.

`batch-list` caps an oversized requested budget at 131072 bytes and returns an immutable snapshot cursor instead of rejecting the command. Controller lease errors now distinguish expiry, owner mismatch, a missing token and a project-scoped token mismatch without printing either token.

On the observed local projects, first safety-index backfill took about 0.571 s and 0.100 s; subsequent reads took about 0.001 s. The change log remains authoritative. A33 adds no automatic history deletion or rewrite command; future compaction must explicitly reconcile consumer cursors. Use `changes --since` or a persisted `--consumer` for bounded incremental reads.

[Deployment](deployment.en.md) · [Validation](validation.en.md) · [Changelog](../CHANGELOG.en.md)
