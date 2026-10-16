"""Versioned audience rules and immutable policy attachments outside Git."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    _ID_RE,
    _text,
    _now,
    project_data_dir,
    default_data_root,
    resolve_project,
    list_changes,
)


def _directory(data_root, project_id, scope="project"):
    if scope not in {"project", "global"}:
        raise ValueError("scope must be project or global")
    data = Path(data_root or default_data_root())
    return (
        data / "global-rules"
        if scope == "global"
        else project_data_dir(data, project_id)
    )


def _blob(directory, content):
    digest = hashlib.sha256(content).hexdigest()
    path = directory / "rule-attachments" / (digest + ".txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("rule attachment integrity failure")
    else:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rule-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {
        "sha256": digest,
        "bytes": len(content),
        "read_command": f"continuity rules attachment --sha256 {digest}",
    }


def put_rule(
    data_root,
    *,
    project_id,
    rule_id,
    text,
    audience="all",
    attachment_file=None,
    update=False,
    scope="project",
):
    if not isinstance(rule_id, str) or _ID_RE.fullmatch(rule_id) is None:
        raise ValueError("rule_id is invalid")
    _text(text, "rule", 4096)
    _text(audience, "audience", 128)
    directory = _directory(data_root, project_id, scope)
    attachment = None
    if attachment_file:
        with Path(attachment_file).open("rb") as stream:
            content = stream.read(262145)
        if not content or len(content) > 262144:
            raise ValueError("policy attachment must contain 1–262144 bytes")
        content.decode("utf-8")
        attachment = _blob(directory, content)
    path = directory / "rules.json"
    with _locked(path):
        rows = json.loads(path.read_text()) if path.exists() else []
        previous = next((r for r in rows if r["rule_id"] == rule_id), None)
        if previous and not update:
            raise ValueError("rule_id already exists; use rules update")
        if previous and attachment_file is None:
            attachment = previous.get("attachment")
        value = {
            "rule_id": rule_id,
            "text": text,
            "audience": audience,
            "status": "active",
            "attachment": attachment,
        }
        if previous and all(previous.get(k) == v for k, v in value.items()):
            return previous
        revision = (previous or {}).get("revision", 1 if previous else 0) + 1
        item = {**value, "revision": revision, "updated_at": _now()}
        history = directory / "rule-history" / rule_id / f"{revision}.json"
        if previous:
            old_revision = previous.get("revision", 1)
            old_path = directory / "rule-history" / rule_id / f"{old_revision}.json"
            if not old_path.exists():
                _atomic_write(old_path, previous)
        _atomic_write(history, item)
        rows = [r for r in rows if r["rule_id"] != rule_id] + [item]
        _atomic_write(path, rows)
    from .collaboration_registry import _record_change

    if scope == "project":
        _record_change(
            Path(data_root or default_data_root()),
            project_id,
            "rule.updated",
            rule_id,
            revision=revision,
            audience=audience,
        )
    return item


def bundle(data_root, *, project_id, audience="worker", lane_id=None, scope=None):
    merged = {}
    for scope in ("global",) if scope == "global" else ("global", "project"):
        path = _directory(data_root, project_id, scope) / "rules.json"
        for row in json.loads(path.read_text()) if path.exists() else []:
            merged[row["rule_id"]] = {**row, "scope": scope}
    rows = list(merged.values())
    audiences = {"all", audience}
    if lane_id:
        audiences.add("lane:" + lane_id)
    selected = sorted(
        [
            r
            for r in rows
            if r.get("status") == "active" and r.get("audience") in audiences
        ],
        key=lambda r: r["rule_id"],
    )
    for row in selected:
        if row.get('attachment'):
            row['attachment']={**row['attachment'],'read_command':f"continuity rules attachment --project-id {project_id} --scope {row['scope']} --sha256 {row['attachment']['sha256']}"}
    digest = hashlib.sha256(
        json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "project_id": project_id,
        "audience": audience,
        "rules": selected,
        "rule_count": len(selected),
        "rules_digest": digest,
        "complete": True,
        "export_command": f"continuity rules export --project-id {project_id} --audience {audience}",
    }


def attachment(
    data_root, *, project_id, sha256, offset=0, max_chars=8192, scope="project"
):
    import re

    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("attachment sha256 is invalid")
    content = (
        _directory(data_root, project_id, scope)
        / "rule-attachments"
        / (sha256 + ".txt")
    ).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError("rule attachment integrity failure")
    text = content.decode("utf-8")
    if not 0 <= offset <= len(text) or not 1 <= max_chars <= 65536:
        raise ValueError("attachment range is invalid")
    end = min(len(text), offset + max_chars)
    return {
        "sha256": sha256,
        "offset_chars": offset,
        "next_offset_chars": end if end < len(text) else None,
        "total_chars": len(text),
        "text": text[offset:end],
        "complete": end == len(text),
    }


def affected(data_root, *, project_id):
    starts = {}
    finished = set()
    cursor = 0
    while True:
        page = list_changes(
            data_root, project_id=project_id, since=cursor, max_results=500
        )
        for r in page["changes"]:
            key = (r.get("task_id"), r.get("attempt_id"))
            if r["event"] == "attempt.started":
                starts[key] = r
            elif r["event"] == "attempt.finished":
                finished.add(key)
        cursor = page["next_cursor"]
        if not page["truncated"]:
            break
    items = []
    for key, r in starts.items():
        if key in finished:
            continue
        current = bundle(
            data_root,
            project_id=project_id,
            audience=r.get("rules_audience") or "worker",
            lane_id=r.get("lane_id"),
        )["rules_digest"]
        if r.get("rules_digest") != current:
            items.append(
                {
                    "task_id": key[0],
                    "attempt_id": key[1],
                    "assignee": r.get("assignee"),
                    "received_digest": r.get("rules_digest"),
                    "current_digest": current,
                    "status": "changed" if r.get("rules_digest") else "unknown",
                }
            )
    return {"items": items, "count": len(items)}


def snapshot_export(data_root, project_id, scope="project"):
    import base64

    folder = _directory(data_root, project_id, scope)
    source = folder / "rules.json"
    rows = json.loads(source.read_text()) if source.exists() else []
    attachments = {}
    for row in rows:
        if row.get("attachment"):
            digest = row["attachment"]["sha256"]
            content = (folder / "rule-attachments" / (digest + ".txt")).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("attachment integrity failure")
            attachments[digest] = base64.b64encode(content).decode("ascii")
    document = {
        "schema_version": "context.rule-snapshot/v1alpha1",
        "scope": scope,
        "namespace": project_id if scope == "project" else "global",
        "rules": rows,
        "attachments": attachments,
    }
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    path = (
        Path(data_root or default_data_root()) / "rule-snapshots" / (digest + ".json")
    )
    if not path.exists():
        _atomic_write(path, document)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rule_count": len(rows),
        "attachment_count": len(attachments),
        "scope": scope,
    }


def snapshot_import(
    data_root, project_id, file, scope="project", force=False, reason=None
):
    import base64

    if force and not reason:
        raise ValueError("forced import requires a reason")
    with Path(file).open("rb") as stream:
        content = stream.read(2097153)
    if len(content) > 2097152:
        raise ValueError("rule snapshot exceeds 2 MiB")
    doc = json.loads(content)
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != "context.rule-snapshot/v1alpha1"
        or doc.get("scope") != scope
    ):
        raise ValueError("snapshot schema/scope mismatch")
    rows = doc.get("rules")
    blobs = doc.get("attachments", {})
    if not isinstance(rows, list) or not isinstance(blobs, dict):
        raise ValueError("invalid snapshot")
    validated = {}
    for digest, encoded in blobs.items():
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("snapshot attachment digest/size mismatch")
        raw.decode("utf-8")
        validated[digest] = raw
    folder = _directory(data_root, project_id, scope)
    current_path = folder / "rules.json"
    current = json.loads(current_path.read_text()) if current_path.exists() else []
    existing = {r["rule_id"]: r for r in current}
    conflicts = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or _ID_RE.fullmatch(row.get("rule_id", "")) is None
        ):
            raise ValueError("invalid snapshot rule id")
        _text(row.get("text"), "rule", 4096)
        _text(row.get("audience"), "audience", 128)
        if row.get("attachment") and row["attachment"]["sha256"] not in validated:
            raise ValueError("snapshot is missing an attachment")
        old = existing.get(row["rule_id"])
        if old and any(
            old.get(k) != row.get(k) for k in ("text", "audience", "attachment")
        ):
            conflicts.append(row["rule_id"])
    if conflicts and not force:
        return {
            "status": "conflict",
            "conflicts": conflicts,
            "applied": 0,
            "next_action": "review differences; use --force --reason only for an intended replacement",
        }
    for raw in validated.values():
        _blob(folder, raw)
    applied = []
    for row in rows:
        attachment_file = (
            folder / "rule-attachments" / (row["attachment"]["sha256"] + ".txt")
            if row.get("attachment")
            else None
        )
        value = put_rule(
            data_root,
            project_id=project_id,
            rule_id=row["rule_id"],
            text=row["text"],
            audience=row["audience"],
            attachment_file=attachment_file,
            update=row["rule_id"] in existing,
            scope=scope,
        )
        applied.append({"rule_id": value["rule_id"], "revision": value["revision"]})
    receipt = {
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
        "scope": scope,
        "applied": applied,
        "reason": reason,
        "at": _now(),
    }
    _atomic_write(folder / "sync-receipts" / (uuid_hex() + ".json"), receipt)
    return {
        "status": "imported",
        "applied": len(applied),
        "conflicts_overridden": conflicts,
        "reason": reason,
    }


def uuid_hex():
    import uuid

    return uuid.uuid4().hex


def export_text(value):
    lines = ["# Continuity rules (generated; edit rules in Continuity, not this export)",
             "Rules digest: " + value["rules_digest"], ""]
    for rule in value["rules"]:
        lines.extend(["## " + rule["rule_id"], rule["text"]])
        if rule.get("attachment"):
            lines.append("Policy attachment: " + json.dumps(rule["attachment"], ensure_ascii=False, sort_keys=True))
        lines.append("")
    return "\n".join(lines) + "\n"


def command(args):
    from .planning_api import render
    render({},max_output_bytes=args.max_output_bytes)

    project = resolve_project(args.root, args.data_root)
    project_id = args.project_id or (project or {}).get("project_id")
    if not project_id and args.scope != "global":
        raise ValueError("supply a registered project root or project-id")
    if project and project["project_id"] != project_id:
        raise ValueError("root and project-id disagree")
    if args.rules_action == "snapshot-export":
        value = snapshot_export(args.data_root, project_id, args.scope)
    elif args.rules_action == "snapshot-import":
        value = snapshot_import(
            args.data_root, project_id, args.file, args.scope, args.force, args.reason
        )
    elif args.rules_action in {"export", "check-mirror"}:
        value = bundle(
            args.data_root,
            project_id=project_id,
            audience=args.audience,
            lane_id=args.lane_id,
            scope=args.scope,
        )
        text = export_text(value)
        if args.rules_action == "check-mirror":
            expected = hashlib.sha256(text.encode()).hexdigest()
            mirror = Path(args.file).expanduser().resolve()
            try:
                with mirror.open("rb") as stream:
                    raw = stream.read(1048577)
            except FileNotFoundError:
                raw = None
            actual = hashlib.sha256(raw).hexdigest() if raw is not None and len(raw) <= 1048576 else None
            value = {"status": "current" if actual == expected else "drift", "rules_digest": value["rules_digest"],
                     "canonical_sha256": expected, "mirror_sha256": actual, "mirror": str(mirror),
                     "read_limit_bytes": 1048576, "action": "Regenerate from rules export --format text; do not maintain a second policy source"}
        elif args.format == "text":
            if len(text.encode()) > args.max_output_bytes:
                raise ValueError("complete rules text exceeds budget; increase --max-output-bytes; no policy was truncated")
            print(text, end="")
            return 0
    elif args.rules_action == "update":
        value = put_rule(
            args.data_root,
            project_id=project_id,
            rule_id=args.rule_id,
            text=args.text,
            audience=args.audience,
            attachment_file=args.attachment_file,
            update=True,
            scope=args.scope,
        )
    elif args.rules_action == "affected":
        value = affected(args.data_root, project_id=project_id)
    elif args.rules_action == "history":
        if _ID_RE.fullmatch(args.rule_id) is None:
            raise ValueError("rule id is invalid")
        folder = (
            _directory(args.data_root, project_id, args.scope)
            / "rule-history"
            / args.rule_id
        )
        value = {
            "items": [
                json.loads(p.read_text())
                for p in sorted(folder.glob("*.json"), key=lambda p: int(p.stem))
            ]
        }
    else:
        value = attachment(
            args.data_root,
            project_id=project_id,
            sha256=args.sha256,
            offset=args.offset,
            max_chars=args.max_chars,
            scope=args.scope,
        )
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if (
        args.rules_action in {"export", "history", "attachment"}
        and len(raw.encode()) + 1 > args.max_output_bytes
    ):
        raise ValueError(
            "complete rules/policy output exceeds budget; increase --max-output-bytes or request a smaller explicit attachment range; no policy was silently truncated"
        )
    print(render(value, format="json", max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    p = commands.add_parser(
        "rules",
        help="Export complete audience rules, read hashed attachments and identify affected workers",
    )
    sub = p.add_subparsers(dest="rules_action", required=True)
    for action in (
        "export",
        "check-mirror",
        "attachment",
        "update",
        "history",
        "affected",
        "snapshot-export",
        "snapshot-import",
        "import",
    ):
        leaf = sub.add_parser(action)
        leaf.set_defaults(handler=command)
        leaf.add_argument("--root", default=".")
        leaf.add_argument("--project-id")
        leaf.add_argument("--data-root")
        leaf.add_argument("--max-output-bytes", type=int, default=65536)
        leaf.add_argument("--scope", choices=("global", "project"), default="project")
        if action == "import":
            from .rule_intake import command as intake_command
            leaf.set_defaults(handler=intake_command)
            leaf.add_argument("--manifest-file", required=True)
            leaf.add_argument("--apply", action="store_true")
        if action in {"export", "check-mirror", "update"}:
            leaf.add_argument(
                "--audience", default="worker" if action in {"export", "check-mirror"} else "all"
            )
        if action == "export":
            leaf.add_argument("--lane-id")
            leaf.add_argument("--format", choices=("json", "text"), default="json", help="Complete JSON or generated worker policy text; never silently truncated")
        if action == "check-mirror":
            leaf.add_argument("--file", required=True)
            leaf.add_argument("--lane-id")
        if action in {"update", "history"}:
            leaf.add_argument("--rule-id", required=True)
        if action == "update":
            leaf.add_argument("--text", required=True)
            leaf.add_argument("--attachment-file")
        if action == "attachment":
            leaf.add_argument("--sha256", required=True)
            leaf.add_argument("--offset", type=int, default=0)
            leaf.add_argument("--max-chars", type=int, default=8192)
        if action == "snapshot-import":
            leaf.add_argument("--file", required=True)
            leaf.add_argument("--force", action="store_true")
            leaf.add_argument("--reason")
