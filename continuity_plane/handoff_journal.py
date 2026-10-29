"""Actual save/restore generation receipts, separate from legacy plugin-source pairing."""

import hashlib
import json
import os
from pathlib import Path
from .collaboration_registry import _atomic_write, _locked, _now, default_data_root

DEFAULT_HOOK_BUDGET_BYTES = 6144


def record(root, session, generation, phase, *, data_root=None, kind="native-event"):
    if (
        phase not in {"saved", "restored"}
        or not isinstance(generation, str)
        or not generation
    ):
        return
    root_key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
    session_key = hashlib.sha256(session.encode()).hexdigest()
    identity = hashlib.sha256((session_key + generation).encode()).hexdigest()
    path = (
        Path(data_root or default_data_root())
        / "handoff-events"
        / root_key
        / (identity + ".json")
    )
    with _locked(path):
        row = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "session": session_key,
                "generation": generation,
                "kind": kind
                if kind in {"native-event", "controlled-test"}
                else "native-event",
            }
        )
        row[phase] = _now()
        _atomic_write(path, row)


def report(root, data_root=None):
    root_key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
    folder = Path(data_root or default_data_root()) / "handoff-events" / root_key
    rows = []
    for path in sorted(
        folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:200]:
        try:
            rows.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass

    def counts(kind):
        selected = [r for r in rows if r.get("kind") == kind]
        paired = [r for r in selected if r.get("saved") and r.get("restored")]
        return {
            "saved": sum(bool(r.get("saved")) for r in selected),
            "restored": sum(bool(r.get("restored")) for r in selected),
            "paired_compactions": len(paired),
            "paired_sessions": len({r["session"] for r in paired}),
        }

    return {
        "native_events": counts("native-event"),
        "controlled_tests": counts("controlled-test"),
        "window": "latest 200 generation receipts",
        "meaning": "save/restore protocol receipts; delivery bytes are measured separately",
    }


def hook_budget_bytes():
    """Per-session byte allowance for notices and planning hints."""
    configured = os.environ.get("CONTINUITY_HOOK_BUDGET_BYTES")
    if configured in {None, ""}:
        return DEFAULT_HOOK_BUDGET_BYTES
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_HOOK_BUDGET_BYTES
    return max(0, min(65536, value))


def budget_path(root, session, data_root=None):
    root_key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:32]
    session_key = hashlib.sha256(session.encode()).hexdigest()[:32]
    return (
        Path(data_root or default_data_root())
        / "hook-budgets"
        / root_key
        / (session_key + ".json")
    )


def reserve(root, session, text, *, data_root=None, kind="hint"):
    """Reserve delivery bytes once; a rejected hint is not re-sent later."""
    if not isinstance(text, str) or not text or not isinstance(session, str) or not session:
        return False
    size = len(text.encode("utf-8"))
    limit = hook_budget_bytes()
    if limit <= 0 or size > limit:
        return False
    path = budget_path(root, session, data_root)
    with _locked(path):
        try:
            counter = json.loads(path.read_text())
        except (OSError, ValueError):
            counter = {}
        if not isinstance(counter, dict):
            counter = {}
        used = counter.get("used")
        if type(used) is not int or used < 0:
            used = 0
        if used + size > limit:
            counter["limit"] = limit
            counter["used"] = used
            dropped = counter.get("dropped")
            if type(dropped) is not int or dropped < 0:
                dropped = 0
            counter["dropped"] = dropped + size
            _atomic_write(path, counter)
            return False
        kinds = counter.get("kinds")
        if not isinstance(kinds, dict):
            kinds = {}
        previous_kind = kinds.get(kind)
        if type(previous_kind) is not int or previous_kind < 0:
            previous_kind = 0
        kinds[kind] = previous_kind + size
        _atomic_write(
            path,
            {"limit": limit, "used": used + size, "kinds": kinds},
        )
    return True


def budget_remaining(root, session, data_root=None):
    path = budget_path(root, session, data_root)
    try:
        counter = json.loads(path.read_text())
        used = int(counter.get("used", 0))
    except (OSError, ValueError, TypeError):
        used = 0
    return max(0, hook_budget_bytes() - used)
