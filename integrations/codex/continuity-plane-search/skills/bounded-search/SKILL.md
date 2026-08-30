---
name: bounded-code-search
description: Use for large-repository architecture, build, test, or symbol lookup when broad shell scans would return excessive output.
---

# Bounded Code Search / 有界代码检索

- 宽泛 `rg`、`find` 或整文件读取前，先运行：`continuity context search --root . --query "<exact term>" --max-results 40 --max-output-bytes 8192`。 / Run this bounded search before broad scans.
- 使用 1–3 个精确术语逐步缩小范围，只展开 receipt 返回的当前文件与行号。 / Narrow with 1–3 exact terms and expand only returned current paths and lines.
- 该命令只搜索 Git tracked current worktree，输出含 revision/hash 且无写权限。无结果时再使用带范围限制的 `rg`、LSP 或项目索引。 / It is read-only and hash-bound; fall back to bounded `rg`, LSP, or the project index only when needed.
