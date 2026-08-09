# M2-04 Artifact Store Acceptance

版本：1  
日期：2026-08-10  
状态：accepted local artifact backend

```yaml
document_id: context.m2-04-artifact-store-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-local-benchmark-2026-08-10
supersedes: null
affected_tasks: [M2-04, M5-04, M6-07]
next_review: M2-05
```

## 范围

M2-04 提供默认零服务的本地 content-addressed artifact store。`ArtifactRef` 使用严格版本化的 `context.artifact-ref/v1alpha1` 合同；对象按 SHA-256 写入本地 CAS，写入过程采用临时文件、fsync 和原子发布。读取支持受 `max_range_bytes` 限制的 bounded range，并在返回任何内容前校验完整对象的大小与 checksum。

该实现保存大日志、diff 和证据对象，不授予 active state、task、claim、effect 或 vector index 权威权限。PostgreSQL、Docmost、Temporal、OTel 和独立 daemon 均不属于默认依赖。

## 验证

| 门 | 结果 |
|---|---|
| ArtifactRef strict schema | `schemas/registry.yaml` 注册、artifact SHA-256 与 schema 文件匹配 |
| 定向测试 | 16/16 passed |
| streamed put / deduplication | 短读流、空对象、并发 writer 和单对象复读通过 |
| bounded range | offset/length 边界、最大范围和零长度读取通过 |
| integrity faults | 缺失、截断、bit flip、对象 symlink、断开的 root/shard symlink 和部分写入均返回 typed error |
| external services | 0 |
| full repository tests | 298 passed，27 个 PostgreSQL tests 因当前进程无 DSN 跳过 |
| Python compile / repository verifier | 通过 |

## 量化结果

[`m2-04-artifact-store-results.yaml`](../../experiments/state/m2-04-artifact-store-results.yaml) 是由 [`tools/run_artifact_benchmark.py`](../../tools/run_artifact_benchmark.py) 生成的本地 receipt。基准使用 1 MiB 对象和 8 KiB 区间：完整读取为 1,048,576 bytes，bounded read 为 8,192 bytes，直接读取量下降 `99.2188%`；完整对象和区间输出的 SHA-256 均通过校验。该结果描述当前 Linux 主机，不代表其他设备的性能。

基准复验命令：

```text
.venv/bin/python tools/run_artifact_benchmark.py \
  --payload-bytes 1048576 --range-bytes 8192 \
  --observed-at 2026-08-10T12:15:00+08:00 \
  --output experiments/state/m2-04-artifact-store-results.yaml
```

## 边界与后续

对象的存储和范围读取已经具备本地证据引用所需的完整性门。State MCP 对 artifact ref 的授权提交、immutable checkpoint/canary、Execution Packet 的 bounded expansion，以及远端或共享 artifact backend 分别属于 M2-05、M2-06、M5-04 和后续可选 adapter；本验收不将本地对象存储误报为共享状态服务。

完成门要求的 checksum、range read 和损坏检测均已通过，active queue 可推进至 M2-05。
