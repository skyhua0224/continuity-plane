"""Explicit, bounded TSV interchange. Imports are candidates, never proof of reading."""

import csv
import io
import json
import os
import tempfile
from pathlib import Path

from .reference_library import candidate, location, _load
from .collaboration_registry import _locked, _atomic_write

FIELDS = ("reference_id", "title", "source", "kind", "version", "section", "platforms", "topics", "applicability")
ALIASES = {"key": "reference_id", "id": "reference_id", "url": "source", "type": "kind", "platform": "platforms", "topic": "topics", "notes": "applicability"}
LIMIT = 4 * 1024 * 1024


def _public(row):
    return {k: row[k] for k in FIELDS} | {"extra": row.get("tsv_extra", {})}


def import_tsv(root, *, file, data_root=None, scope="project"):
    with Path(file).open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("TSV exceeds 4 MiB")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter="\t", strict=True)
    try:
        headers = reader.fieldnames or []
    except csv.Error as exc:
        raise ValueError("Malformed TSV header") from exc
    names = [ALIASES.get(h, h) for h in headers]
    if len(names) != len(set(names)) or not {"reference_id", "title", "source"} <= set(names):
        raise ValueError("TSV requires unique reference_id/title/source headers (key/url aliases supported)")
    items = []
    seen = set()
    try:
        for number, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"TSV row {number}: column count differs from header")
            row = {ALIASES.get(k, k): v for k, v in row.items()}
            row["kind"] = row.get("kind") or "supplementary"
            for key in ("platforms", "topics"):
                value = row.get(key, "")
                row[key] = json.loads(value) if value.startswith("[") else [v.strip() for v in value.split(";") if v.strip()]
                if not isinstance(row[key], list) or not all(isinstance(v, str) for v in row[key]):
                    raise ValueError(f"TSV row {number}: {key} must be a text list")
            item = candidate(root, **{k: row[k] for k in FIELDS if k in row})
            extra = {k: v for k, v in row.items() if k not in FIELDS}
            from .vocabulary_memory import _SECRET_RE

            if any(_SECRET_RE.search(str(k) + "=" + str(v)) or _SECRET_RE.search(str(v)) for k, v in row.items()):
                raise ValueError(f"TSV row {number}: credential-like metadata is not allowed")
            if extra:
                item["tsv_extra"] = extra
            if item["reference_id"] in seen:
                raise ValueError(f"TSV row {number}: duplicate reference_id")
            seen.add(item["reference_id"])
            items.append(item)
            if len(items) > 1000:
                raise ValueError("TSV exceeds 1000 references")
    except csv.Error as exc:
        raise ValueError("Malformed TSV") from exc
    path = location(root, data_root, scope)
    with _locked(path):
        library = _load(path)
        existing = {i["reference_id"]: i for i in library["items"]}
        conflicts = [i["reference_id"] for i in items if i["reference_id"] in existing and _public(i) != _public(existing[i["reference_id"]])]
        if conflicts:
            raise ValueError("TSV conflicts; no records changed: " + ", ".join(conflicts[:10]))
        added = [i for i in items if i["reference_id"] not in existing]
        if len(existing) + len(added) > 1000:
            raise ValueError("reference library capacity reached (1000)")
        if added:
            library["items"].extend(added)
            library["revision"] += 1
            _atomic_write(path, library)
    return {"imported": len(added), "unchanged": len(items) - len(added), "authority": "declared-not-verified"}


def export_tsv(root, *, file, data_root=None, scope="project"):
    # Export precisely the selected scope, never implicitly include global data.
    path = location(root, data_root, scope)
    with _locked(path):
        items = _load(path)["items"]
    extra = sorted({k for item in items for k in item.get("tsv_extra", {})})
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=[*FIELDS, *extra], delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for item in sorted(items, key=lambda i: i["reference_id"]):
        row = {k: item[k] for k in FIELDS} | item.get("tsv_extra", {})
        for key in ("platforms", "topics"):
            row[key] = json.dumps(row[key], ensure_ascii=False)
        writer.writerow(row)
    data = out.getvalue()
    if len(data.encode()) > LIMIT:
        raise ValueError("TSV export exceeds 4 MiB")
    destination = Path(file)
    # Same-filesystem staging and atomic no-replace publication. A crash must
    # not leave a partially written source index or overwrite an existing one.
    descriptor, temporary = tempfile.mkstemp(prefix=".reference-export-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)
    return {"exported": len(items), "bytes": len(data.encode())}
