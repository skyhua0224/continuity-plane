---
name: bounded-code-search
description: Bounded large-repository code lookup.
---

# Bounded Code Search / 有界代码检索

- 陌生符号先调用一次 MCP `continuity_context_lookup`；仅工具不可用/报错时用 CLI `continuity context lookup`。 / Call MCP lookup first; CLI is fallback only.
- lookup 命中后直接复用，不查 help/cache，不再 `rg/find/read`。 / Reuse a hit without follow-up discovery.
- 文本检索运行 `continuity context search --root . --query "<term>" --max-results 40 --max-output-bytes 8192`。 / Use bounded text search.
- 用 1–3 个精确术语，只展开 receipt 的文件与行号。 / Use 1–3 terms and expand only returned refs.
- 只搜索 Git tracked current worktree；输出有 revision/hash，无权限。 / Tracked-worktree only; hash-bound and authority-free.
