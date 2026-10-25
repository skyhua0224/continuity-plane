"""Read-only model load from actual attempts; no allocation on recommendation reads."""

from datetime import UTC, datetime, timedelta
import json
from .collaboration_registry import (
    _atomic_write, _locked, default_data_root, iter_changes,
    latest_change_cursor, project_data_dir,
)


def _attempts(data_root, project_id):
    path = project_data_dir(data_root or default_data_root(), project_id) / "runtime-attempt-index.json"
    journal = path.with_name("changes.jsonl")
    with _locked(path):
        try:
            info = journal.stat()
            identity = [info.st_dev, info.st_ino]
        except FileNotFoundError:
            identity = None
        try:
            index = json.loads(path.read_text())
        except (OSError, ValueError):
            index = {}
        high_water = latest_change_cursor(data_root, project_id=project_id)
        if index.get("identity") != identity or index.get("cursor", 0) > high_water:
            index = {}
        cursor = index.get("cursor", 0)
        attempts = index.get("attempts", {})
        if cursor == high_water and index:
            return attempts.values()
        for row in iter_changes(data_root, project_id=project_id, since=cursor):
            cursor = max(cursor, row["cursor"])
            if row.get("event") not in {"attempt.started", "attempt.finished", "run.started", "run.finished"}:
                continue
            key = json.dumps([row.get("task_id"), row.get("attempt_id")])
            attempts.setdefault(key, {})[
                "start" if row["event"].endswith("started") else "finish"
            ] = {
                key: row[key] for key in (
                    "model", "assignee", "finished_at", "observed_at", "status",
                    "elapsed_seconds", "retryable", "available_at",
                ) if key in row
            }
        _atomic_write(path, {"identity": identity, "cursor": cursor, "attempts": attempts})
        return attempts.values()


def statistics(data_root, project_id, now=None):
    from .executor_routing import _model_alias

    now = now or datetime.now(UTC)
    stats = {}
    successes = {}
    throttles = {}
    for attempt in _attempts(data_root, project_id):
        start = attempt.get("start", {})
        finish = attempt.get("finish", {})
        model = _model_alias(
            start.get("model")
            or finish.get("model")
            or start.get("assignee")
            or finish.get("assignee")
            or ""
        )
        if model not in {"sol", "sonnet", "grok", "glm", "astra", "opus"}:
            continue
        item = stats.setdefault(
            model, dict(runs=0, elapsed_seconds=0, running=0, unavailable_until=None)
        )
        try:
            ended = datetime.fromisoformat(
                finish.get("finished_at") or finish.get("observed_at", "")
            )
            if ended.tzinfo and finish.get("status") == "completed":
                successes[model] = max(ended, successes.get(model, ended))
            elif ended.tzinfo and finish.get("retryable"):
                throttles[model] = max(ended, throttles.get(model, ended))
        except (ValueError, TypeError):
            pass
        item["runs"] += bool(start)
        item["running"] += bool(start and not finish)
        elapsed = finish.get("elapsed_seconds", 0)
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            item["elapsed_seconds"] += elapsed
        if finish.get("retryable"):
            try:
                until = (
                    datetime.fromisoformat(finish["available_at"])
                    if finish.get("available_at")
                    else datetime.fromisoformat(finish["observed_at"])
                    + timedelta(seconds=60)
                )
                if (
                    until.tzinfo
                    and until > now
                    and (
                        not item["unavailable_until"]
                        or until > datetime.fromisoformat(item["unavailable_until"])
                    )
                ):
                    item["unavailable_until"] = until.isoformat()
            except (KeyError, ValueError, TypeError):
                pass
    for model in stats:
        if (
            model in successes
            and model in throttles
            and successes[model] > throttles[model]
        ):
            stats[model]["unavailable_until"] = None
    return stats


def apply(data_root, project_id, preference):
    stats = statistics(data_root, project_id)
    from .subscription_quota import report

    quotas = report(data_root, project_id)["models"]
    import json
    from pathlib import Path
    from .collaboration_registry import default_data_root
    from .executor_routing import _model_alias

    adapters_file = Path(data_root or default_data_root()) / "executor-adapters.json"
    try:
        configured = (
            json.loads(adapters_file.read_text()) if adapters_file.exists() else {}
        )
    except (OSError, ValueError):
        configured = {}
    from .subscription_quota import automatic_available

    for model in {_model_alias(n) for n in configured}:
        matching = [(n, p) for n, p in configured.items() if _model_alias(n) == model]
        if matching and not any(
            automatic_available(data_root, project_id, n, profile=p)
            for n, p in matching
        ):
            entry = stats.setdefault(model, dict(runs=0, elapsed_seconds=0, running=0))
            entry["quota_unavailable"] = True
            entry["quota_status"] = quotas.get(model, {}).get("status", "unknown")
    if not stats:
        return preference
    result = dict(preference)
    pool = preference.get("preferred_pool", preference["candidates"])
    available = [
        m
        for m in preference["candidates"]
        if not stats.get(m, {}).get("unavailable_until")
        and not stats.get(m, {}).get("quota_unavailable")
    ]
    key = lambda m: (
        stats.get(m, {}).get("running", 0),
        stats.get(m, {}).get("runs", 0),
        stats.get(m, {}).get("elapsed_seconds", 0),
    )
    result["candidates"] = sorted([m for m in available if m in pool], key=key) + [
        m for m in available if m not in pool
    ]
    result["allocation"] = {
        "policy": "actual-running-count-duration; suitability-first",
        "counts_before": {m: stats.get(m, {}).get("runs", 0) for m in pool},
        "selected": result["candidates"][0] if result["candidates"] else None,
    }
    result["runtime_usage"] = stats
    result["subscription_quota"] = quotas
    return result
