"""Short cursors over immutable local metadata snapshots; no invalid JSON truncation."""

import hashlib
import json
import re
import time
from .collaboration_registry import default_data_root, project_data_dir, _atomic_write


def tasks_page(
    data_root,
    project_id,
    items,
    *,
    query,
    budget,
    cursor=None,
    page_size=None,
    metadata=None,
):
    if not 1024 <= budget <= 131072:
        raise ValueError("output budget must be 1024–131072 bytes")
    query_hash = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    folder = (
        project_data_dir(data_root or default_data_root(), project_id) / "list-pages"
    )
    start = 0
    if cursor:
        match = re.fullmatch(r"([0-9a-f]{32}):(\d+)", cursor)
        if not match:
            raise ValueError("invalid task-list cursor")
        identity = match[1]
        start = int(match[2])
        path = folder / (identity + ".json")
        snapshot = json.loads(path.read_text())
        if snapshot["query_sha256"] != query_hash:
            raise ValueError(
                "cursor belongs to different filters; reuse the original filters"
            )
        if time.time() - snapshot["created_at"] > 3600:
            raise ValueError(
                "task-list snapshot expired; restart the list and use changes for updates"
            )
        items = snapshot["items"]
    else:
        identity = hashlib.sha256(
            (
                query_hash + json.dumps(items, sort_keys=True, ensure_ascii=False)
            ).encode()
        ).hexdigest()[:32]
        path = folder / (identity + ".json")
        if not path.exists() or time.time() - path.stat().st_mtime > 3600:
            _atomic_write(
                path,
                {"query_sha256": query_hash, "created_at": time.time(), "items": items},
            )
    if start > len(items):
        raise ValueError("cursor is past the snapshot")
    end = min(len(items), start + (page_size or len(items) or 1))
    selected = list(items[start:end])
    while True:
        end = start + len(selected)
        result = {
            **(metadata or {}),
            "items": selected,
            "total_items": len(items),
            "next_cursor": identity + ":" + str(end) if end < len(items) else None,
            "has_more": end < len(items),
            "truncated": end < len(items),
            "snapshot": identity,
            "returned_bytes": 0,
        }
        for _ in range(6):
            size = (
                len(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()) + 1
            )
            if size == result["returned_bytes"]:
                break
            result["returned_bytes"] = size
        if result["returned_bytes"] <= budget:
            return result
        if len(selected) > 1:
            selected.pop()
            continue
        if selected and not selected[0].get("details_omitted"):
            item = selected[0]
            selected = [
                {
                    k: v
                    for k, v in item.items()
                    if k in {"task_id", "title", "status", "evidence_gaps"}
                }
            ]
            selected[0]["details_omitted"] = True
            selected[0]["expand_command"] = (
                "continuity collaboration task show --task-id " + item["task_id"]
            )
            continue
        raise ValueError(
            "one task summary cannot fit the budget; increase max-output-bytes"
        )
