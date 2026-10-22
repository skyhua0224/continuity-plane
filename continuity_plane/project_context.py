"""Declared project context: session bindings, integration source and lessons.

These records confer no native account access or execution authority. Native
usage continues to be collected only by the owning host/profile.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from .collaboration_registry import (
    _atomic_write,
    _locked,
    _now,
    _record_change,
    _load_ledger,
    default_data_root,
    latest_change_cursor,
    project_data_dir,
    resolve_project,
    list_projects,
)


def _path(data_root, project_id):
    return (
        project_data_dir(data_root or default_data_root(), project_id)
        / "project-context.json"
    )


def _load(path):
    return (
        json.loads(path.read_text())
        if path.exists()
        else {
            "schema_version": "context.project-context/v1",
            "sessions": {},
            "lessons": {},
        }
    )


def _text(value, name, limit=8192):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > limit
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
    ):
        raise ValueError(f"{name} must be nonempty text within {limit} bytes")
    from .vocabulary_memory import _SECRET_RE

    if _SECRET_RE.search(value):
        raise ValueError(
            "shared project context must not contain credential assignments; redact before recording"
        )
    return value


def _session(provider, session_id):
    if provider not in {"claude", "codex"}:
        raise ValueError("session provider must be explicit: claude or codex")
    return (
        provider
        + ":"
        + hashlib.sha256(_text(session_id, "session-id", 256).encode()).hexdigest()
    )


def binding(data_root, project_id, provider, session_id):
    return _load(_path(data_root, project_id))["sessions"].get(
        _session(provider, session_id)
    )


def _observed(root, data_root, provider, session_hash):
    from .provider_usage import report

    observations = [
        r
        for r in report(root, data_root, include_bindings=False)["session_observations"]
        if r["provider"] == provider and r["session"] == session_hash
    ]
    if len(observations) > 1:
        raise ValueError(
            "session has multiple collection origins; do not combine different host/profile counters"
        )
    return observations[0] if observations else None


def bind(
    root,
    data_root,
    project_id,
    *,
    provider,
    session_id,
    task_id=None,
    batch_id=None,
    role="controller",
    unbind=False,
):
    if role not in {"controller", "worker"}:
        raise ValueError("binding role must be controller or worker")
    key = _session(provider, session_id)
    ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
    if not unbind:
        if not task_id and not batch_id:
            raise ValueError("binding needs --task-id or --batch-id")
        if task_id and task_id not in {t["task_id"] for t in ledger["tasks"]}:
            raise ValueError("binding task does not exist in this project")
        batch = ledger.get("workflow_records", {}).get("batch", {}).get(batch_id)
        if batch_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", batch_id):
            raise ValueError("batch id is invalid")
        if batch and task_id and task_id not in batch["value"]["task_ids"]:
            raise ValueError("binding task is not in this batch")
    sample = _observed(root, data_root, provider, key.split(":", 1)[1])
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        previous = doc["sessions"].get(key)
        if (
            not unbind
            and previous
            and previous.get("active")
            and (
                previous.get("task_id"),
                previous.get("batch_id"),
                previous.get("role", "controller"),
            )
            == (task_id, batch_id, role)
        ):
            return previous
        history = list((previous or {}).get("history", []))
        if previous and previous.get("active"):
            history.append(
                {k: v for k, v in previous.items() if k != "history"}
                | {
                    "end_tokens": (sample or {}).get("tokens"),
                    "ended_at": _now(),
                    "end_origin": (sample or {}).get("collection_origin"),
                }
            )
        value = {
            "provider": provider,
            "session": key.split(":", 1)[1],
            "task_id": task_id,
            "batch_id": batch_id,
            "role": role,
            "active": not unbind,
            "bound_at": _now(),
            "start_tokens": (sample or {}).get("tokens"),
            "collection_origin": (sample or {}).get("collection_origin"),
            "history": history,
            "authority": "declared-project-context-not-native-authentication",
        }
        doc["sessions"][key] = value
        _atomic_write(path, doc)
    _record_change(
        Path(data_root or default_data_root()),
        project_id,
        "session.unbound" if unbind else "session.bound",
        task_id,
        batch_id=batch_id,
    )
    return value


def binding_usage(data_root, project_id, observations):
    sessions = _load(_path(data_root, project_id))["sessions"]
    tasks, batches = {}, {}
    unknown = 0
    for current in sessions.values():
        rows = [
            *current.get("history", []),
            *([current] if current.get("active") else []),
        ]
        sample = [
            r
            for r in observations
            if r["provider"] == current["provider"]
            and r["session"] == current["session"]
        ]
        for row in rows:
            start = row.get("start_tokens")
            end = (
                row.get("end_tokens")
                if "end_tokens" in row
                else (sample[0]["tokens"] if len(sample) == 1 else None)
            )
            origin = (
                row.get("end_origin")
                if "end_tokens" in row
                else (sample[0].get("collection_origin") if len(sample) == 1 else None)
            )
            if (
                start is None
                or end is None
                or not origin
                or origin != row.get("collection_origin")
                or any(end.get(k, 0) < v for k, v in start.items())
            ):
                unknown += 1
                continue
            delta = {k: v - start.get(k, 0) for k, v in end.items()}
            for key, target in (
                (row.get("task_id"), tasks),
                (row.get("batch_id"), batches),
            ):
                if key:
                    totals = target.setdefault(key, {})
                    for token, count in delta.items():
                        totals[token] = totals.get(token, 0) + count
    return {
        "by_task": tasks,
        "by_batch": batches,
        "unattributable_segments": unknown,
        "attribution": "observed counter deltas between explicit session bindings; no retroactive or monetary cost estimate",
        "cost_available": False,
    }


def establish_pending(data_root, project_id, observations):
    """The first observed counter becomes a baseline, never retroactive usage."""
    path = _path(data_root, project_id)
    if not path.exists():
        return
    with _locked(path):
        doc = _load(path)
        changed = False
        for row in doc["sessions"].values():
            if not row.get("active") or row.get("start_tokens") is not None:
                continue
            samples = [
                r
                for r in observations
                if r["provider"] == row["provider"] and r["session"] == row["session"]
            ]
            if len(samples) == 1 and samples[0].get("collection_origin"):
                row.update(
                    start_tokens=samples[0]["tokens"],
                    collection_origin=samples[0]["collection_origin"],
                    baseline_observed_at=_now(),
                )
                changed = True
        if changed:
            _atomic_write(path, doc)


def context_set(
    data_root,
    project_id,
    integration_root,
    integration_ref="refs/heads/main",
    *,
    controller=None,
    controller_token=None,
):
    from .lifecycle import _git
    from .controller_lease import require

    require(data_root, project_id, controller, controller_token)

    root = Path(integration_root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(
            "integration-root must be an existing absolute path on the authority host"
        )
    if (
        isinstance(integration_ref, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,255}", integration_ref)
        and not integration_ref.startswith("refs/")
    ):
        integration_ref = "refs/heads/" + integration_ref
    if not isinstance(integration_ref, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_./-]{0,255}", integration_ref
    ):
        raise ValueError("integration-ref is invalid")
    if not _git(root, "rev-parse", "--verify", integration_ref + "^{commit}"):
        raise ValueError(
            "integration ref is not available on this host; no checkout HEAD substituted"
        )
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        from .controller_lease import _require

        _require(doc, controller, controller_token)
        doc["integration"] = {
            "root": str(root.resolve()),
            "ref": integration_ref,
            "recorded_at": _now(),
        }
        _atomic_write(path, doc)
    return doc["integration"]


def integration(data_root, project_id, fallback_root):
    from .lifecycle import _git

    try:
        selected = _load(_path(data_root, project_id)).get(
            "integration", {"root": str(fallback_root), "ref": "refs/heads/main"}
        )
    except (OSError, ValueError, KeyError):
        return {
            "head": None,
            "available": False,
            "source": "unavailable-project-context",
            "checkout_head_substituted": False,
        }
    head = _git(
        Path(selected["root"]), "rev-parse", "--verify", selected["ref"] + "^{commit}"
    )
    return {
        **selected,
        "head": head,
        "source": "authority-host-git-ref",
        "available": bool(head),
        "checkout_head_substituted": False,
    }


def add_lesson(
    data_root, project_id, *, lesson_id, text, source, tags=(), task_id=None
):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", lesson_id):
        raise ValueError("lesson-id is invalid")
    _text(text, "lesson text")
    _text(source, "lesson source", 2048)
    if (
        not isinstance(tags, (list, tuple))
        or len(tags) > 32
        or any(not isinstance(t, str) or not t.strip() or len(t) > 64 for t in tags)
    ):
        raise ValueError("lesson tags must be short nonempty strings")
    if task_id and task_id not in {
        t["task_id"]
        for t in _load_ledger(Path(data_root or default_data_root()), project_id)[
            "tasks"
        ]
    }:
        raise ValueError("lesson task does not exist in this project")
    value = {
        "lesson_id": lesson_id,
        "text": text,
        "source": source,
        "tags": list(tags),
        "task_id": task_id,
        "authority": "reported-experience-not-policy-or-verified-evidence",
    }
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        old = doc["lessons"].get(lesson_id)
        if old:
            if {k: v for k, v in old.items() if k != "recorded_at"} != value:
                raise ValueError(
                    "lesson id already exists; use a new correction id to retain history"
                )
            return old
        value["recorded_at"] = _now()
        doc["lessons"][lesson_id] = value
        _atomic_write(path, doc)
    _record_change(
        Path(data_root or default_data_root()),
        project_id,
        "lesson.recorded",
        task_id,
        lesson_id=lesson_id,
    )
    return value


def lessons(data_root, project_id, *, query=None, tag=None, max_results=20):
    if type(max_results) is not int or not 1 <= max_results <= 100:
        raise ValueError("max-results must be 1–100")
    rows = [
        r
        for r in reversed(list(_load(_path(data_root, project_id))["lessons"].values()))
        if (not tag or tag in r["tags"])
        and (
            not query
            or query.casefold() in (r["lesson_id"] + " " + r["text"]).casefold()
        )
    ]
    return {
        "items": rows[:max_results],
        "total_items": len(rows),
        "has_more": len(rows) > max_results,
    }


def handoff(
    root,
    data_root,
    project_id,
    *,
    plan=None,
    next_action=None,
    batch_id=None,
    pending=None,
    completed_batch=None,
    controller=None,
    controller_token=None,
    auto=False,
    capture_member=None,
):
    from .controller_awareness import scene
    from .controller_lease import require

    require(data_root, project_id, controller, controller_token)

    if batch_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", batch_id):
        raise ValueError("handoff batch id is invalid")
    for name, value in (("plan", plan), ("next action", next_action)):
        if value is not None:
            _text(value, name, 8192)
    if pending is not None and (
        not isinstance(pending, list)
        or len(pending) > 64
        or any(
            not isinstance(v, str) or not v.strip() or len(v.encode()) > 2048
            for v in pending
        )
    ):
        raise ValueError(
            "pending decisions must be at most 64 nonempty texts within 2048 bytes each"
        )
    if pending is not None:
        for item in pending:
            _text(item, "pending decision", 2048)
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        from .controller_lease import _require

        _require(doc, controller, controller_token)
        # Snapshot under the same lock used by batch writers. An old batch
        # retry repairs the latest projection; it must not rewind a later batch.
        ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
        previous = doc.get("handoff", {})
        snapshot = (
            scene(data_root, project_id, root=root, max_bytes=16384, max_workers=64)
            if auto
            else {
                "status": "deferred",
                "basis": "fast save; observations not collected",
                "change_cursor": latest_change_cursor(
                    Path(data_root or default_data_root()),
                    project_id=project_id,
                ),
                "details_command": "continuity handoff show --project-id " + project_id,
            }
        )
        value = {
            "schema_version": "context.controller-handoff/v1",
            "project_id": project_id,
            "current_batch": batch_id
            if batch_id is not None
            else previous.get("current_batch"),
            "latest_integrated_batch": next(
                reversed(ledger.get("workflow_records", {}).get("batch", {})),
                completed_batch or previous.get("latest_integrated_batch"),
            ),
            "plan": plan if plan is not None else previous.get("plan"),
            "next_action": next_action
            if next_action is not None
            else previous.get("next_action"),
            "pending_decisions": pending
            if pending is not None
            else previous.get("pending_decisions", []),
            "scene": snapshot,
            "authority": "declared-controller-plan-and-observed-project-metadata; no external-operation approval",
        }
        if auto:
            from .controller_observation import draft

            value["draft"] = draft(data_root, project_id)
        elif previous.get("draft"):
            value["draft"] = previous["draft"]
        value["batch_state"] = (
            "recorded"
            if value["current_batch"]
            in ledger.get("workflow_records", {}).get("batch", {})
            else "planned-or-unbound"
        )
        if len(json.dumps(value, ensure_ascii=False).encode()) > 32768:
            raise ValueError(
                "controller handoff exceeds 32768 bytes; split long details into referenced project artifacts"
            )
        old_content = {
            k: v for k, v in previous.items() if k not in {"revision", "updated_at"}
        }
        if value == old_content:
            if capture_member:
                from .controller_session import capture

                capture(
                    root, data_root, project_id, capture_member, previous["revision"]
                )
            return previous
        value.update(revision=previous.get("revision", 0) + 1, updated_at=_now())
        history_path = (
            _path(data_root, project_id).parent
            / "controller-handoffs"
            / (str(value["revision"]) + ".json")
        )
        _atomic_write(history_path, value)
        doc["handoff"] = value
        _atomic_write(path, doc)
    if capture_member:
        from .controller_session import capture

        capture(root, data_root, project_id, capture_member, value["revision"])
    return value


def handoff_summary(data_root, project_id):
    shared = _load(_path(data_root, project_id)).get("handoff")
    if not shared:
        return {"status": "not-recorded"}
    return {
        "revision": shared["revision"],
        "current_batch": shared.get("current_batch"),
        "latest_integrated_batch": shared.get("latest_integrated_batch"),
        "batch_state": shared.get("batch_state"),
        "plan": (shared.get("plan") or "")[:300],
        "next_action": (shared.get("next_action") or "")[:240],
        "pending_decisions": [v[:120] for v in shared.get("pending_decisions", [])[:3]],
        "pending_total": len(shared.get("pending_decisions", [])),
        "details_command": f"continuity handoff show --project-id {project_id}",
        "authority": "declared-plan-not-ownership-or-operation-approval",
    }


def resolve_context_args(args):
    root = args.root
    registration = resolve_project(root or ".", args.data_root)
    registered_by_id = None
    if args.project_id:
        registered_by_id = next(
            (
                p
                for p in list_projects(args.data_root)
                if p["project_id"] == args.project_id
            ),
            None,
        )
    if args.project_id and root is None:
        registration = registered_by_id
    if args.project_id and registered_by_id is None:
        raise ValueError(
            "project-id is not registered in this data root: "
            f"requested_project_id={args.project_id} "
            f"requested_root={str(Path(root).resolve()) if root else None} "
            f"data_root={Path(args.data_root).resolve() if args.data_root else Path(default_data_root()).resolve()}; "
            "register the owning root on this host before retrying. If the authoritative state is on another host, use the explicit --remote/--remote-root CLI route instead of copying its data."
        )
    if not registration:
        raise ValueError(
            "root is not registered in this data root: "
            f"requested_root={str(Path(root).resolve()) if root else None} "
            f"requested_project_id={args.project_id} "
            f"data_root={Path(args.data_root).resolve() if args.data_root else Path(default_data_root()).resolve()}; "
            "if the registered project belongs to another host, use the explicit --remote/--remote-root route"
        )
    if args.project_id and registration["project_id"] != args.project_id:
        raise ValueError(
            "registered root and project-id identify different projects: "
            f"requested_root={str(Path(root).resolve()) if root else None} "
            f"requested_project_id={args.project_id} "
            f"root_project_id={registration['project_id']} "
            f"root_project_root={registration.get('control_root')} "
            f"requested_project_registered_root={registered_by_id.get('control_root')} "
            f"requested_project_registered_id={registered_by_id['project_id']}; "
            f"re-run with --root {registered_by_id.get('control_root')} or omit --root while keeping --project-id {args.project_id}. "
            "If the owning registration is on another host, use the explicit --remote/--remote-root CLI route."
        )
    args.root = str(Path(root or registration["control_root"]).resolve())
    args.project_id = registration["project_id"]
    return registration


def command(args):
    from .planning_api import render

    render({}, max_output_bytes=args.max_output_bytes)
    registration = resolve_context_args(args)
    project_id = args.project_id
    action = args.project_context_action
    if action in {"session-bind", "session-unbind"}:
        value = bind(
            args.root,
            args.data_root,
            project_id,
            provider=args.provider,
            session_id=args.session_id,
            task_id=getattr(args, "task_id", None),
            batch_id=getattr(args, "batch_id", None),
            role=getattr(args, "role", "controller"),
            unbind=action.endswith("unbind"),
        )
    elif action == "session-show":
        value = binding(args.data_root, project_id, args.provider, args.session_id) or {
            "status": "unbound"
        }
    elif action == "lesson-add":
        value = add_lesson(
            args.data_root,
            project_id,
            lesson_id=args.lesson_id,
            text=args.text,
            source=args.source,
            tags=args.tag,
            task_id=args.task_id,
        )
    elif action == "lesson-list":
        value = lessons(
            args.data_root,
            project_id,
            query=args.query,
            tag=args.tag,
            max_results=args.max_results,
        )
    elif action == "lesson-show":
        value = _load(_path(args.data_root, project_id))["lessons"].get(args.lesson_id)
        if value is None:
            raise ValueError("lesson does not exist")
    elif action == "context-set":
        value = context_set(
            args.data_root,
            project_id,
            args.integration_root,
            args.integration_ref,
            controller=args.controller,
            controller_token=args.controller_token,
        )
    elif action == "handoff-write":
        value = handoff(
            args.root,
            args.data_root,
            project_id,
            plan=args.plan,
            next_action=args.next_action,
            batch_id=args.batch_id,
            pending=args.pending,
            controller=args.controller,
            controller_token=args.controller_token,
            auto=args.auto,
            capture_member=args.capture_member,
        )
    elif action == "handoff-show":
        from .controller_observation import handoff_view

        value = handoff_view(
            args.data_root, project_id, recipient=os.environ.get("CONTINUITY_ASSIGNEE")
        )
    else:
        value = integration(
            args.data_root,
            project_id,
            (registration or {}).get("control_root", args.root),
        )
    print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands, collaboration):
    for group_name, actions in (
        ("session", ("bind", "show", "unbind")),
        ("lesson", ("add", "list", "show")),
        ("handoff", ("write", "show")),
    ):
        group = commands.add_parser(
            group_name,
            help="Declared project context; no native account access or provider launch",
        )
        leaves = group.add_subparsers(dest=group_name + "_command", required=True)
        for action in actions:
            _arguments(leaves.add_parser(action), group_name + "-" + action)
    for name in ("context-set", "context-show"):
        _arguments(
            collaboration.add_parser(
                name,
                help="Select/read an authoritative integration Git ref, never an arbitrary checkout HEAD",
            ),
            name,
        )


def _arguments(parser, action):
    parser.set_defaults(handler=command, project_context_action=action)
    for key in ("root", "project-id", "data-root"):
        parser.add_argument("--" + key, default=None)
    parser.add_argument("--format", choices=("brief", "json"), default="json")
    parser.add_argument("--max-output-bytes", type=int, default=8192)
    if action.startswith("session-"):
        parser.add_argument("--provider", choices=("claude", "codex"), required=True)
        parser.add_argument(
            "--session-id",
            required=True,
            help="Own native session identifier; only its hash is stored; not authentication",
        )
        if action == "session-bind":
            parser.add_argument("--task-id")
            parser.add_argument("--batch-id")
            parser.add_argument(
                "--role", choices=("controller", "worker"), default="controller"
            )
    elif action == "context-set":
        parser.add_argument("--integration-root", required=True)
        parser.add_argument(
            "--integration-ref",
            "--integration-base",
            dest="integration_ref",
            default="refs/heads/main",
            help="Project default implementation base; refs/heads/ is added for a base branch name",
        )
        parser.add_argument("--controller")
        parser.add_argument("--controller-token")
    elif action == "handoff-write":
        parser.add_argument(
            "--capture-member",
            help="Explicit private profile snapshot for this local member; never stored in shared handoff",
        )
        parser.add_argument(
            "--auto",
            action="store_true",
            help="Refresh observations and generate an evidence-linked candidate draft; preserve declared plan",
        )
        parser.add_argument("--plan")
        parser.add_argument("--next-action")
        parser.add_argument("--batch-id")
        parser.add_argument(
            "--pending",
            action="append",
            help="Declared item awaiting a decision; repeatable, does not imply approval",
        )
        parser.add_argument("--controller")
        parser.add_argument("--controller-token")
    elif action.startswith("lesson-"):
        if action != "lesson-list":
            parser.add_argument("--lesson-id", required=True)
        if action == "lesson-add":
            parser.add_argument("--text", required=True)
            parser.add_argument("--source", required=True)
            parser.add_argument("--task-id")
            parser.add_argument("--tag", action="append", default=[])
        elif action == "lesson-list":
            parser.add_argument("--query")
            parser.add_argument("--tag")
            parser.add_argument("--max-results", type=int, default=20)
