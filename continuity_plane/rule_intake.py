"""Explicit hash-checked policy intake; source documents remain untouched."""

import hashlib
import json
from pathlib import Path

from .collaboration_registry import _atomic_write, _now
from .rules_delivery import _directory, _blob, put_rule
from .vocabulary_memory import _SECRET_RE


def intake(data_root, project_id, manifest_file, *, apply=False, scope="project"):
    with Path(manifest_file).open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError("rule intake manifest exceeds 256 KiB")
    manifest = json.loads(raw)
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or not 1 <= len(sources) <= 128:
        raise ValueError("rule intake requires 1–128 explicit sources")
    prepared = []
    ids = set()
    for entry in sources:
        path = Path(entry["path"]).expanduser().resolve()
        if path.suffix.casefold() not in {".md", ".txt"}:
            raise ValueError(
                "policy intake accepts Markdown/text documents, not raw transcripts"
            )
        with path.open("rb") as stream:
            content = stream.read(262145)
        if not content or len(content) > 262144:
            raise ValueError("rule source must contain 1–262144 bytes")
        source_digest = hashlib.sha256(content).hexdigest()
        if apply and entry.get("expected_sha256") != source_digest:
            raise ValueError(
                "rule source changed or lacks expected_sha256; preview and review before applying"
            )
        lines = content.decode("utf-8").splitlines(keepends=True)
        start, end = entry.get("line_start", 1), entry.get("line_end", len(lines))
        if (
            type(start) is not int
            or type(end) is not int
            or not 1 <= start <= end <= len(lines)
        ):
            raise ValueError("rule source line range is invalid")
        selected = "".join(lines[start - 1 : end])
        if _SECRET_RE.search(selected):
            raise ValueError(
                "policy source contains credential assignments; redact before intake"
            )
        rule_id = entry.get("rule_id")
        from .collaboration_registry import _ID_RE, _text

        if (
            not isinstance(rule_id, str)
            or not _ID_RE.fullmatch(rule_id)
            or rule_id in ids
        ):
            raise ValueError("intake rule ids must be valid and unique")
        ids.add(rule_id)
        summary = _text(entry.get("summary"), "rule summary", 2048)
        audience = _text(entry.get("audience", "all"), "audience", 128)
        prepared.append(
            {
                "rule_id": rule_id,
                "summary": summary,
                "audience": audience,
                "path": str(path),
                "source_sha256": source_digest,
                "line_start": start,
                "line_end": end,
                "content": selected,
            }
        )
    directory = _directory(data_root, project_id, scope)
    items = []
    for entry in prepared:
        content = entry.pop("content").encode()
        digest = hashlib.sha256(content).hexdigest()
        item = {**entry, "attachment_sha256": digest, "attachment_bytes": len(content)}
        if apply:
            _blob(directory, content)
            rule = put_rule(
                data_root,
                project_id=project_id,
                scope=scope,
                rule_id=entry["rule_id"],
                text=entry["summary"],
                audience=entry["audience"],
                update=True,
                attachment_file=directory / "rule-attachments" / (digest + ".txt"),
            )
            item["revision"] = rule["revision"]
        items.append(item)
    receipt = {
        "schema_version": "context.rule-intake/v1",
        "applied": apply,
        "items": items,
        "source_files_modified": False,
        "recorded_at": _now(),
    }
    if apply:
        digest = hashlib.sha256(raw).hexdigest()
        _atomic_write(directory / "intake-receipts" / (digest + ".json"), receipt)
    return receipt


def command(args):
    from .planning_api import render
    from .collaboration_registry import resolve_project

    render({}, max_output_bytes=args.max_output_bytes)
    project = resolve_project(args.root, args.data_root)
    project_id = args.project_id or (project or {}).get("project_id")
    if not project_id and args.scope != "global":
        raise ValueError("registered root or project-id required")
    result = intake(
        args.data_root,
        project_id,
        args.manifest_file,
        apply=args.apply,
        scope=args.scope,
    )
    print(render(result, format="json", max_output_bytes=args.max_output_bytes))
    return 0
