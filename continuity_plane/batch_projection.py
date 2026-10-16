"""Read-only collaboration status projections, separate from authoritative State."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from .collaboration_registry import _load_ledger, default_data_root

MARKER = "<!-- continuity:collaboration-status/v1 -->"


def _cell(text):
    return " ".join(str(text or "—").split()).replace("|", "\\|").replace("<", "&lt;")


def project(data_root, project_id, batch_id=None):
    ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
    records = ledger.get("workflow_records", {})
    batches = list(records.get("batch", {}).values())
    if batch_id:
        batches = [b for b in batches if b["id"] == batch_id]
        if not batches:
            raise ValueError("batch does not exist")
    selected = {t for b in batches for t in b["value"]["task_ids"]}
    cards = [t for t in ledger["tasks"] if not batch_id or t["task_id"] in selected]
    counts = dict(sorted(Counter(t["status"] for t in cards).items()))
    source = {
        "cards": cards,
        "batches": batches,
        "decisions": records.get("decision", {}),
    }
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    lines = [
        MARKER,
        "# Collaboration status",
        "",
        "Generated metadata projection; not authoritative State or approval.",
        "",
        f"Project: {_cell(project_id)}",
        f"Source SHA-256: {digest}",
        "",
        ", ".join(f"{k}: {v}" for k, v in counts.items()) or "No cards.",
        "",
    ]
    for batch in sorted(batches, key=lambda b: (b.get("recorded_at", ""), b["id"])):
        value = batch["value"]
        lines.extend(
            [
                f"## Batch {_cell(batch['id'])}",
                "",
                f"Baseline: `{value['baseline']}` → result: `{value['result_commit']}`",
                "",
                "| Check | Result | Required |",
                "|---|---|---|",
            ]
        )
        for check in value["verification"]["checks"]:
            lines.append(
                f"| {_cell(check['name'])} | {_cell(check['status'])}: {_cell(check['result'])} | {check['required']} |"
            )
        for item in value["verification"].get("unverified", []):
            lines.append(f"Unverified: {_cell(item)}")
        if value.get("metrics"):
            lines.extend(["", "| Metric | Declared value |", "|---|---|"])
            for name, metric in sorted(value["metrics"].items()):
                lines.append(f"| {_cell(name)} | {_cell(json.dumps(metric, ensure_ascii=False, sort_keys=True))} |")
        for decision_id in value.get("exception_decisions", []):
            decision = records.get("decision", {}).get(decision_id, {}).get("value", {})
            lines.append(
                f"Exception {_cell(decision_id)}: {_cell(decision.get('text'))}; source: {_cell(decision.get('source'))}; actor: {_cell(decision.get('actor'))}"
            )
        lines.append("")
    lines.extend(["## Cards", "", "| Task | Status | Next action |", "|---|---|---|"])
    for card in sorted(cards, key=lambda t: t["task_id"]):
        lines.append(
            f"| {_cell(card['task_id'])}: {_cell(card['title'])} | {card['status']} | {_cell(card.get('next_action'))} |"
        )
    return {
        "schema_version": "context.collaboration-status/v1",
        "project_id": project_id,
        "source_sha256": digest,
        "task_count": len(cards),
        "batch_count": len(batches),
        "markdown": "\n".join(lines) + "\n",
    }


def command(args):
    from .planning_api import render

    # Validate output budget before any optional file write.
    render({}, max_output_bytes=args.max_output_bytes)
    value = project(args.data_root, args.project_id, args.batch)
    if (
        args.format == "markdown"
        and len(value["markdown"].encode()) > args.max_output_bytes
    ):
        raise ValueError(
            "status exceeds output budget; select --batch or increase budget; no content was truncated"
        )
    if args.output:
        path = Path(args.output).expanduser().resolve()
        root = Path(args.root).resolve()
        if not path.is_relative_to(root):
            raise ValueError("status output must be inside the selected project root")
        if path.exists() and not path.read_text().startswith(MARKER):
            if not args.force or not args.reason:
                raise ValueError(
                    "existing status is not owned by this projection; use --force --reason after review"
                )
            old = path.read_bytes()
            backup = path.with_name(
                path.name + ".before-continuity-" + hashlib.sha256(old).hexdigest()[:16]
            )
            if not backup.exists():
                backup.write_bytes(old)
        from .worker_dispatch import _write

        _write(path, value["markdown"])
        value["output"] = str(path)
    if args.format == "markdown":
        if len(value["markdown"].encode()) > args.max_output_bytes:
            raise ValueError(
                "status exceeds output budget; select --batch or increase budget; no content was truncated"
            )
        print(value["markdown"], end="")
    else:
        print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    parser = commands.add_parser(
        "status-render",
        help="Render cards and integration batches without changing State",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--project-id")
    parser.add_argument("--data-root")
    parser.add_argument("--batch")
    parser.add_argument(
        "--output",
        help="Explicit project-local Markdown file; existing unmanaged files require backup and override",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument(
        "--format", choices=("brief", "json", "markdown"), default="markdown"
    )
    parser.add_argument("--max-output-bytes", type=int, default=32768)
    parser.set_defaults(handler=command)
