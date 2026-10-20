"""One exact CLI contract, reused by planning MCP; explicit native intake only."""

import argparse
from pathlib import Path

from . import controller_memory as memory
from .project_context import resolve_context_args


def register_cli(commands):
    existing = commands.choices["memory"]
    memories = next(
        a for a in existing._actions if isinstance(a, argparse._SubParsersAction)
    )
    for action in (
        "note",
        "notes",
        "show",
        "adopt",
        "revoke",
        "grant",
        "consent-revoke",
        "ingest",
        "share",
        "history",
    ):
        leaf = memories.add_parser(
            action,
            help="Scoped working memory; legacy add/list/resolve remain vocabulary operations",
        )
        _common(leaf, "memory-" + action)
        if action in {"note", "show", "adopt", "revoke", "share", "history"}:
            leaf.add_argument("--record-id", required=True)
        if action == "note":
            leaf.add_argument("--kind", choices=sorted(memory.KINDS), required=True)
            leaf.add_argument("--text", required=True)
            leaf.add_argument("--source", required=True)
            leaf.add_argument(
                "--scope", choices=sorted(memory.SCOPES), default="private-project"
            )
            leaf.add_argument("--expires-at")
            leaf.add_argument("--expected-revision", type=int)
        elif action in {"adopt", "revoke", "share"}:
            leaf.add_argument("--expected-revision", type=int, required=True)
            leaf.add_argument("--reason", required=True)
        elif action in {"grant", "consent-revoke", "ingest"}:
            leaf.add_argument("--consent-id", required=True)
            if action == "grant":
                leaf.add_argument(
                    "--provider", choices=("codex", "claude"), required=True
                )
                leaf.add_argument("--source-file", action="append", required=True)
        elif action == "notes":
            leaf.add_argument("--query")
            leaf.add_argument("--include-candidates", action="store_true")
    group = commands.add_parser(
        "profile",
        help="Controller communication/report/tool preferences, never permissions",
    )
    leaves = group.add_subparsers(required=True)
    for action in ("show", "set"):
        leaf = leaves.add_parser(action)
        _common(leaf, "profile-" + action)
        if action == "set":
            leaf.add_argument("--patch-file", required=True)
            leaf.add_argument("--expected-revision", type=int, required=True)
            leaf.add_argument("--global", dest="global_profile", action="store_true")
        else:
            leaf.add_argument("--public-only", action="store_true")
    control = next(
        a
        for a in commands.choices["controller"]._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    leaf = control.add_parser(
        "start",
        help="Bounded fast handoff/profile/memory packet; no scene scan or process launch",
    )
    _common(leaf, "start")
    leaf.add_argument("--query")
    leaf.add_argument(
        "--capability",
        action="append",
        help="Caller-declared available intent, e.g. question; does not grant tools",
    )
    leaf.add_argument("--ack-revision", type=int)
    leaf.add_argument("--profile-sha256")
    leaf.add_argument(
        "--public-only",
        action="store_true",
        help="Never read member-private profiles/notes; suitable for shared server context",
    )
    leaf.add_argument(
        "--no-refresh-native",
        action="store_true",
        help="Skip incremental refresh of already consented origin-local notes",
    )


def _common(leaf, action):
    leaf.add_argument("--root")
    leaf.add_argument("--project-id")
    leaf.add_argument("--data-root")
    leaf.add_argument(
        "--member",
        help="Local planning identity, not authentication; same-OS-user agents need host sandbox isolation",
    )
    leaf.add_argument("--format", choices=("brief", "json"), default="brief")
    leaf.add_argument("--max-output-bytes", type=int, default=8192)
    leaf.set_defaults(handler=command, memory_action=action)


def command(args):
    from .planning_api import render

    render({}, max_output_bytes=args.max_output_bytes)
    resolve_context_args(args)
    identity = memory.member(args.member)
    common = (args.data_root, args.project_id, identity)
    action = args.memory_action
    if action == "memory-note":
        value = memory.remember(
            args.data_root,
            args.project_id,
            identity=identity,
            record_id=args.record_id,
            kind=args.kind,
            text=args.text,
            source=args.source,
            scope=args.scope,
            expected_revision=args.expected_revision,
            expires_at=args.expires_at,
        )
    elif action == "memory-notes":
        rows = memory.records(
            *common, query=args.query, include_candidates=args.include_candidates
        )
        value = {
            "status": "local-private",
            "items": rows,
            "total_items": len(rows),
            "authority": "approved reference data; candidates excluded unless requested",
        }
    elif action == "memory-show":
        value = next(
            (
                r
                for r in memory.records(*common, include_candidates=True)
                if r["record_id"] == args.record_id
            ),
            None,
        )
        if value is None:
            raise ValueError("active memory record not found in this member/project")
    elif action in {"memory-adopt", "memory-revoke"}:
        value = memory.decide(
            args.data_root,
            identity,
            args.record_id,
            status="approved" if action.endswith("adopt") else "revoked",
            expected_revision=args.expected_revision,
            reason=args.reason,
        )
    elif action == "memory-share":
        value = memory.share(
            args.root,
            *common,
            args.record_id,
            expected_revision=args.expected_revision,
            reason=args.reason,
        )
    elif action in {"memory-grant", "memory-consent-revoke"}:
        value = memory.consent(
            args.data_root,
            identity,
            args.project_id,
            consent_id=args.consent_id,
            provider=getattr(args, "provider", None),
            files=getattr(args, "source_file", None),
            revoke=action.endswith("revoke"),
        )
    elif action == "memory-ingest":
        value = memory.ingest(*common, args.consent_id)
    elif action == "memory-history":
        if not memory._ID.fullmatch(args.record_id):
            raise ValueError("invalid record identity")
        paths = sorted(
            (
                memory.directory(args.data_root, identity) / "history" / args.record_id
            ).glob("*.json")
        )
        value = {
            "items": [memory._read(p) for p in paths[-100:]],
            "total_items": len(paths),
        }
    elif action == "profile-set":
        patch = memory._read(Path(args.patch_file), limit=32768)
        if not isinstance(patch, dict):
            raise ValueError("profile patch must be an existing JSON object file")
        value = memory.profile(
            args.data_root,
            None if args.global_profile else args.project_id,
            identity,
            patch=patch,
            expected_revision=args.expected_revision,
        )
    elif action == "profile-show":
        value = memory.effective(
            args.root, *common, include_private=not args.public_only
        )
    else:
        from .controller_session import start

        value = start(
            args.root,
            *common,
            query=args.query,
            budget=args.max_output_bytes,
            acknowledge=args.ack_revision,
            profile_sha256=args.profile_sha256,
            capabilities=args.capability,
            public_only=args.public_only,
            refresh_native=not args.no_refresh_native,
        )
    print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0
