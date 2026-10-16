"""Claude hook observations, not native transcript import or process liveness."""

import hashlib
import json
from pathlib import Path

from .collaboration_registry import (
    _atomic_write,
    _locked,
    _now,
    _record_change,
    default_data_root,
)
from .project_context import _path, _load


def observe(data_root, project_id, payload):
    from .runtime_origin import current

    event = payload.get("hook_event_name")
    if event not in {"SubagentStart", "SubagentStop"}:
        return None
    session, agent = payload.get("session_id"), payload.get("agent_id")
    if (
        not isinstance(session, str)
        or not session
        or not isinstance(agent, str)
        or not 1 <= len(agent) <= 256
    ):
        return {"status": "invalid-native-agent-metadata"}
    origin = payload.get("collection_origin") or current("claude")
    from .runtime_origin import validate

    origin = validate(origin)
    if origin["provider"] != "claude":
        raise ValueError(
            "native Claude subagent observation needs its Claude collection origin"
        )
    key = hashlib.sha256(
        json.dumps([origin, session, agent], sort_keys=True).encode()
    ).hexdigest()
    path = _path(data_root, project_id)
    flush(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        attempts = doc.setdefault("native_attempts", {})
        previous = attempts.get(key)
        if event == "SubagentStop" and previous is None:
            return {"status": "unmatched-native-stop", "attempt_created": False}
        running = event == "SubagentStart"
        if previous and previous["status"] == ("running" if running else "stopped"):
            return {
                "status": "duplicate-native-observation",
                "attempt_id": previous["attempt_id"],
            }
        generation = (previous or {}).get("generation", 0) + (1 if running else 0)
        value = {
            "attempt_id": "native-" + key[:24] + "-" + str(generation),
            "generation": generation,
            "status": "running" if running else "stopped",
            "agent_type": str(payload.get("agent_type") or "unknown")[:128],
            "session": hashlib.sha256(session.encode()).hexdigest(),
            "collection_origin": origin,
            "observed_at": _now(),
            "authority": "host-hook-observation; no PID, heartbeat, success or exit-code inference",
        }
        attempts[key] = value
        # Queue the exact event with the state before attempting ledger delivery.
        pending = doc.setdefault("native_pending_events", {})
        pending[value["attempt_id"] + ":" + event] = {
            "event": "attempt.started" if running else "attempt.finished",
            **value,
        }
        _atomic_write(path, doc)
    flush(data_root, project_id)
    return value


def flush(data_root, project_id):
    path = _path(data_root, project_id)
    if not path.exists():
        return
    with _locked(path):
        doc = _load(path)
        if not doc.get("native_pending_events"):
            return
        for key, row in list(doc.get("native_pending_events", {}).items()):
            try:
                _record_change(
                    Path(data_root or default_data_root()),
                    project_id,
                    row["event"],
                    None,
                    **{k: v for k, v in row.items() if k != "event"},
                    observed_native=True,
                    event_id="native:" + key,
                )
            except OSError:
                break
            del doc["native_pending_events"][key]
        _atomic_write(path, doc)
