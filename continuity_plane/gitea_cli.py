"""CLI/MCP entry routing for opt-in, Issue-authoritative collaboration."""

from __future__ import annotations

import json
from pathlib import Path

from .collaboration_registry import (
    default_data_root,
    list_projects,
    project_data_dir,
    resolve_project,
)
from .gitea_tasks import CLOSED, GiteaTasks
from .gitea_transport import GiteaClient, GiteaError, project_config, validate_config


def _registration(data_root=None, project_id=None, root=None):
    return (
        next(
            (p for p in list_projects(data_root) if p["project_id"] == project_id), None
        )
        if project_id
        else resolve_project(root or ".", data_root)
    )


def is_shared_project(data_root=None, project_id=None, root=None):
    project = _registration(data_root, project_id, root)
    return bool(project_config(project["control_root"] if project else root or "."))


def backend_for(data_root=None, project_id=None, root=None):
    project = (
        next(
            (p for p in list_projects(data_root) if p["project_id"] == project_id), None
        )
        if project_id
        else resolve_project(root or ".", data_root)
    )
    if project is None:
        if root and project_config(root):
            raise GiteaError(
                "register this local checkout before using the declared shared backend"
            )
        return None
    config = project_config(project["control_root"])
    if not config:
        return None
    directory = project_data_dir(
        data_root or default_data_root(), project["project_id"]
    )
    return GiteaTasks(config, directory)


def add_parsers(collaboration_commands, task_commands):
    batch = task_commands.add_parser(
        "batch-update",
        help="CLI-only bounded JSON batch; sequential, revisioned, stops on first failure",
    )
    batch.add_argument("--root", default=".")
    batch.add_argument("--project-id")
    batch.add_argument("--data-root")
    batch.add_argument("--file", required=True)
    batch.add_argument("--format", choices=("brief", "json"), default="brief")
    batch.add_argument("--max-output-bytes", type=int, default=8192)
    batch.set_defaults(handler=batch_update)
    backend = collaboration_commands.add_parser(
        "backend",
        help="Configure/check native Issue collaboration; credentials stay local",
    )
    commands = backend.add_subparsers(dest="backend_action", required=True)
    for name in ("configure", "doctor", "sync", "pending", "replay", "cached"):
        parser = commands.add_parser(name)
        parser.add_argument("--root", default=".")
        parser.add_argument("--project-id")
        parser.add_argument("--data-root")
        parser.add_argument("--format", choices=("brief", "json"), default="brief")
        parser.add_argument("--max-output-bytes", type=int, default=8192)
        parser.set_defaults(handler=backend_command)
        if name == "configure":
            parser.add_argument("--instance", required=True)
            parser.add_argument("--repository", required=True)
            parser.add_argument("--credential-ref", required=True)
        elif name == "sync":
            parser.add_argument("--consumer", default="default")
            parser.add_argument("--ack-revision")
            parser.add_argument("--cursor")
            parser.add_argument("--max-results", type=int, default=50)
        elif name == "replay":
            parser.add_argument("--operation-id", required=True)
    for name in (
        "release",
        "block",
        "handoff-request",
        "handoff-accept",
        "ready-for-review",
        "request-review",
        "resolve",
    ):
        parser = task_commands.add_parser(
            name,
            help="Shared Gitea task operation; remote revision and own authentication required",
        )
        parser.add_argument("--project-id")
        parser.add_argument("--root", default=".")
        parser.add_argument("--data-root")
        parser.add_argument("--task-id", required=True)
        parser.add_argument("--operation-id", required=True)
        parser.add_argument("--expected-remote-revision", required=True)
        parser.add_argument("--format", choices=("brief", "json"), default="brief")
        parser.add_argument("--max-output-bytes", type=int, default=8192)
        if name == "handoff-request":
            parser.add_argument("--to", required=True)
            parser.add_argument("--note", required=True)
        elif name == "block":
            parser.add_argument("--blocked-reason", required=True)
        elif name == "request-review":
            parser.add_argument("--reviewer", action="append", required=True)
        elif name == "ready-for-review":
            parser.add_argument("--pr", type=int, required=True)
            parser.add_argument("--verification", required=True)
        elif name == "resolve":
            parser.add_argument(
                "--resolve-comment", type=int, action="append", required=True
            )
            parser.add_argument(
                "--status",
                choices=("queued", "active", "blocked", "ready-for-review"),
                required=True,
            )
        parser.set_defaults(handler=shared_task_command, shared_task_action=name)
    for name in ("add", "update", "claim"):
        parser = task_commands.choices[name]
        parser.add_argument(
            "--operation-id",
            help="Stable retry key required with a shared Issue backend",
        )
        parser.add_argument(
            "--expected-remote-revision",
            help="Revision from task show; required for shared changes",
        )
    # Local tasks retain their required assignee; shared claims may omit it.
    for action in task_commands.choices["claim"]._actions:
        if action.dest == "assignee":
            action.required = False
    for name in ("add", "update"):
        parser = task_commands.choices[name]
        parser.add_argument(
            "--dependency",
            action="append",
            help="Stable shared task ID; hint, not module ownership",
        )
        parser.add_argument("--pr", type=int, help="PR number in configured repository")
    parser = task_commands.choices["list"]
    for action in parser._actions:
        if action.dest == "status":
            action.choices = sorted(set(action.choices) | {"conflict"})
    for flag in ("mine", "created-by-me", "review-requested", "dependencies-ready"):
        parser.add_argument("--" + flag, action="store_true")
    update = task_commands.choices["update"]
    update.add_argument("--title", help="Shared task title, recorded as an Issue event")
    update.add_argument("--objective", help="Shared task objective")
    update.add_argument("--priority", choices=("p0", "p1", "p2"))
    update.add_argument(
        "--exit-criterion",
        action="append",
        help="Replace shared task acceptance criteria",
    )


