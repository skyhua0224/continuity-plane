"""Actual save/restore generation receipts, separate from legacy plugin-source pairing."""

import hashlib
import json
from pathlib import Path
from .collaboration_registry import _atomic_write, _locked, _now, default_data_root


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
