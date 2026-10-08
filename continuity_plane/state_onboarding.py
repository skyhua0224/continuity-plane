"""Explicit, source-bound additions to typed planning State; never activate Work."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def record_changes(
    root: Path, snapshot: dict[str, Any], args: Any, now: str
) -> list[dict[str, Any]]:
    source = (root / args.source_file).resolve()
    if not source.is_relative_to(root) or source.is_symlink() or not source.is_file():
        raise ValueError("source-file must be an existing file inside the project root")
    if source.stat().st_size > 1024 * 1024:
        raise ValueError("source-file exceeds the 1 MiB planning document limit")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence_id = "evidence-plan-" + digest[:24]
    evidence = {
        "evidence_id": evidence_id,
        "kind": "artifact",
        "artifact_ref": f"{source.relative_to(root).as_posix()}#sha256={digest}",
        "content_sha256": digest,
        "validity": "candidate",
        "observed_at": now,
        "verified_at": None,
    }
    kind = args.record_kind
    if kind == "work":
        value = {
            "work_id": args.work_id,
            "kind": args.kind,
            "title": args.title,
            "status": "proposed",
            "parent_work_id": args.parent_work_id,
            "dependency_ids": sorted(set(args.dependency or [])),
            "owner_refs": [],
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": s}
                for s in sorted(set(args.scope or ["project-governance"]))
            ],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [evidence_id],
            "blocker_ids": [],
            "revision": snapshot["project"]["revision"] + 1,
        }
        collection, key = "works", "work_id"
    elif kind == "decision":
        value = {
            "decision_id": args.decision_id,
            "work_id": args.work_id,
            "status": args.record_status,
            "statement": args.statement,
            "decided_at": now,
            "supersedes_decision_id": None,
            "evidence_ids": [evidence_id],
        }
        collection, key = "decisions", "decision_id"
    else:
        value = {
            "constraint_id": args.constraint_id,
            "status": "active",
            "statement": args.statement,
            "scope_work_ids": sorted(set(args.work_id)),
            "expires_at": None,
            "supersedes_constraint_id": None,
            "evidence_ids": [evidence_id],
        }
        collection, key = "constraints", "constraint_id"
    known_work = {w["work_id"] for w in snapshot["works"]}
    refs = (
        value.get("dependency_ids", [])
        + ([value["parent_work_id"]] if value.get("parent_work_id") else [])
        if kind == "work"
        else [value["work_id"]]
        if kind == "decision"
        else value["scope_work_ids"]
    )
    if any(ref not in known_work for ref in refs):
        raise ValueError(
            "referenced Work is not in State; add parent/dependency Work first"
        )
    existing = next(
        (item for item in snapshot[collection] if item[key] == value[key]), None
    )
    if existing is not None:
        comparable = lambda item: {
            k: v for k, v in item.items() if k not in {"revision", "decided_at"}
        }
        if comparable(existing) == comparable(value):
            return []
        raise ValueError(
            "record ID already exists with different content; no record was overwritten"
        )
    changes = []
    if not any(item["evidence_id"] == evidence_id for item in snapshot["evidence"]):
        changes.append(
            {"collection": "evidence", "object_id": evidence_id, "value": evidence}
        )
    changes.append({"collection": collection, "object_id": value[key], "value": value})
    return changes