def _print(args, value):
    from .planning_api import render

    print(
        render(
            value,
            format=getattr(args, "format", "json"),
            max_output_bytes=getattr(args, "max_output_bytes", 8192),
        )
    )


def backend_command(args):
    from .persistent_storage import sync_directory

    if args.backend_action == "configure":
        config = validate_config(
            {
                "backend": "gitea-issues",
                "instance": args.instance,
                "repository": args.repository,
                "credential_ref": args.credential_ref,
            }
        )
        report = GiteaClient(config).doctor()  # Validate connection before enabling.
        project = (
            next(
                (
                    p
                    for p in list_projects(args.data_root)
                    if p["project_id"] == args.project_id
                ),
                None,
            )
            if args.project_id
            else resolve_project(args.root, args.data_root)
        )
        if not project:
            raise GiteaError("register local checkout before configure")
        path = Path(project["control_root"]) / ".continuity" / "collaboration.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        # No overwrite of an existing declaration or credential-bearing config.
        import os

        with path.open("x", encoding="utf-8") as handle:
            handle.write(
                "[collaboration]\n"
                + "".join(
                    f"{key} = {json.dumps(value)}\n" for key, value in config.items()
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(path.parent)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            with ignore.open("x", encoding="utf-8") as handle:
                handle.write(
                    "# Shared declarations and reviewed knowledge only; runtime data stays member-local.\n"
                    "*\n!.gitignore\n!collaboration.toml\n!controller-profile.json\n!knowledge/\n!knowledge/*.json\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(path.parent)
        _print(args, {**report, "declaration": str(path), "authority": "gitea-issue"})
        return 0
    backend = backend_for(args.data_root, args.project_id, args.root)
    if backend is None:
        raise GiteaError(
            "no shared collaboration declaration; use collaboration backend configure"
        )
    if args.backend_action == "doctor":
        value = backend.client.doctor()
    elif args.backend_action == "sync":
        value = backend.sync(
            args.consumer,
            acknowledge=args.ack_revision,
            cursor=args.cursor,
            max_results=args.max_results,
            budget=args.max_output_bytes,
        )
    elif args.backend_action == "cached":
        value = {
            "status": "offline-cache",
            "authority": "cache-only; not current shared state",
            "tasks": [
                json.loads(p.read_text())["task"]
                for p in sorted((backend.directory / "cache").glob("*.json"))
            ],
        }
    elif args.backend_action == "pending":
        value = {
            "status": "local-only",
            "items": [
                {"operation_id": p.stem, "state": json.loads(p.read_text())["state"]}
                for p in sorted((backend.directory / "operations").glob("*.json"))
            ],
        }
    else:
        path = backend._journal(args.operation_id)
        if not path.exists():
            raise GiteaError("operation not found")
        intent = json.loads(path.read_text())["intent"]
        if intent["kind"] == "create":
            value = backend.create(
                intent["task_id"], intent["title"], intent["fields"], args.operation_id
            )
        elif intent["kind"] == "review-request":
            value = backend.request_review(
                intent["task_id"],
                intent["reviewers"],
                intent["revision"],
                args.operation_id,
            )
        else:
            value = backend.mutate(
                intent["task_id"],
                intent["kind"],
                intent["fields"],
                intent["revision"],
                args.operation_id,
                target=intent["target"],
                note=intent["note"],
                resolves=intent["resolves"],
            )
    _print(args, value)
    return 0


def batch_update(args):
    path = Path(args.file)
    if path.stat().st_size > 131072:
        raise GiteaError("batch file exceeds 128 KiB")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GiteaError("batch file must be valid UTF-8 JSON") from None
    updates = document.get("updates") if isinstance(document, dict) else None
    if not isinstance(updates, list) or not 1 <= len(updates) <= 50:
        raise GiteaError(
            "batch needs 1..50 updates; large work belongs in separate reviewed batches"
        )
    allowed = {
        "task_id",
        "operation_id",
        "expected_remote_revision",
        "status",
        "next_action",
        "branch",
        "commit",
        "verification",
        "pr",
        "body",
    }
    backend = backend_for(args.data_root, args.project_id, args.root)
    result = {
        "status": "completed",
        "total": len(updates),
        "succeeded": [],
        "failed": None,
        "transactional": False,
        "message": "Sequential revisioned updates; no all-or-none claim.",
    }
    for index, row in enumerate(updates):
        if not isinstance(row, dict) or set(row) - allowed:
            result.update(
                status="stopped",
                failed={"index": index, "reason": "unknown or invalid field"},
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 1
        fields = {
            k: row[k]
            for k in (
                "status",
                "next_action",
                "branch",
                "commit",
                "verification",
                "pr",
                "body",
            )
            if k in row
        }
        try:
            if backend:
                for key in ("task_id", "operation_id", "expected_remote_revision"):
                    if not row.get(key):
                        raise GiteaError("shared batch requires " + key + " per item")
                value = backend.mutate(
                    row["task_id"],
                    "update",
                    fields,
                    row["expected_remote_revision"],
                    row["operation_id"],
                )
            else:
                from .collaboration_registry import update_task

                value = update_task(
                    args.data_root,
                    project_id=args.project_id,
                    task_id=row.get("task_id"),
                    body=row.get("body"),
                    status=row.get("status"),
                    next_action=row.get("next_action"),
                    branch=row.get("branch"),
                    commit=row.get("commit"),
                    verification=row.get("verification"),
                )
            result["succeeded"].append(
                {
                    "index": index,
                    "task_id": row.get("task_id"),
                    "status": value.get("status"),
                }
            )
        except (GiteaError, ValueError) as exc:
            result.update(
                status="stopped",
                failed={
                    "index": index,
                    "task_id": row.get("task_id"),
                    "reason": str(exc)[:500],
                },
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def shared_task_command(args):
    backend = backend_for(args.data_root, args.project_id, getattr(args, "root", None))
    if backend is None:
        raise GiteaError("this operation requires gitea-issues backend")
    return route_task(args, backend)


def route_task(args, backend):
    from .delivery_records import task_body

    action = getattr(
        args, "shared_task_action", None
    ) or args.handler.__name__.removeprefix("_collaboration_task_")
    if action in {"show", "next", "list"}:
        if action == "show":
            value = backend.show(args.task_id)
        else:
            tasks, malformed = backend.list()
            account = backend.client.identity()
            all_tasks = {t["task_id"]: t for t in tasks}
            for attr, key in (
                ("status", "status"),
                ("assignee", "assignee"),
                ("lane_id", "lane_id"),
            ):
                if getattr(args, attr, None):
                    tasks = [t for t in tasks if t.get(key) == getattr(args, attr)]
            if getattr(args, "mine", False):
                tasks = [t for t in tasks if t.get("assignee") == account]
            if getattr(args, "created_by_me", False):
                tasks = [t for t in tasks if t.get("created_by") == account]
            if getattr(args, "review_requested", False):
                tasks = [
                    t
                    for t in tasks
                    if account
                    in t.get("pull_request", {}).get("requested_reviewers", [])
                ]
            if getattr(args, "dependencies_ready", False):
                tasks = [
                    t
                    for t in tasks
                    if all(
                        all_tasks.get(d, {}).get("status") in {"completed", "merged"}
                        for d in t["dependencies"]
                    )
                ]
            if getattr(args, "query", None):
                tasks = [
                    t
                    for t in tasks
                    if args.query.casefold()
                    in json.dumps(t, ensure_ascii=False).casefold()
                ]
            if getattr(args, "updated_after", None):
                tasks = [t for t in tasks if t["updated_at"] >= args.updated_after]
            if not getattr(args, "all", False) and not getattr(args, "status", None):
                tasks = [t for t in tasks if t["status"] not in CLOSED]
            tasks.sort(key=lambda t: (t["updated_at"], t["task_id"]), reverse=True)
            if action == "next":
                value = tasks[0] if tasks else {"status": "empty"}
            else:
                from .metadata_pages import tasks_page

                query = {
                    "backend": backend.scope,
                    **{
                        k: getattr(args, k, None)
                        for k in (
                            "status",
                            "assignee",
                            "lane_id",
                            "query",
                            "updated_after",
                            "all",
                            "mine",
                            "created_by_me",
                            "review_requested",
                            "dependencies_ready",
                            "summary",
                        )
                    },
                }
                if args.summary:
                    tasks = [
                        {
                            k: t.get(k)
                            for k in (
                                "task_id",
                                "title",
                                "status",
                                "assignee",
                                "issue_url",
                                "remote_revision",
                                "next_action",
                                "conflicts",
                            )
                        }
                        for t in tasks
                    ]
                value = tasks_page(
                    args.data_root,
                    args.project_id,
                    tasks,
                    query=query,
                    budget=args.max_output_bytes,
                    cursor=args.cursor,
                    page_size=args.max_results,
                    metadata={
                        "authority": "gitea-issue",
                        "read_model": "cached-page-snapshot",
                        "malformed_count": len(malformed),
                    },
                )
        _print(args, value)
        return 0
    unsupported = [
        field
        for field in (
            "report_file",
            "verification_json",
            "landed_commit",
            "released_in",
            "worktree",
            "preauthorized_effect",
            "queued_effect",
        )
        if getattr(args, field, None)
    ]
    if unsupported:
        raise GiteaError(
            "shared backend does not publish local-only parameters: "
            + ", ".join(unsupported)
            + "; use verification/body for bounded shared evidence"
        )
    revision = getattr(args, "expected_remote_revision", None)
    op = getattr(args, "operation_id", None)
    if action != "add" and not revision:
        raise GiteaError(
            "shared mutation requires --expected-remote-revision from task show; no local force bypass"
        )
    if getattr(args, "force", False):
        raise GiteaError(
            "--force cannot bypass remote coordination; sync and use explicit resolve"
        )
    if action in {"add", "update"}:
        if getattr(args, "assignee", None):
            raise GiteaError(
                "shared owner changes require claim or accepted handoff; AI assignee is not human owner"
            )
        fields = {
            key: getattr(args, key)
            for key in (
                "objective",
                "next_action",
                "priority",
                "branch",
                "commit",
                "verification",
                "blocked_reason",
                "result",
                "pr",
                "lane_id",
                "mode",
                "status",
            )
            if getattr(args, key, None) is not None
        }
        for attr, key in (
            ("allowed_file", "allowed_files"),
            ("dependency", "dependencies"),
            ("exit_criterion", "exit_criteria"),
        ):
            if getattr(args, attr, None) is not None:
                fields[key] = getattr(args, attr)
        body = task_body(args)
        if action == "update" and getattr(args, "title", None) is not None:
            fields["title"] = args.title
        if body is not None:
            fields["body"] = body
        value = (
            backend.create(args.task_id, args.title, fields, op)
            if action == "add"
            else backend.mutate(args.task_id, "update", fields, revision, op)
        )
    elif action == "request-review":
        value = backend.request_review(args.task_id, args.reviewer, revision, op)
    else:
        if (
            action == "claim"
            and args.assignee
            and args.assignee != backend.client.identity()
        ):
            raise GiteaError(
                "claim uses authenticated human account; cannot impersonate --assignee"
            )
        fields = (
            {"status": "blocked", "blocked_reason": args.blocked_reason}
            if action == "block"
            else {}
        )
        if action == "ready-for-review":
            fields = {
                "status": "ready-for-review",
                "pr": args.pr,
                "verification": args.verification,
            }
        if action == "resolve":
            fields = {"status": args.status}
        value = backend.mutate(
            args.task_id,
            action,
            fields,
            revision,
            op,
            target=getattr(args, "to", None),
            note=getattr(args, "note", None),
            resolves=getattr(args, "resolve_comment", None),
        )
    _print(args, value)
    return 0


def maybe_route(args):
    """Called before local task handlers by both CLI and planning MCP."""
    handler = getattr(args, "handler", None)
    if handler in {backend_command, shared_task_command}:
        return None
    name = getattr(handler, "__name__", "")
    delivery_shortcut = getattr(handler, "__module__", "").endswith(".task_shortcuts")
    if not (
        name.startswith("_collaboration_task_")
        or name
        in {
            "_collaboration_packet",
            "_collaboration_changes",
            "_collaboration_doctor",
            "merged",
        }
        or getattr(args, "command", None) in {"worker", "dispatch"}
        or delivery_shortcut
    ):
        return None
    backend = backend_for(
        getattr(args, "data_root", None),
        getattr(args, "project_id", None),
        getattr(args, "root", None),
    )
    if backend is None:
        if name == "_collaboration_task_update" and any(
            getattr(args, key, None)
            for key in ("title", "objective", "priority", "exit_criterion")
        ):
            raise GiteaError(
                "these task definition updates require the shared Issue backend; local progress update contract is unchanged"
            )
        if name.startswith("_collaboration_task_") and any(
            getattr(args, key, None)
            for key in (
                "operation_id",
                "expected_remote_revision",
                "pr",
                "dependency",
                "mine",
                "created_by_me",
                "review_requested",
                "dependencies_ready",
            )
        ):
            raise GiteaError(
                "shared task options require an actual gitea-issues declaration; refusing a local-only write or query"
            )
        return None
    if delivery_shortcut:
        raise GiteaError(
            "shared delivery: use task update --status merged --expected-remote-revision REV --operation-id ID; merged PR is verified remotely"
        )
    if name.startswith("_collaboration_task_"):
        return route_task(args, backend)
    if name == "_collaboration_doctor":
        _print(args, backend.client.doctor())
        return 0
    if name in {"_collaboration_packet", "_collaboration_changes"}:
        if (
            getattr(args, "since", None) not in {None, 0}
            or getattr(args, "ack", None) is not None
        ):
            raise GiteaError(
                "shared changes use backend sync --consumer NAME --ack-revision REV, not local integer cursors"
            )
        _print(
            args,
            backend.sync(
                consumer=getattr(args, "consumer", None) or "cli",
                budget=getattr(args, "max_output_bytes", 8192),
            ),
        )
        return 0
    raise GiteaError(
        "local worker dispatch is not shared-task ownership; use your local AI with the Issue task context"
    )
