# M1-02 Source Registry Acceptance

版本：1  
日期：2026-08-09  
状态：🧑‍💻（实现与离线验收完成；真实 fixture 仍等待 M1-04 的受控提取与独立复核）

## 范围

本叶只处理聊天来源元数据、provider-neutral opaque thread/range references 和 provenance admission contract。原始 transcript、provider 原始 thread ID、archive path 和真实 fixture 不进入 Git。

## 验收证据

运行命令：

```text
python3 -m unittest tests.test_m1_02_source_registry -v
```

当前结果：15 tests passed。

覆盖项：

- 同一 `project_id + source_provider + provider_thread_id` 在同一 namespace key 下稳定映射到 `thr_...`；不同来源和不同 namespace key 不复用 opaque ref。
- base64url namespace secret 至少为 256 bit，编码值不进入公开 registry；`opaque_key_id` 拒绝路径、secret-like 值和空白。
- byte range 由 `thr_... + start + end` 稳定映射到 `rng_...`，非法 thread ref、负数和逆序 range 被拒绝。
- public registry record 和 provenance 只保留 opaque refs 与必要元数据；原始 provider thread ID、provider id、local/archive path 字段被拒绝。
- provenance 的 schema version、provider、RFC3339 时间、semver、SHA-256、classification、validity、evidence refs、retention 和敏感标记均受 validator 约束。
- `validity=verified` 必须引用 `artifact://` 或 `assertion:` current evidence；candidate 可以没有 evidence。
- `contains_sensitive_data=true` 可以保留在受控候选记录中，但 admission gate 必须拒绝。
- 未知字段和不支持的 schema version 被拒绝，确保未来版本通过显式 migration/replay 演进。

## 未完成门

M1-02 不授权导入聊天正文。M1-03 sanitizer 已完成，但 M1-04 的受控 byte-range 提取、current-evidence verification 和 independent validator 完成前，真实 fixture admission 仍保持关闭；因此本记录的 `🧑‍💻` 只表示本叶实现、TDD、离线测试和 admission guard 已完成。
