"""Incremental local usage metadata import; never persist message bodies."""

import hashlib
import json
import os
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    resolve_project,
)


def path_for(root, data_root=None):
    project = resolve_project(root, data_root)
    root = Path(project["control_root"] if project else root).resolve()
    return (
        Path(data_root or default_data_root())
        / "provider-usage"
        / (hashlib.sha256(str(root).encode()).hexdigest() + ".json")
    )


def _load(path):
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"sources": {}, "records": {}, "unknown_rows": 0}
    )


def _counts(usage, provider):
    keys = (
        {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
            "cache_creation_input_tokens": "cache_creation_tokens",
        }
        if provider == "claude"
        else {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cached_input_tokens": "cache_read_tokens",
            "reasoning_output_tokens": "reasoning_output_tokens",
        }
    )
    if not isinstance(usage, dict) or not any(
        k in usage for k in ("input_tokens", "output_tokens")
    ):
        return None
    result = {}
    for src, dst in keys.items():
        value = usage.get(src, 0)
        if type(value) is not int or value < 0:
            return None
        result[dst] = value
    return result


def ingest(
    root,
    *,
    transcript,
    provider,
    session_id,
    task_id=None,
    data_root=None,
    max_bytes=4194304,
):
    if (
        provider not in {"claude", "codex"}
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise ValueError("provider and session_id are required")
    if not 1024 <= max_bytes <= 16777216:
        raise ValueError("max_bytes must be 1024–16777216")
    source = Path(transcript).resolve()
    path = path_for(root, data_root)
    session = hashlib.sha256(session_id.encode()).hexdigest()
    source_key = hashlib.sha256((str(source) + provider + session).encode()).hexdigest()
    processed = 0
    accepted = 0
    from .runtime_origin import current

    origin = current(provider)
    source_key = hashlib.sha256(
        (source_key + json.dumps(origin, sort_keys=True)).encode()
    ).hexdigest()
    with _locked(path):
        doc = _load(path)
        with source.open("rb") as stream:
            import os

            stat = os.fstat(stream.fileno())
            identity = [stat.st_dev, stat.st_ino]
            previous = doc["sources"].get(source_key, {})
            offset = (
                previous.get("offset", 0) if previous.get("identity") == identity else 0
            )
            if offset > stat.st_size:
                offset = 0
            stream.seek(offset)
            while processed < max_bytes:
                before = stream.tell()
                line = stream.readline(min(2097153, max_bytes - processed + 1))
                if not line:
                    break
                if not line.endswith(b"\n"):
                    stream.seek(before)
                    break
                processed += len(line)
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    doc["unknown_rows"] += 1
                    continue
                record_id = None
                counts = None
                model = None
                usage_shaped = False
                if isinstance(row, dict) and provider == "claude":
                    msg = row.get("message", {})
                    usage_shaped = isinstance(msg, dict) and "usage" in msg
                    if (
                        isinstance(msg, dict)
                        and msg.get("id")
                        and isinstance(msg["id"], str)
                    ):
                        counts = _counts(msg.get("usage"), provider)
                        record_id = msg["id"]
                        model = msg.get("model")
                elif isinstance(row, dict) and provider == "codex":
                    payload = row.get("payload", {})
                    usage_shaped = (
                        isinstance(payload, dict)
                        and payload.get("type") == "token_count"
                    )
                    if (
                        isinstance(payload, dict)
                        and payload.get("type") == "token_count"
                    ):
                        info = payload.get("info") or {}
                        if isinstance(info, dict):
                            counts = _counts(info.get("total_token_usage"), provider)
                            capacity = info.get("model_context_window")
                            latest_input = (info.get("last_token_usage") or {}).get(
                                "input_tokens"
                            )
                            if (
                                type(capacity) is int
                                and capacity > 0
                                and type(latest_input) is int
                                and 0 <= latest_input <= capacity
                            ):
                                from .runtime_origin import key as origin_key

                                doc.setdefault("context_windows", {})[
                                    origin_key(origin) + ":" + session
                                ] = {
                                    "collection_origin": origin,
                                    "session": session,
                                    "capacity_tokens": capacity,
                                    "last_request_input_tokens": latest_input,
                                    "last_request_input_fraction": round(
                                        latest_input / capacity, 6
                                    ),
                                    "source": "host token_count model_context_window + last_token_usage",
                                    "meaning": "last measured request input share; not real-time occupancy or useful-work efficiency",
                                }
                            record_id = "cumulative-session"
                            model = payload.get("model")
                if counts is None:
                    if usage_shaped:
                        doc["unknown_rows"] += 1
                    continue
                key = hashlib.sha256(
                    (provider + session + record_id).encode()
                ).hexdigest()
                prior = doc["records"].get(key, {})
                if prior.get("collection_origin") not in (None, origin):
                    key = hashlib.sha256(
                        (key + json.dumps(origin, sort_keys=True)).encode()
                    ).hexdigest()
                old = doc["records"].get(key, {})
                old_counts = old.get("tokens", {})
                regressed = any(v < old_counts.get(k, 0) for k, v in counts.items())
                task_ids = set(old.get("task_ids", []))
                task_ids.update(t for t in (old.get("task_id"), task_id) if t)
                ambiguous = (
                    old.get("task_attribution_ambiguous", False) or len(task_ids) > 1
                )
                if old_counts and task_id and not old.get("task_id"):
                    ambiguous = True
                doc["records"][key] = {
                    "provider": provider,
                    "session": session,
                    "task_id": None if ambiguous else old.get("task_id") or task_id,
                    "task_ids": sorted(task_ids),
                    "task_attribution_ambiguous": ambiguous,
                    "model": model if isinstance(model, str) else None,
                    "collection_origin": origin,
                    "collection_kind": "local-file-usage-metadata; not account authentication or quota",
                    "tokens": {
                        k: max(v, old_counts.get(k, 0)) for k, v in counts.items()
                    },
                    "counter_regressed": regressed
                    or old.get("counter_regressed", False),
                    "semantics": "cumulative-session"
                    if provider == "codex"
                    else "message-usage",
                }
                accepted += 1
            doc["sources"][source_key] = {"identity": identity, "offset": stream.tell()}
            remaining = max(0, stat.st_size - stream.tell())
        _atomic_write(path, doc)
    _observe_bindings(root, data_root)
    return {
        "accepted_usage_rows": accepted,
        "read_bytes": processed,
        "remaining_bytes": remaining,
        "provider_usage_available": bool(doc["records"]),
        "transcript_body_saved": False,
    }


def report(root, data_root=None, *, include_bindings=True):
    doc = _load(path_for(root, data_root))
    records = list(doc["records"].values())
    snapshots = list(doc.get("snapshots", {}).values())
    known = {
        (r["provider"], r["session"])
        for r in [*records, *snapshots]
        if r.get("collection_origin")
    }
    unscoped = [
        r
        for r in [*records, *snapshots]
        if not r.get("collection_origin") and (r["provider"], r["session"]) in known
    ]
    records = [
        r
        for r in records
        if r.get("collection_origin") or (r["provider"], r["session"]) not in known
    ]
    snapshots = [
        r
        for r in snapshots
        if r.get("collection_origin") or (r["provider"], r["session"]) not in known
    ]

    def total(rows):
        result = {}
        for row in rows:
            for key, value in row["tokens"].items():
                result[key] = result.get(key, 0) + value
        return result

    for snapshot in snapshots:
        subset = [
            r
            for r in records
            if r["provider"] == snapshot["provider"]
            and r["session"] == snapshot["session"]
            and r.get("collection_origin") == snapshot.get("collection_origin")
        ]
        counts = total(subset)
        records = [r for r in records if r not in subset]
        combined = [snapshot, *subset]
        tasks = {
            t for r in combined for t in (*r.get("task_ids", []), r.get("task_id")) if t
        }
        ambiguous = (
            any(r.get("task_attribution_ambiguous") for r in combined) or len(tasks) > 1
        )
        if tasks and any(not r.get("task_id") and r.get("tokens") for r in combined):
            ambiguous = True
        records.append(
            {
                **snapshot,
                "task_id": next(iter(tasks))
                if len(tasks) == 1 and not ambiguous
                else None,
                "task_ids": sorted(tasks),
                "task_attribution_ambiguous": ambiguous,
                "tokens": {
                    k: max(v, counts.get(k, 0)) for k, v in snapshot["tokens"].items()
                },
            }
        )
    groups = {}
    for provider in ("claude", "codex"):
        rows = [r for r in records if r["provider"] == provider]
        groups[provider] = {
            "records": len(rows),
            "tokens": total(rows),
            "input_semantics": "uncached input; cache read/create are separate"
            if provider == "claude"
            else "input includes cached input; reasoning is part of output",
        }
    project = resolve_project(root, data_root)
    from .collaboration_registry import list_tasks

    completed = (
        [
            t["task_id"]
            for t in list_tasks(data_root, project_id=project["project_id"])
            if t["status"] in {"completed", "merged"}
        ]
        if project
        else []
    )
    attributed = {
        t: total([r for r in records if r.get("task_id") == t]) for t in completed
    }
    observed = {t: counts for t, counts in attributed.items() if counts}
    result = {
        "provider_usage_available": bool(records),
        "by_provider": groups,
        "by_origin": {
            key: total(
                [
                    r
                    for r in records
                    if json.dumps(r.get("collection_origin"), sort_keys=True) == key
                ]
            )
            for key in sorted(
                {
                    json.dumps(r.get("collection_origin"), sort_keys=True)
                    for r in records
                }
            )
        },
        "by_session": {
            s: total([r for r in records if r["session"] == s])
            for s in sorted({r["session"] for r in records})
        },
        "by_task": {
            t: total([r for r in records if r["task_id"] == t])
            for t in sorted({r["task_id"] for r in records if r["task_id"]})
        },
        "counter_regressions": sum(
            bool(r.get("counter_regressed", False)) for r in records
        ),
        "ambiguous_task_records": sum(
            bool(r.get("task_attribution_ambiguous")) for r in records
        ),
        "unknown_rows": doc["unknown_rows"],
        "superseded_unscoped_observations": len(unscoped),
        "coverage": "only explicitly ingested files and recognized usage schemas; not whole-account billing",
        "cost_available": False,
        "completed_task_usage": {
            "completed_tasks": len(completed),
            "tasks_with_usage": len(observed),
            "by_task": observed,
            "attribution": "explicit host task ids only; not inferred from overlapping wall time",
        },
        "compaction_windows": doc.get("windows", [])[-20:],
        "window_utilization": doc.get("context_windows") or None,
        "window_utilization_reason": "Only host-reported capacity and last-request input are measured; unavailable providers and useful-work efficiency remain unknown",
    }
    identities = {(r["provider"], r["session"], json.dumps(r.get("collection_origin"), sort_keys=True)) for r in records}
    result["session_observations"] = [
        {"provider": provider, "session": session, "collection_origin": json.loads(origin),
         "tokens": total([r for r in records if r["provider"] == provider and r["session"] == session and json.dumps(r.get("collection_origin"), sort_keys=True) == origin])}
        for provider, session, origin in sorted(identities)
    ]
    if include_bindings:
        project = resolve_project(root, data_root)
        if project:
            from .project_context import binding_usage

            try:
                result["session_binding_usage"] = binding_usage(data_root, project["project_id"], result["session_observations"])
            except (OSError, ValueError, KeyError):
                result["session_binding_usage"] = {"status": "unavailable", "cost_available": False}
    return result


def _observe_bindings(root, data_root):
    project = resolve_project(root, data_root)
    if project:
        from .project_context import establish_pending

        try:
            establish_pending(data_root, project["project_id"], report(root, data_root, include_bindings=False)["session_observations"])
        except (OSError, ValueError, KeyError):
            pass


def export_snapshot(root, provider, session_id, data_root=None):
    from .runtime_origin import current

    origin = current(provider)
    doc = _load(path_for(root, data_root))
    session = hashlib.sha256(session_id.encode()).hexdigest()
    rows = [
        r
        for r in doc["records"].values()
        if r["provider"] == provider
        and r["session"] == session
        and r.get("collection_origin") in (None, origin)
    ]
    if not rows:
        return None
    from datetime import UTC, datetime

    tokens = {}
    for row in rows:
        for key, value in row["tokens"].items():
            tokens[key] = tokens.get(key, 0) + value
    tasks = {r["task_id"] for r in rows if r.get("task_id")}
    return {
        "provider": provider,
        "collection_origin": origin
        if all(r.get("collection_origin") == origin for r in rows)
        else None,
        "collection_kind": "normalized-usage-summary; never reusable account/session state",
        "session": session,
        "tokens": tokens,
        "task_id": next(iter(tasks))
        if len(tasks) == 1
        and not any(r.get("task_attribution_ambiguous") for r in rows)
        else None,
        "task_ids": sorted(
            {t for r in rows for t in r.get("task_ids", [r.get("task_id")]) if t}
        ),
        "task_attribution_ambiguous": len(tasks) > 1
        or any(r.get("task_attribution_ambiguous") for r in rows),
        "counter_regressed": any(r.get("counter_regressed") for r in rows),
        "collected_at": datetime.now(UTC).isoformat(),
    }


def import_snapshot(root, snapshot, data_root=None):
    import re

    if (
        not isinstance(snapshot, dict)
        or snapshot.get("provider") not in {"claude", "codex"}
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot.get("session", ""))
    ):
        raise ValueError("invalid usage snapshot identity")
    tokens = snapshot.get("tokens")
    origin = snapshot.get("collection_origin")
    if origin is not None:
        from .runtime_origin import validate

        origin = validate(origin)
        if origin["provider"] != snapshot["provider"]:
            raise ValueError("usage origin provider does not match its counters")
    allowed = {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_output_tokens",
    }
    if (
        not isinstance(tokens, dict)
        or not tokens
        or not set(tokens) <= allowed
        or any(type(v) is not int or v < 0 for v in tokens.values())
    ):
        raise ValueError("invalid normalized usage counters")
    path = path_for(root, data_root)
    with _locked(path):
        doc = _load(path)
        key = snapshot["provider"] + ":" + snapshot["session"]
        if origin is not None:
            from .runtime_origin import key as origin_key

            key += ":" + origin_key(origin)
        old = doc.setdefault("snapshots", {}).get(key, {})
        doc["snapshots"][key] = {
            k: snapshot.get(k)
            for k in (
                "provider",
                "session",
                "task_id",
                "counter_regressed",
                "collected_at",
                "collection_origin",
                "collection_kind",
            )
        }
        doc["snapshots"][key]["tokens"] = {
            k: max(v, old.get("tokens", {}).get(k, 0)) for k, v in tokens.items()
        }
        tasks = set(old.get("task_ids", [])) | set(snapshot.get("task_ids", []))
        tasks.update(t for t in (old.get("task_id"), snapshot.get("task_id")) if t)
        ambiguous = bool(
            old.get("task_attribution_ambiguous")
            or snapshot.get("task_attribution_ambiguous")
            or len(tasks) > 1
            or (
                old.get("tokens") and not old.get("task_id") and snapshot.get("task_id")
            )
        )
        doc["snapshots"][key].update(
            task_ids=sorted(tasks),
            task_attribution_ambiguous=ambiguous,
            task_id=None if ambiguous else next(iter(tasks), None),
        )
        _atomic_write(path, doc)
    _observe_bindings(root, data_root)


