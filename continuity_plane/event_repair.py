"""Explicit non-destructive cursor repair with byte-identical backup."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from .collaboration_registry import _changes_path, _locked, default_data_root


def repair(data_root, *, project_id, apply=False):
    path = _changes_path(Path(data_root or default_data_root()), project_id)
    with _locked(path):
        original = path.read_bytes() if path.exists() else b""
        rows = []
        changed = 0
        cursor = 0
        mapping = []
        for number, line in enumerate(original.splitlines(), 1):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                raise ValueError(f"corrupt event at line {number}; no repair applied")
            if not isinstance(row, dict):
                raise ValueError(
                    f"non-object event at line {number}; no repair applied"
                )
            old = row.get("cursor")
            cursor = max(cursor + 1, old if type(old) is int and old > 0 else 0)
            if old != cursor:
                changed += 1
                mapping.append({"line": number, "old": old, "new": cursor})
            rows.append({**row, "cursor": cursor})
        result = {
            "changed_cursors": changed,
            "applied": False,
            "events": len(rows),
            "reset_consumer_cursors_required": bool(changed),
            "mapping": mapping[:100],
            "mapping_truncated": len(mapping) > 100,
        }
        if apply and changed:
            digest = hashlib.sha256(original).hexdigest()
            backup = path.with_name(path.name + ".before-repair-" + digest[:16])
            if backup.exists() and backup.read_bytes() != original:
                raise ValueError("repair backup conflict")
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            fd, tmp = tempfile.mkstemp(prefix=".repair-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            result.update(applied=True, backup=str(backup), backup_sha256=digest)
        return result


def command(args):
    print(
        json.dumps(
            repair(args.data_root, project_id=args.project_id, apply=args.apply),
            ensure_ascii=False,
        )
    )
    return 0
