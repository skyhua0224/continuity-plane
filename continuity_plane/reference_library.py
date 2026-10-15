"""External, declared reference evidence. No network requests or execution gates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, urlsplit

from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    resolve_project,
)

SCHEMA = "context.reference-library/v1alpha1"
KINDS = (
    "standard",
    "official-os",
    "official-library",
    "official-product",
    "implementation",
    "supplementary",
)
STATUSES = ("candidate", "read", "unavailable", "superseded")


def _text(value, field, limit=1024, required=False):
    if (
        not isinstance(value, str)
        or (required and not value.strip())
        or len(value.encode()) > limit
    ):
        raise ValueError(
            f"{field} must be {'nonempty ' if required else ''}text of at most {limit} bytes"
        )
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{field} contains control characters")
    return value.strip()


def location(root, data_root=None, scope="project"):
    if scope not in {"project", "global"}:
        raise ValueError("scope must be project or global")
    data = Path(data_root or default_data_root()).expanduser().resolve()
    root = Path(root).resolve()
    project = resolve_project(root, data)
    canonical = Path(project["control_root"]).resolve() if project else root
    key = (
        "global"
        if scope == "global"
        else hashlib.sha256(str(canonical).encode()).hexdigest()
    )
    return data / "references" / (key + ".json")


def _load(path):
    if not path.exists():
        return {
            "schema_version": SCHEMA,
            "revision": 0,
            "preference": "inherit",
            "items": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA
        or not isinstance(value.get("items"), list)
    ):
        raise ValueError(
            "reference library schema is unsupported; upgrade or restore a backup"
        )
    return value


def _change(root, data_root, scope, change):
    path = location(root, data_root, scope)
    with _locked(path):
        value = _load(path)
        result = change(value)
        value["revision"] += 1
        _atomic_write(path, value)
    return result


def add(
    root,
    *,
    reference_id,
    title,
    source,
    kind,
    version="",
    section="",
    platforms=(),
    topics=(),
    applicability="",
    data_root=None,
    scope="project",
):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", reference_id):
        raise ValueError("reference_id must be a short filename-safe identifier")
    if kind not in KINDS:
        raise ValueError("invalid reference kind")
    source = _text(source, "source", 2048, True)
    parsed = urlsplit(source)
    if not (parsed.scheme in {"https", "http"} and parsed.hostname) and not (
        Path(source).is_absolute() or PureWindowsPath(source).is_absolute()
    ):
        raise ValueError(
            "source must be an HTTP(S) URL or an absolute local reference path"
        )
    if (
        parsed.username
        or parsed.password
        or any(
            key not in {"view", "version", "lang", "locale", "tab", "tabs"}
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise ValueError(
            "source must not contain credentials or unrecognized query parameters; store a public permalink (version/view/locale parameters are supported)"
        )
    for values in (platforms, topics):
        if len(values) > 16:
            raise ValueError("at most 16 platforms or topics")
        for item in values:
            _text(item, "platform/topic", 128, True)
    item = dict(
        reference_id=reference_id,
        title=_text(title, "title", 256, True),
        source=source,
        kind=kind,
        authority="declared-not-verified",
        version=_text(version, "version", 256),
        section=_text(section, "section", 256),
        platforms=list(platforms),
        topics=list(topics),
        applicability=_text(applicability, "applicability"),
        review_status="candidate",
        reviews=[],
        uses=[],
        created_at=datetime.now(UTC).isoformat(),
    )

    def change(value):
        if any(i["reference_id"] == reference_id for i in value["items"]):
            raise ValueError(
                "reference_id already exists; use reference record to record a review or adoption"
            )
        if len(value["items"]) >= 1000:
            raise ValueError(
                "reference library capacity reached (1000); narrow the project scope"
            )
        value["items"].append(item)
        return item

    return _change(root, data_root, scope, change)


def record(
    root,
    *,
    reference_id,
    actor,
    summary,
    status=None,
    version=None,
    evidence="",
    task_id=None,
    code_ref=None,
    superseded_by=None,
    data_root=None,
    scope="project",
):
    actor = _text(actor, "actor", 256, True)
    summary = _text(summary, "summary", 1536, True)
    evidence = _text(evidence, "evidence", 1024)
    if status is not None and status not in STATUSES:
        raise ValueError("invalid review status")
    if status == "read" and (not evidence or not version):
        raise ValueError(
            "read review requires --version and --evidence (section or checked revision); no automatic verification is implied"
        )
    if status == "superseded" and not superseded_by:
        raise ValueError("superseded review requires --superseded-by")
    for key, val in (
        ("version", version),
        ("task_id", task_id),
        ("code_ref", code_ref),
        ("superseded_by", superseded_by),
    ):
        if val is not None:
            _text(val, key, 256, True)
    if task_id:
        from .collaboration_registry import list_tasks

        project = resolve_project(root, data_root)
        if not project or not any(
            t["task_id"] == task_id
            for t in list_tasks(data_root, project_id=project["project_id"])
        ):
            raise ValueError("task_id does not exist in this project")

    def change(value):
        item = next(
            (i for i in value["items"] if i["reference_id"] == reference_id), None
        )
        if item is None:
            raise ValueError("reference_id not found in this scope")
        entry = dict(
            actor=actor,
            at=datetime.now(UTC).isoformat(),
            summary=summary,
            evidence=evidence,
            version=version,
            task_id=task_id,
            code_ref=code_ref,
        )
        if status:
            entry.update(status=status, superseded_by=superseded_by)
            item["review_status"] = status
            item["reviews"] = (item["reviews"] + [entry])[-32:]
            if version:
                item["version"] = version
        else:
            item["uses"] = (item["uses"] + [entry])[-32:]
        return item

    return _change(root, data_root, scope, change)


def preference(root, *, mode=None, data_root=None, scope="project"):
    if mode is not None:
        if mode not in {"inherit", "primary-first", "off"}:
            raise ValueError("mode must be inherit, primary-first or off")

        def change(value):
            value["preference"] = mode
            return {"scope": scope, "preference": mode}

        return _change(root, data_root, scope, change)
    selected = _load(location(root, data_root, scope))["preference"]
    if scope == "project" and selected == "inherit":
        selected = _load(location(root, data_root, "global"))["preference"]
    return {"scope": scope, "preference": selected}


def find(
    root,
    *,
    query="",
    platform=None,
    kind=None,
    task_id=None,
    reference_id=None,
    max_results=10,
    data_root=None,
    scope="project",
):
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be between 1 and 100")
    _text(query, "query", 1024)
    # Project views include reusable global references; scope/id disambiguates duplicates.
    scopes = ("project", "global") if scope == "project" else ("global",)
    rows = []
    for source_scope in scopes:
        value = _load(location(root, data_root, source_scope))
        for item in value["items"]:
            if reference_id and item["reference_id"] != reference_id:
                continue
            if kind and item["kind"] != kind:
                continue
            if platform and platform.casefold() not in [
                p.casefold() for p in item["platforms"]
            ]:
                continue
            if task_id and not any(
                u.get("task_id") == task_id for u in item["uses"] + item["reviews"]
            ):
                continue
            haystack = json.dumps(
                {
                    k: item[k]
                    for k in (
                        "reference_id",
                        "title",
                        "source",
                        "topics",
                        "applicability",
                        "section",
                    )
                },
                ensure_ascii=False,
            ).casefold()
            if not all(word in haystack for word in query.casefold().split()):
                continue
            latest = item["reviews"][-1] if item["reviews"] else None
            due = not latest or datetime.fromisoformat(latest["at"]) < datetime.now(
                UTC
            ) - timedelta(days=30)
            rows.append(
                {
                    **item,
                    "scope": source_scope,
                    "review_due": bool(due),
                    "freshness": "not-automatically-verified",
                }
            )
    rows.sort(
        key=lambda i: (
            i["review_status"] in {"superseded", "unavailable"},
            i["scope"] == "global",
            i["reference_id"],
        )
    )
    return {
        "items": rows[:max_results],
        "total_items": len(rows),
        "truncated": len(rows) > max_results,
    }


def hint(root, *, data_root=None):
    if preference(root, data_root=data_root)["preference"] != "primary-first":
        return None
    return (
        "Reference preference: consult applicable standards and official OS/library docs for consequential design; "
        "reuse `continuity reference list` / reference.list, record version and adoption reason. "
        "Newest is not automatically best; missing sources flag uncertainty, not a global work stop."
    )


def command(args):
    from .planning_api import render

    # Validate the requested output contract before writing metadata.
    render({}, format=args.format, max_output_bytes=args.max_output_bytes)
    shared = dict(root=args.root, data_root=args.data_root, scope=args.scope)
    if args.project_id:
        from .collaboration_registry import list_projects

        project = next(
            (
                p
                for p in list_projects(args.data_root)
                if p["project_id"] == args.project_id
            ),
            None,
        )
        if project is None:
            raise ValueError("project_id is not registered")
        actual = resolve_project(args.root, args.data_root)
        if args.root != "." and (not actual or actual["project_id"] != args.project_id):
            raise ValueError("root and project_id refer to different projects")
        shared["root"] = project["control_root"]
    if args.reference_command == "add":
        result = add(
            **shared,
            **{
                k: getattr(args, k)
                for k in (
                    "reference_id",
                    "title",
                    "source",
                    "kind",
                    "version",
                    "section",
                    "applicability",
                )
            },
            platforms=args.platform,
            topics=args.topic,
        )
    elif args.reference_command == "record":
        result = record(
            **shared,
            **{
                k: getattr(args, k)
                for k in (
                    "reference_id",
                    "actor",
                    "summary",
                    "status",
                    "version",
                    "evidence",
                    "task_id",
                    "code_ref",
                    "superseded_by",
                )
            },
        )
    elif args.reference_command == "preference":
        result = preference(**shared, mode=args.mode)
    else:
        result = find(
            **shared,
            **{
                k: getattr(args, k)
                for k in (
                    "query",
                    "platform",
                    "kind",
                    "task_id",
                    "reference_id",
                    "max_results",
                )
            },
        )
        if args.reference_command == "show" and not result["items"]:
            raise ValueError("reference_id not found")
    print(render(result, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    parser = commands.add_parser(
        "reference",
        help="Reuse external standards, official docs and reference repositories; no work gates",
    )
    sub = parser.add_subparsers(dest="reference_command", required=True)
    for name in ("add", "list", "show", "record", "preference"):
        p = sub.add_parser(name)
        p.set_defaults(handler=command)
        p.add_argument("--root", default=".")
        p.add_argument("--project-id")
        p.add_argument("--data-root")
        p.add_argument("--scope", choices=("project", "global"), default="project")
        p.add_argument("--format", choices=("brief", "json"), default="brief")
        p.add_argument("--max-output-bytes", type=int, default=4096)
        if name in {"add", "show", "record"}:
            p.add_argument("--reference-id", required=True)
        if name == "add":
            for field in ("title", "source"):
                p.add_argument("--" + field, required=True)
            p.add_argument(
                "--kind",
                required=True,
                choices=KINDS,
                help="Declared source category; registration does not verify authority",
            )
            for field in ("version", "section", "applicability"):
                p.add_argument("--" + field, default="")
            for field in ("platform", "topic"):
                p.add_argument("--" + field, action="append", default=[])
        elif name == "record":
            p.add_argument("--actor", required=True)
            p.add_argument(
                "--summary",
                required=True,
                help="Adoption/rejection rationale or concise review finding",
            )
            p.add_argument(
                "--status",
                choices=STATUSES,
                help="Omit to record a use; read requires version and evidence",
            )
            p.add_argument("--evidence", default="")
            for field in ("version", "task-id", "code-ref", "superseded-by"):
                p.add_argument("--" + field)
        elif name == "preference":
            p.add_argument(
                "--mode",
                choices=("inherit", "primary-first", "off"),
                help="Omit to inspect; global preference can be overridden per project",
            )
        else:
            p.add_argument("--query", default="")
            p.add_argument("--platform")
            p.add_argument("--kind", choices=KINDS)
            p.add_argument("--task-id")
            p.add_argument("--max-results", type=int, default=10)
            if name == "list":
                p.add_argument("--reference-id")