def capture_hook(payload, provider, *, data_root=None):
    if payload.get("hook_event_name") not in {
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "Stop",
    }:
        return None
    transcript = payload.get("transcript_path")
    session = payload.get("session_id")
    root = payload.get("cwd")
    if (
        not isinstance(transcript, str)
        or not isinstance(session, str)
        or not isinstance(root, str)
    ):
        return None
    try:
        ingest(
            root,
            transcript=transcript,
            provider=provider,
            session_id=session,
            task_id=payload.get("task_id") or os.environ.get("CONTINUITY_TASK_ID"),
            data_root=data_root,
        )
        return export_snapshot(root, provider, session, data_root)
    except Exception:
        return None


def capture_compaction(root, session_id, generation, data_root=None):
    from .collaboration_registry import list_changes
    from datetime import UTC, datetime

    session = hashlib.sha256(session_id.encode()).hexdigest()
    path = path_for(root, data_root)
    project = resolve_project(root, data_root)
    current = report(root, data_root).get("by_session", {}).get(session, {})
    with _locked(path):
        doc = _load(path)
        windows = doc.setdefault("windows", [])
        if any(w["generation"] == generation for w in windows):
            return
        previous = next((w for w in reversed(windows) if w["session"] == session), None)
        cursor = previous["cursor"] if previous else 0
        completed = set()
        if project:
            while True:
                page = list_changes(
                    data_root,
                    project_id=project["project_id"],
                    since=cursor,
                    max_results=500,
                )
                completed.update(
                    r["task_id"]
                    for r in page["changes"]
                    if r["event"] == "task.updated"
                    and r.get("status") in {"ready-for-review", "completed", "merged"}
                )
                cursor = page["next_cursor"]
                if not page["truncated"]:
                    break
        delta = (
            {
                k: max(0, v - previous.get("tokens", {}).get(k, 0))
                for k, v in current.items()
            }
            if previous
            else None
        )
        windows.append(
            {
                "session": session,
                "generation": generation,
                "at": datetime.now(UTC).isoformat(),
                "tokens": current,
                "cursor": cursor,
                "tokens_since_previous": delta,
                "completed_tasks_since_previous": len(completed) if previous else None,
                "scope": "project activity during session interval; not causal cost allocation",
                "tokens_per_completed_task": {
                    k: round(v / len(completed), 2) for k, v in (delta or {}).items()
                }
                if previous and completed
                else None,
            }
        )
        doc["windows"] = windows[-100:]
        _atomic_write(path, doc)


def command(args):
    value = ingest(
        args.root,
        transcript=args.transcript,
        provider=args.provider,
        session_id=args.session_id,
        task_id=args.task_id,
        data_root=args.data_root,
        max_bytes=args.max_bytes,
    )
    print(json.dumps(value, ensure_ascii=False))
    return 0


def register_cli(commands):
    p = commands.add_parser(
        "ingest-usage",
        help="Incrementally import local host usage metadata; never save transcript text",
    )
    for field in ("root", "data-root", "task-id"):
        p.add_argument("--" + field, default="." if field == "root" else None)
    p.add_argument("--provider", choices=("claude", "codex"), required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--max-bytes", type=int, default=4194304)
    p.set_defaults(handler=command)
