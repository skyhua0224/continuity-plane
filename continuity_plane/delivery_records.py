"""Bounded reports, commit-bound verification and declared integration evidence."""

import hashlib
import json
import re
from pathlib import Path


def read_report(path):
    with Path(path).open("rb") as stream:
        content = stream.read(65537)
    if not content or len(content) > 65536:
        raise ValueError("report must contain 1–65536 bytes")
    text = content.decode("utf-8")
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "text": text,
    }


def scope_hash(task, body):
    return hashlib.sha256(
        json.dumps(
            {
                "body": body,
                "objective": task["objective"],
                "allowed_files": task["allowed_files"],
                "exit_criteria": task["exit_criteria"],
                "worktree": task["worktree"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def verification_record(value, commit):
    if (
        not isinstance(value, dict)
        or value.get("commit") != commit
        or not re.fullmatch(r"[0-9a-f]{7,64}", commit or "")
    ):
        raise ValueError("verification record must name the exact task commit")
    if not isinstance(value.get("actor"), str) or not value["actor"].strip():
        raise ValueError("verification actor is required")
    checks = value.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
        raise ValueError("verification needs 1–64 structured checks")
    for check in checks:
        if (
            not isinstance(check, dict)
            or check.get("status") not in {"pass", "fail", "not-run"}
            or type(check.get("required")) is not bool
        ):
            raise ValueError(
                "each check needs status pass/fail/not-run and a required boolean"
            )
        if any(
            not isinstance(check.get(k), str) or not check[k].strip()
            for k in ("name", "command", "result")
        ):
            raise ValueError("each check needs name, command and an honest result line")
    if not isinstance(value.get("unverified", []), list) or not all(
        isinstance(v, str) for v in value.get("unverified", [])
    ):
        raise ValueError("unverified must be a string list")
    if len(json.dumps(value).encode()) > 32768:
        raise ValueError("verification record exceeds 32768 bytes")
    from .collaboration_registry import _now

    return {**value, "recorded_at": _now(), "authority": "executor-declared-evidence"}


def merge_gate(task, details):
    gaps = []
    if not task.get("branch") or not task.get("commit"):
        gaps.append("missing branch or commit")
    records = details.get("verification_records", [])
    if records:
        latest = next(
            (v for v in reversed(records) if v["commit"] == task["commit"]), None
        )
        if latest is None:
            return gaps + ["no verification bound to the current commit"]
        required = [c for c in latest["checks"] if c["required"]]
        if not required or any(c["status"] != "pass" for c in required):
            gaps.append("required checks missing, failed or not run")
    else:
        # Preserve legacy evidence behavior until a structured record is supplied.
        if not task.get("verification"):
            gaps.append("missing verification")
        elif re.search(
            r"\b(not (run|tested|built)|skipped|unverified)\b",
            task["verification"].casefold(),
        ):
            gaps.append(
                "legacy evidence mentions unverified items; use structured verification to distinguish optional checks"
            )
    return gaps


def detail_view(data_root, project_id, task_id, include_report=False):
    from .collaboration_registry import _load_ledger, default_data_root

    ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
    details = dict(ledger.get("task_details", {}).get(task_id, {}))
    if "report" in details and not include_report:
        details["report"] = {k: v for k, v in details["report"].items() if k != "text"}
    return details


def task_body(args):
    if getattr(args, "body", None) is not None and getattr(args, "body_file", None):
        raise ValueError("use body or body-file, not both")
    text = args.body
    if (
        getattr(args, "expected_body_sha256", None)
        and text is None
        and not getattr(args, "body_file", None)
    ):
        raise ValueError("expected-body-sha256 requires a body or body-file")
    if getattr(args, "body_file", None):
        report = read_report(args.body_file)
        text = report["text"]
        if report["bytes"] > 32768:
            raise ValueError(
                "task body exceeds 32 KiB; move long policy content to rule attachments"
            )
    if text is not None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("task body is empty")
        expected = getattr(args, "expected_body_sha256", None)
        if expected and hashlib.sha256(text.encode()).hexdigest() != expected:
            raise ValueError("task body digest does not match the expected task file")
    return text
