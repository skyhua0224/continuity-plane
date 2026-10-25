"""Local worker routing policy; explicit candidates only, never provider probing."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from .collaboration_registry import (
    _atomic_write,
    _locked,
    _now,
    _record_change,
    default_data_root,
    project_data_dir,
)
from .executor_routing import _model_alias


POLICY_SCHEMA = "context.worker-policy/v1alpha1"


def _path(data_root, project_id):
    return project_data_dir(
        Path(data_root or default_data_root()), project_id
    ) / "worker-policy.json"


def _profiles(data_root):
    path = Path(data_root or default_data_root()) / "executor-adapters.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load(data_root, project_id):
    path = _path(data_root, project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ValueError("worker policy is unreadable") from exc
    if not value:
        return {
            "schema_version": POLICY_SCHEMA,
            "fallbacks": {},
            "updated_at": None,
        }
    if value.get("schema_version") != POLICY_SCHEMA or not isinstance(
        value.get("fallbacks"), dict
    ):
        raise ValueError("worker policy schema is unsupported")
    return value


def set_fallback(data_root, project_id, *, model, candidates):
    if not isinstance(model, str) or not model:
        raise ValueError("fallback source model is required")
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 5:
        raise ValueError("fallback policy accepts at most five candidates")
    profiles = _profiles(data_root)
    if model not in profiles:
        raise ValueError("fallback source model is not a configured adapter")
    candidates = list(dict.fromkeys(candidates))
    if any(not isinstance(item, str) or item not in profiles for item in candidates):
        raise ValueError("every fallback candidate must be a configured adapter")
    if model in candidates:
        raise ValueError("fallback candidate must differ from the source model")
    root = Path(data_root or default_data_root())
    path = _path(root, project_id)
    with _locked(path):
        document = load(root, project_id)
        if candidates:
            document["fallbacks"][model] = candidates
        else:
            document["fallbacks"].pop(model, None)
        document["updated_at"] = _now()
        _atomic_write(path, document)
        _record_change(
            root,
            project_id,
            "worker.policy-updated",
            None,
            model=model,
            fallback_models=candidates,
        )
    return {"model": model, "fallback_models": candidates, **document}


def fallback_candidates(data_root, project_id, model):
    """Resolve exact or normalized model policy to currently configured adapters."""

    profiles = _profiles(data_root)
    document = load(data_root, project_id)
    exact = document.get("fallbacks", {}).get(model, [])
    if exact:
        selected = exact
    else:
        alias = _model_alias(model)
        selected = next(
            (
                candidates
                for source, candidates in document.get("fallbacks", {}).items()
                if _model_alias(source) == alias
            ),
            [],
        )
    return [candidate for candidate in selected if candidate in profiles and candidate != model][:5]


def set_completion_hook(data_root, project_id, *, argv=None, enabled=None):
    root = Path(data_root or default_data_root())
    path = _path(root, project_id)
    with _locked(path):
        document = load(root, project_id)
        previous = document.get("on_job_end") or {}
        if argv is not None:
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 32
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item.encode()) > 4096
                    for item in argv
                )
            ):
                raise ValueError(
                    "on-job-end argv must contain 1–32 nonempty bounded strings"
                )
            previous = {
                **previous,
                "argv": argv,
                "enabled": True,
                "recorded_at": _now(),
            }
        elif enabled is not None:
            previous = {**previous, "enabled": bool(enabled)}
        else:
            raise ValueError("completion hook update needs argv or enabled state")
        document["on_job_end"] = previous
        document["updated_at"] = _now()
        _atomic_write(path, document)
        _record_change(
            root,
            project_id,
            "worker.hook-policy-updated",
            None,
            enabled=bool(previous.get("enabled")),
            has_argv=bool(previous.get("argv")),
        )
    return previous


def launch_completion_hook(job, result):
    """Launch an explicitly configured local hook without waiting for it."""

    with _locked(Path(job["job_dir"]) / "completion-hook"):
        return _launch_completion_hook(job, result)


def _launch_completion_hook(job, result):

    document = load(job["data_root"], job["project_id"])
    config = document.get("on_job_end") or {}
    if not config.get("enabled") or not config.get("argv"):
        return None
    job_path = Path(job["job_dir"]) / "job.json"
    try:
        current = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not-launched", "reason": "job.json unreadable"}
    previous = current.get("completion_hook") or {}
    if previous.get("status") in {"launching", "launched", "failed"}:
        return previous
    current["completion_hook"] = {
        "status": "launching",
        "configured_at": config.get("recorded_at"),
    }
    _atomic_write(job_path, current)
    log = Path(job["job_dir"]) / "completion-hook.log"
    argv = [
        item.replace("{task_id}", str(job["task_id"])).replace(
            "{job_id}", str(job["job_id"])
        )
        for item in config["argv"]
    ]
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "PATHEXT"}
    }
    env.update(
        {
            "CONTINUITY_PROJECT_ID": job["project_id"],
            "CONTINUITY_TASK_ID": job["task_id"],
            "CONTINUITY_JOB_ID": job["job_id"],
            "CONTINUITY_JOB_STATUS": str(result.get("status")),
            "CONTINUITY_DATA_ROOT": str(Path(job["data_root"]).resolve()),
        }
    )
    try:
        with log.open("ab") as stream:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=os.name != "nt",
            )
        threading.Thread(target=process.wait, daemon=True).start()
        receipt = {
            "status": "launched",
            "pid": process.pid,
            "log": str(log),
            "blocking": False,
        }
        event = "worker.hook-launched"
    except (OSError, ValueError) as exc:
        receipt = {
            "status": "failed",
            "reason": str(exc)[:512],
            "log": str(log),
            "blocking": False,
        }
        event = "worker.hook-failed"
    current = json.loads(job_path.read_text(encoding="utf-8"))
    current["completion_hook"] = receipt
    _atomic_write(job_path, current)
    _record_change(
        Path(job["data_root"]),
        job["project_id"],
        event,
        job["task_id"],
        attempt_id=job["job_id"],
        **{key: value for key, value in receipt.items() if key != "status"},
    )
    return receipt


def fallback_command(args):
    data = Path(args.data_root or default_data_root())
    project_id = args.project_id
    if not project_id:
        from .collaboration_registry import resolve_project

        project = resolve_project(args.root, data)
        if project is None:
            raise ValueError("registered root or project-id is required")
        project_id = project["project_id"]
    if args.fallback_action == "show":
        value = load(data, project_id)
    else:
        if not args.clear and not args.candidate:
            raise ValueError("fallback-policy set requires --candidate or --clear")
        value = set_fallback(
            data,
            project_id,
            model=args.model,
            candidates=[] if args.clear else (args.candidate or []),
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


def completion_hook_command(args):
    data = Path(args.data_root or default_data_root())
    project_id = args.project_id
    if not project_id:
        from .collaboration_registry import resolve_project

        project = resolve_project(args.root, data)
        if project is None:
            raise ValueError("registered root or project-id is required")
        project_id = project["project_id"]
    if args.hook_action == "show":
        value = load(data, project_id).get("on_job_end") or {"enabled": False}
    elif args.hook_action == "clear":
        value = set_completion_hook(data, project_id, enabled=False)
    else:
        value = set_completion_hook(
            data,
            project_id,
            argv=json.loads(args.argv_json),
            enabled=True,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0
