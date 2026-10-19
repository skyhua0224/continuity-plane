"""Explicit subscription quota facts and approved, idle-only session handoffs."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    project_data_dir,
    _load_ledger,
    _record_change,
    resolve_project,
)


def _path(data_root, project_id):
    return (
        project_data_dir(Path(data_root or default_data_root()), project_id)
        / "subscription-quota.json"
    )


def _load(path):
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"models": {}, "handoffs": {}}
    )


def record(
    data_root,
    project_id,
    model,
    *,
    used_percent,
    resets_at,
    source,
    official_subscription,
    now=None,
    provider="codex",
    origin=None,
):
    now = now or datetime.now(UTC)
    if not official_subscription:
        raise ValueError(
            "subscription quota must be explicitly identified; token counts are not subscription limits"
        )
    if type(used_percent) not in (int, float) or not 0 <= used_percent <= 100:
        raise ValueError("used-percent must be 0–100")
    until = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    if (
        not until.tzinfo
        or until <= now
        or not isinstance(source, str)
        or not source.strip()
    ):
        raise ValueError(
            "quota needs a future timezone-bearing reset and an explicit source"
        )
    from .executor_routing import _model_alias
    from .runtime_origin import current, key, require_local

    origin = origin or current(provider)
    require_local(origin, provider)

    model = _model_alias(model)
    item = {
        "model": model,
        "used_percent": used_percent,
        "resets_at": until.isoformat(),
        "observed_at": now.isoformat(),
        "source": source,
        "official_subscription": True,
        "collection_origin": origin,
    }
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        identity = key(origin) + ":" + model
        previous = doc["models"].get(identity, {})
        doc["models"][identity] = item
        _atomic_write(path, doc)
    status = (
        "exhausted"
        if used_percent >= 100
        else "near-limit"
        if used_percent >= 90
        else "available"
    )
    before = previous.get("used_percent", 0)
    old_status = (
        "exhausted" if before >= 100 else "near-limit" if before >= 90 else "available"
    )
    if status != old_status:
        _record_change(
            Path(data_root or default_data_root()),
            project_id,
            "quota.changed",
            model,
            model=model,
            status=status,
        )
    return item


def report(data_root, project_id, *, now=None):
    now = now or datetime.now(UTC)
    doc = _load(_path(data_root, project_id))
    models = {}
    foreign = 0
    from .runtime_origin import current, validate

    for identity, item in doc["models"].items():
        origin = item.get("collection_origin")
        if origin is None or validate(origin) != current(origin["provider"]):
            foreign += 1
            continue
        model = item["model"]
        observed = datetime.fromisoformat(item["observed_at"])
        reset = datetime.fromisoformat(item["resets_at"])
        status = (
            "needs-refresh"
            if reset <= now
            else "exhausted"
            if item["used_percent"] >= 100
            else "needs-refresh"
            if now - observed > timedelta(minutes=15)
            else "near-limit"
            if item["used_percent"] >= 90
            else "available"
        )
        models[model] = {**item, "status": status}
    return {
        "models": models,
        "handoffs": list(doc["handoffs"].values()),
        "coverage": "explicit subscription snapshots only; unknown is not zero usage",
        "other_or_unscoped_origins_ignored": foreign,
    }


def automatic_available(data_root, project_id, model, *, profile=None, now=None):
    """Native subscription snapshots never govern an unrelated gateway route."""
    if profile is None:
        path = Path(data_root or default_data_root()) / "executor-adapters.json"
        profiles = json.loads(path.read_text()) if path.exists() else {}
        profile = profiles.get(model, {})
    if not profile.get("official_subscription"):
        return True
    from .executor_routing import _model_alias

    quota = report(data_root, project_id, now=now)["models"].get(
        _model_alias(model), {}
    )
    return (
        quota.get("collection_origin") == profile.get("execution_origin")
        and quota.get("status") == "available"
    )


def propose(
    data_root, project_id, task_id, primary_model, target_model, *, approval_source=None
):
    ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
    task = next((t for t in ledger["tasks"] if t["task_id"] == task_id), None)
    if not task:
        raise ValueError("task_id does not exist")
    from .delivery_records import scope_hash

    scope = scope_hash(
        task, ledger.get("task_details", {}).get(task_id, {}).get("body", "")
    )
    path = _path(data_root, project_id)
    jobs_path = path.parent / "worker-jobs.json"
    jobs = json.loads(jobs_path.read_text()) if jobs_path.exists() else {}
    previous_job = jobs.get(task_id, {})
    if previous_job.get("model") != primary_model:
        raise ValueError("primary model must match the recorded task session")
    adapters = Path(data_root or default_data_root()) / "executor-adapters.json"
    configured = json.loads(adapters.read_text()) if adapters.exists() else {}
    if target_model not in configured or primary_model == target_model:
        raise ValueError("handoff target must be another configured adapter")
    identity = hashlib.sha256(
        (task_id + scope + previous_job["job_id"] + target_model).encode()
    ).hexdigest()[:20]
    with _locked(path):
        doc = _load(path)
        old = doc["handoffs"].get(identity, {})
        value = {
            **old,
            "id": identity,
            "task_id": task_id,
            "scope_sha256": scope,
            "primary_model": primary_model,
            "target_model": target_model,
            "primary_job": previous_job["job_id"],
            "approved": bool(approval_source),
            "approval_source": approval_source,
            "status": old.get("status", "waiting"),
        }
        if old.get("approved") and not approval_source:
            value["approved"], value["approval_source"] = True, old["approval_source"]
        doc["handoffs"][identity] = value
        _atomic_write(path, doc)
    return value


def return_ready(root, data_root, project_id, handoff_id, *, execute=False):
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        value = doc["handoffs"].get(handoff_id)
        if not value:
            raise ValueError("handoff does not exist")
        if value["status"] == "returned":
            return {**value, "executed": False}
        if not value["approved"]:
            return {**value, "next_action": "request-user-approval", "executed": False}
        ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
        task = next(t for t in ledger["tasks"] if t["task_id"] == value["task_id"])
        from .delivery_records import scope_hash

        if value["scope_sha256"] != scope_hash(
            task,
            ledger.get("task_details", {}).get(task["task_id"], {}).get("body", ""),
        ):
            return {
                **value,
                "next_action": "scope-changed-review-required",
                "executed": False,
            }
        from .worker_dispatch import require_policy_clear

        require_policy_clear(
            data_root, project_id, task["task_id"], value["scope_sha256"]
        )
        if (
            ledger.get("execution_bindings", {}).get(task["task_id"], {}).get("status")
            == "running"
        ):
            return {**value, "next_action": "wait-for-idle", "executed": False}
        from .executor_routing import _model_alias

        quota = report(data_root, project_id)["models"].get(
            _model_alias(value["primary_model"]), {}
        )
        if quota.get("status") != "available":
            return {**value, "next_action": "refresh-primary-quota", "executed": False}
        primary_file = path.parent / "worker-jobs" / value["primary_job"] / "job.json"
        primary = json.loads(primary_file.read_text())
        if quota.get("collection_origin") != primary.get("execution_origin"):
            return {
                **value,
                "next_action": "quota-origin-does-not-match-primary",
                "executed": False,
            }
        if not primary.get("profile", {}).get("official_subscription"):
            return {
                **value,
                "next_action": "adapter-subscription-binding-not-declared",
                "executed": False,
            }
        if not execute:
            return {
                **value,
                "next_action": "resume-approved-primary-session",
                "executed": False,
            }
        from .worker_dispatch import dispatch

        result = dispatch(
            root,
            data_root=data_root,
            project_id=project_id,
            task_id=task["task_id"],
            model=value["primary_model"],
            resume=True,
            resume_job=value["primary_job"],
            background=True,
            handoff={"approval_id": value["id"], "preserve_progress": True},
        )
        value["status"] = "returned"
        value["return_job"] = result["job_id"]
        _atomic_write(path, doc)
        return {**value, "executed": True}


def command(args):
    from .planning_api import render

    render({}, max_output_bytes=args.max_output_bytes)
    project = resolve_project(args.root, args.data_root)
    project_id = args.project_id or (project or {}).get("project_id")
    if not project_id:
        raise ValueError("registered root or project-id required")
    if args.quota_action == "refresh":
        result = refresh(
            args.data_root,
            project_id,
            args.model,
            executable=args.executable,
            provider=args.provider,
        )
    elif args.quota_action == "tick":
        for model in args.refresh_model:
            refresh(args.data_root, project_id, model, executable=args.executable)
        result = {
            "items": [
                advance(
                    args.root,
                    args.data_root,
                    project_id,
                    value["id"],
                    execute=args.execute_approved,
                )
                for value in report(args.data_root, project_id)["handoffs"]
                if value["approved"] and value["status"] != "returned"
            ]
        }
    elif args.quota_action == "record":
        result = record(
            args.data_root,
            project_id,
            args.model,
            used_percent=args.used_percent,
            resets_at=args.resets_at,
            source=args.source,
            official_subscription=args.official_subscription,
            provider=args.provider,
        )
    elif args.quota_action == "handoff":
        result = propose(
            args.data_root,
            project_id,
            args.task_id,
            args.model,
            args.target_model,
            approval_source=args.approval_source,
        )
    elif args.quota_action == "return":
        result = return_ready(
            args.root, args.data_root, project_id, args.handoff_id, execute=args.execute
        )
    else:
        result = report(args.data_root, project_id)
    print(render(result, format="json", max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    parser = commands.add_parser(
        "quota",
        help="Subscription quota facts; explicit approved handoffs, never infer limits from token counts",
    )
    group = parser.add_subparsers(dest="quota_action", required=True)
    for action in ("record", "report", "handoff", "return", "refresh", "tick"):
        leaf = group.add_parser(action)
        leaf.set_defaults(handler=command)
        leaf.add_argument("--root", default=".")
        leaf.add_argument("--project-id")
        leaf.add_argument("--data-root")
        leaf.add_argument("--max-output-bytes", type=int, default=8192)
        if action in {"record", "handoff", "refresh"}:
            leaf.add_argument("--model", required=True)
        if action == "record":
            leaf.add_argument(
                "--provider",
                choices=("codex", "claude", "custom"),
                required=True,
                help="Querying profile namespace, not a model-name guess",
            )
            leaf.add_argument("--used-percent", required=True, type=float)
            leaf.add_argument("--resets-at", required=True)
            leaf.add_argument("--source", required=True)
            leaf.add_argument("--official-subscription", action="store_true")
        if action == "handoff":
            leaf.add_argument("--task-id", required=True)
            leaf.add_argument("--target-model", required=True)
            leaf.add_argument("--approval-source")
        if action == "return":
            leaf.add_argument("--handoff-id", required=True)
            leaf.add_argument(
                "--execute",
                action="store_true",
                help="Explicitly run an approved return, only when idle and quota is freshly available",
            )
        if action == "refresh":
            leaf.add_argument(
                "--provider",
                choices=("codex", "claude"),
                required=True,
                help="Explicit native client; unsupported pollers never fall back to another account",
            )
            leaf.add_argument(
                "--executable",
                default="codex",
                help="Existing Codex CLI; no login/config changes or model call",
            )
        if action == "tick":
            leaf.add_argument(
                "--refresh-model",
                action="append",
                default=[],
                help="Explicit native Codex subscription models to refresh before evaluating handoffs",
            )
            leaf.add_argument("--executable", default="codex")
            leaf.add_argument(
                "--execute-approved",
                action="store_true",
                help="Opt in to approved idle-only returns; suitable for a caller-owned timer",
            )


def advance(root, data_root, project_id, handoff_id, *, execute=False):
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        value = doc["handoffs"].get(handoff_id)
        if not value or not value["approved"]:
            return {"executed": False, "next_action": "request-user-approval"}
        if value["status"] == "waiting":
            ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
            if (
                ledger.get("execution_bindings", {})
                .get(value["task_id"], {})
                .get("status")
                == "running"
            ):
                return {**value, "executed": False, "next_action": "wait-for-idle"}
            primary = path.parent / "worker-jobs" / value["primary_job"] / "result.json"
            result = json.loads(primary.read_text()) if primary.exists() else {}
            if not result.get("retryable"):
                return {
                    **value,
                    "executed": False,
                    "next_action": "no-transient-primary-failure",
                }
            task = next(t for t in ledger["tasks"] if t["task_id"] == value["task_id"])
            from .delivery_records import scope_hash

            if value["scope_sha256"] != scope_hash(
                task,
                ledger.get("task_details", {}).get(task["task_id"], {}).get("body", ""),
            ):
                return {
                    **value,
                    "executed": False,
                    "next_action": "scope-changed-review-required",
                }
            if not execute:
                return {
                    **value,
                    "executed": False,
                    "next_action": "dispatch-approved-standby",
                }
            if not automatic_available(data_root, project_id, value["target_model"]):
                return {
                    **value,
                    "executed": False,
                    "next_action": "refresh-standby-quota",
                }
            from .worker_dispatch import dispatch

            launched = dispatch(
                root,
                data_root=data_root,
                project_id=project_id,
                task_id=value["task_id"],
                model=value["target_model"],
                background=True,
                handoff={
                    "previous_job": value["primary_job"],
                    "reason": result.get("failure_class"),
                    "preserve_progress": True,
                },
            )
            value["status"] = "secondary-running"
            value["secondary_job"] = launched["job_id"]
            _atomic_write(path, doc)
            return {**value, "executed": True}
    return return_ready(root, data_root, project_id, handoff_id, execute=execute)


def refresh(
    data_root,
    project_id,
    model,
    *,
    executable="codex",
    limits=None,
    limit_id="codex",
    provider="codex",
):
    if provider != "codex":
        raise ValueError(
            "native Claude quota polling is not configured; query usage on the owning machine/profile and record only the observed counters with --provider claude; never substitute a Codex account or transplant private state"
        )
    adapters_file = Path(data_root or default_data_root()) / "executor-adapters.json"
    configured = json.loads(adapters_file.read_text()) if adapters_file.exists() else {}
    adapter = configured.get(model)
    if adapter:
        namespace = adapter.get("profile_provider", adapter["provider"])
        if namespace != provider:
            raise ValueError(
                "adapter belongs to another native profile family; query its owning client rather than a different account"
            )
        from .runtime_origin import require_local

        if not adapter.get("execution_origin"):
            raise ValueError(
                "adapter has no host/profile binding; reconfigure on its owning host"
            )
        require_local(adapter["execution_origin"], provider)
    if limits is None:
        from .codex_account_probe import read_limits

        limits = read_limits(executable)
    bucket = (limits.get("rateLimitsByLimitId") or {}).get(limit_id)
    if bucket is None:
        single = limits.get("rateLimits", {})
        bucket = single if single.get("limitId", "codex") == limit_id else {}
    windows = [
        value
        for value in (bucket.get("primary"), bucket.get("secondary"))
        if isinstance(value, dict)
    ]
    windows = [
        w
        for w in windows
        if type(w.get("usedPercent")) in (int, float)
        and type(w.get("resetsAt")) in (int, float)
        and w["resetsAt"] > datetime.now(UTC).timestamp()
    ]
    if not windows:
        raise ValueError(
            "native account did not return a usable subscription quota window"
        )
    window = max(windows, key=lambda w: w["usedPercent"])
    return record(
        data_root,
        project_id,
        model,
        used_percent=window["usedPercent"],
        resets_at=datetime.fromtimestamp(window["resetsAt"], UTC).isoformat(),
        source="codex-native:account/rateLimits/read",
        official_subscription=True,
        origin=limits.get("collection_origin"),
    )
