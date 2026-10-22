"""Observed origin/model cooldown, not native account quota polling."""

from datetime import UTC, datetime

from .collaboration_registry import _atomic_write, _load, _locked, project_data_dir
from .runtime_origin import current, key, require_local


def _path(data_root, project_id, origin, model):
    from .executor_routing import _model_alias
    from .controller_memory import sha

    return (
        project_data_dir(data_root, project_id)
        / "dispatch-health"
        / (sha({"origin": key(origin), "model": _model_alias(model)}) + ".json")
    )


def record(
    data_root,
    project_id,
    origin,
    model,
    failure,
    *,
    task_id=None,
    attempt_id=None,
):
    if failure.get("failure_class") not in {
        "quota-exhausted",
        "rate-limit",
        "capacity",
    }:
        return None
    require_local(origin, origin["provider"])
    path = _path(data_root, project_id, origin, model)
    with _locked(path):
        item = {
            "failure_class": failure["failure_class"],
            "available_at": failure.get("available_at"),
            "origin": origin,
            "model": model,
            "basis": "observed worker error; no subscription query",
            "reason": failure.get("reason") or failure.get("guidance"),
            "failure_guidance": failure.get("guidance"),
            "task_id": task_id,
            "attempt_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat(),
            "status": "paused",
        }
        _atomic_write(path, item)
    return item


def require_available(data_root, project_id, origin, model, *, now=None):
    require_local(origin, origin["provider"])
    path = _path(data_root, project_id, origin, model)
    item = _load(path)
    if not item or item.get("status") != "paused":
        return
    now = now or datetime.now(UTC)
    until = item.get("available_at")
    if until and datetime.fromisoformat(until.replace("Z", "+00:00")) <= now:
        return
    # Unknown rate-limit/capacity resets get no invented indefinite quota claim.
    if not until and item["failure_class"] != "quota-exhausted":
        return
    raise ValueError(
        "origin/model dispatch paused: "
        + item["failure_class"]
        + "; reset="
        + (until or "unknown")
        + "; reason="
        + str(item.get("reason") or item.get("basis") or "not-recorded")
        + "; task="
        + str(item.get("task_id") or "unknown")
        + "; inspect the original attempt; use dispatch-health clear with a recorded reason after reconciliation"
    )


def clear(data_root, project_id, origin, model, reason):
    from .project_context import _text

    _text(reason, "availability reconciliation reason", 2048)
    require_local(origin, origin["provider"])
    path = _path(data_root, project_id, origin, model)
    with _locked(path):
        item = _load(path)
        if not item:
            raise ValueError("no observed cooldown exists")
        item.update(status="manually-reconciled", reason=reason)
        _atomic_write(path, item)
    return {"status": "reconciled", "account_query": False}


def show(data_root, project_id):
    folder = project_data_dir(data_root, project_id) / "dispatch-health"
    rows = []
    if folder.exists():
        for path in sorted(folder.glob("*.json")):
            value = _load(path)
            if value:
                rows.append(
                    {
                        "model": value.get("model"),
                        "failure_class": value.get("failure_class"),
                        "available_at": value.get("available_at"),
                        "status": value.get("status"),
                        "basis": value.get("basis"),
                        "reason": value.get("reason"),
                        "explanation": value.get("reason")
                        or value.get("failure_guidance")
                        or "No provider detail was stored with this observed cooldown.",
                        "failure_guidance": value.get("failure_guidance"),
                        "task_id": value.get("task_id"),
                        "attempt_id": value.get("attempt_id"),
                        "observed_at": value.get("observed_at"),
                        "scope": "project data root + host/profile + model",
                    }
                )
    return {
        "status": "local-observed",
        "items": rows,
        "native_account_query": False,
    }


def register_cli(commands):
    parser = commands.add_parser(
        "dispatch-health",
        help="Observed worker cooldowns; no account quota polling",
    )
    leaves = parser.add_subparsers(dest="health_action", required=True)
    for action in ("show", "clear"):
        leaf = leaves.add_parser(action)
        leaf.add_argument("--project-id", required=True)
        leaf.add_argument("--data-root")
        leaf.add_argument("--format", choices=("brief", "json"), default="brief")
        leaf.add_argument("--max-output-bytes", type=int, default=4096)
        if action == "clear":
            leaf.add_argument(
                "--provider", choices=("codex", "claude", "custom"), required=True
            )
            leaf.add_argument("--model", required=True)
            leaf.add_argument("--reason", required=True)
        leaf.set_defaults(handler=command, health_action=action)


def command(args):
    from .planning_api import render

    if args.health_action == "show":
        value = show(args.data_root, args.project_id)
    else:
        value = clear(
            args.data_root,
            args.project_id,
            current(args.provider),
            args.model,
            args.reason,
        )
    print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0
