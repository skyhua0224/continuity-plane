"""Small current-checkout retrieval routes, never a whole-codebase analysis."""

import json
import re
from pathlib import Path


def code_context(root, body, allowed_files, max_bytes=2048):
    from .code_index import lookup_code_index

    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,100}\b", body)
    identifiers = [s for s in identifiers if "_" in s or re.search(r"[a-z][A-Z]", s)]
    queries = list(dict.fromkeys([*allowed_files[:4], *identifiers[:4]]))[:6]
    queries = [
        q for q in queries if len(json.dumps(q, ensure_ascii=False).encode()) <= 256
    ]
    result = {
        "authority": "hash-bound retrieval routes; not complete impact analysis or edit authorization",
        "references": [],
        "queries": queries,
        "truncated": False,
    }
    seen = set()
    for query in queries:
        try:
            receipt = lookup_code_index(
                root,
                query=query,
                max_results=3,
                max_output_bytes=2048,
                record_usage=False,
            )
        except (OSError, ValueError, RuntimeError):
            result["unavailable_queries"] = result.get("unavailable_queries", 0) + 1
            continue
        for row in receipt["matches"]:
            path = Path(row["path"])
            if path.is_absolute() or ".." in path.parts:
                continue
            key = (row["path"], row.get("line"))
            if key in seen:
                continue
            seen.add(key)
            ref = {
                k: row.get(k) for k in ("path", "line", "name", "kind", "file_sha256")
            }
            ref["repository_revision"] = receipt["repository_revision"]
            result["references"].append(ref)
            if len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes - 128:
                result["references"].pop()
                result["truncated"] = True
                return result
    result["status"] = (
        "available" if result["references"] else "no-matching-indexed-references"
    )
    return result
