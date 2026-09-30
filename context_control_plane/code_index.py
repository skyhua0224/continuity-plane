"""Incremental, provider-neutral code index for bounded symbol lookup."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


INDEX_SCHEMA_VERSION = "context.code-index/v1alpha1"
LOOKUP_SCHEMA_VERSION = "context.code-index-lookup/v1alpha1"
MAX_QUERY_BYTES = 1024
MAX_FILES = 50_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 200
MAX_OUTPUT_BYTES = 64 * 1024

_INDEX_FIELDS = {
    "schema_version",
    "repository_revision",
    "index_revision",
    "cache_status",
    "tracked_files",
    "indexed_files",
    "reused_files",
    "rehashed_files",
    "removed_files",
    "cache_key",
    "max_files",
    "state_write_authority",
    "memory_authority",
    "receipt_sha256",
}
_LOOKUP_FIELDS = {
    "schema_version",
    "repository_revision",
    "index_revision",
    "cache_status",
    "query_sha256",
    "matches",
    "match_count",
    "matched_before_limit",
    "truncated",
    "max_results",
    "max_output_bytes",
    "returned_bytes",
    "state_write_authority",
    "memory_authority",
    "receipt_sha256",
}
_ENTRY_FIELDS = {"path", "size_bytes", "mtime_ns", "sha256", "language", "symbols"}
_SKIPPED_FIELDS = {"path", "size_bytes", "mtime_ns"}
_SYMBOL_FIELDS = {"kind", "name", "line"}
_MATCH_FIELDS = {"path", "line", "kind", "name", "file_sha256"}

_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}
_SYMBOL_PATTERNS = (
    ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
    ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)")),
    ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)")),
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: dict[str, Any], field: str) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop(field, None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )


def _repository_revision(root: Path) -> str:
    result = _git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise ValueError("repository revision is unavailable")
    try:
        revision = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("repository revision is invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository revision is invalid")
    return revision


def _tracked_paths(root: Path) -> list[Path]:
    result = _git(root, "ls-files", "-z")
    if result.returncode != 0:
        raise ValueError("tracked file list is unavailable")
    paths: list[Path] = []
    for value in result.stdout.split(b"\0"):
        if not value:
            continue
        try:
            relative = Path(value.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if relative.is_absolute() or ".." in relative.parts:
            continue
        paths.append(relative)
    return sorted(paths, key=lambda path: path.as_posix())


def default_code_index_path(root: str | Path) -> Path:
    """Return a user-local cache path that cannot write into the project root."""
    root = Path(root).resolve()
    configured = os.environ.get("CONTINUITY_CODE_INDEX_CACHE")
    if configured:
        return Path(configured).expanduser() / f"{_cache_key(root)}.json"
    cache_root = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "continuity-plane" / "code-index" / f"{_cache_key(root)}.json"


def _cache_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]


def _language(path: Path) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _symbols(payload: bytes) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in _SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match is not None:
                result.append({"kind": kind, "name": match.group(1), "line": line_number})
                break
    return result


def _read_cache(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != INDEX_SCHEMA_VERSION
        or value.get("cache_key") != _cache_key(root)
        or not isinstance(value.get("entries"), list)
        or not isinstance(value.get("skipped"), list)
    ):
        return None
    entries: dict[str, dict[str, Any]] = {}
    for entry in value["entries"]:
        if isinstance(entry, dict) and set(entry) == _ENTRY_FIELDS and isinstance(entry.get("path"), str):
            entries[entry["path"]] = entry
    value["entries"] = entries
    value["skipped"] = {
        item["path"]: item
        for item in value["skipped"]
        if isinstance(item, dict) and set(item) == _SKIPPED_FIELDS and isinstance(item.get("path"), str)
    }
    return value


def _write_cache(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".code-index-", suffix=".tmp", dir=path.parent)
    try:
        with open(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _entry_for(root: Path, relative: Path) -> dict[str, Any] | None:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_FILE_BYTES:
            return None
        payload = path.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in payload[:8192]:
        return None
    language = _language(path)
    return {
        "path": relative.as_posix(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "language": language or "text",
        "symbols": _symbols(payload),
    }


def _index_revision(
    repository_revision: str,
    entries: dict[str, dict[str, Any]],
    skipped: dict[str, dict[str, Any]],
) -> str:
    stable = {
        "repository_revision": repository_revision,
        "entries": [entries[path] for path in sorted(entries)],
        "skipped": [skipped[path] for path in sorted(skipped)],
    }
    return hashlib.sha256(_canonical(stable)).hexdigest()


def build_code_index(
    root: str | Path,
    *,
    cache_path: str | Path | None = None,
    max_files: int = 50_000,
) -> dict[str, Any]:
    """Incrementally index tracked text files into disposable user-local storage."""
    root = Path(root).resolve()
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError("max_files is invalid")
    cache = Path(cache_path).expanduser().resolve() if cache_path is not None else default_code_index_path(root).resolve()
    if root == cache or root in cache.parents:
        raise ValueError("code index cache must be outside the project root")
    revision = _repository_revision(root)
    tracked = _tracked_paths(root)
    if len(tracked) > max_files:
        raise ValueError("tracked file count exceeds max_files")
    previous = _read_cache(cache, root)
    old_entries = previous["entries"] if previous is not None else {}
    old_skipped = previous["skipped"] if previous is not None else {}
    entries: dict[str, dict[str, Any]] = {}
    skipped: dict[str, dict[str, Any]] = {}
    reused = 0
    rehashed = 0
    for relative in tracked:
        key = relative.as_posix()
        old = old_entries.get(key)
        path = root / relative
        try:
            stat = path.stat()
        except OSError:
            continue
        if (
            isinstance(old, dict)
            and old.get("size_bytes") == stat.st_size
            and old.get("mtime_ns") == stat.st_mtime_ns
        ):
            entries[key] = old
            reused += 1
            continue
        old_skip = old_skipped.get(key)
        if (
            isinstance(old_skip, dict)
            and old_skip.get("size_bytes") == stat.st_size
            and old_skip.get("mtime_ns") == stat.st_mtime_ns
        ):
            skipped[key] = old_skip
            reused += 1
            continue
        entry = _entry_for(root, relative)
        rehashed += 1
        if entry is not None:
            entries[key] = entry
        else:
            skipped[key] = {
                "path": key,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    removed = (
        len((set(old_entries) | set(old_skipped)) - (set(entries) | set(skipped)))
        if previous is not None
        else 0
    )
    status = "miss" if previous is None else "hit" if rehashed == 0 and removed == 0 else "partial"
    index_revision = _index_revision(revision, entries, skipped)
    _write_cache(
        cache,
        {
            "schema_version": INDEX_SCHEMA_VERSION,
            "cache_key": _cache_key(root),
            "repository_revision": revision,
            "index_revision": index_revision,
            "entries": [entries[key] for key in sorted(entries)],
            "skipped": [skipped[key] for key in sorted(skipped)],
        },
    )
    receipt = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "repository_revision": revision,
        "index_revision": index_revision,
        "cache_status": status,
        "tracked_files": len(tracked),
        "indexed_files": len(entries),
        "reused_files": reused,
        "rehashed_files": rehashed,
        "removed_files": removed,
        "cache_key": _cache_key(root),
        "max_files": max_files,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    validate_code_index_receipt(receipt)
    return receipt


def _query(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\0" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > MAX_QUERY_BYTES
    ):
        raise ValueError("query is invalid")
    return value.casefold()


def lookup_code_index(
    root: str | Path,
    *,
    query: str,
    cache_path: str | Path | None = None,
    max_results: int = 20,
    max_output_bytes: int = 8192,
) -> dict[str, Any]:
    """Return bounded symbol/path references without returning source bodies."""
    needle = _query(query)
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
        raise ValueError("max_results is invalid")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("max_output_bytes is invalid")
    index = build_code_index(root, cache_path=cache_path)
    cache = Path(cache_path).expanduser().resolve() if cache_path is not None else default_code_index_path(root).resolve()
    document = _read_cache(cache, Path(root).resolve())
    if document is None:
        raise ValueError("code index cache is unavailable")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for entry in document["entries"].values():
        path = entry["path"]
        path_score = 2 if needle in path.casefold() else None
        for symbol in entry["symbols"]:
            name = symbol["name"]
            score = 0 if name.casefold() == needle else 1 if needle in name.casefold() else path_score
            if score is not None:
                candidates.append(
                    (
                        score,
                        {
                            "path": path,
                            "line": symbol["line"],
                            "kind": symbol["kind"],
                            "name": name,
                            "file_sha256": entry["sha256"],
                        },
                    )
                )
        if path_score is not None and not entry["symbols"]:
            candidates.append(
                (path_score, {"path": path, "line": 1, "kind": "file", "name": path, "file_sha256": entry["sha256"]})
            )
    candidates.sort(key=lambda item: (item[0], item[1]["path"], item[1]["line"], item[1]["name"]))
    all_matches = [item[1] for item in candidates]
    receipt = {
        "schema_version": LOOKUP_SCHEMA_VERSION,
        "repository_revision": index["repository_revision"],
        "index_revision": index["index_revision"],
        "cache_status": index["cache_status"],
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "matches": all_matches[:max_results],
        "match_count": min(len(all_matches), max_results),
        "matched_before_limit": len(all_matches),
        "truncated": len(all_matches) > max_results,
        "max_results": max_results,
        "max_output_bytes": max_output_bytes,
        "returned_bytes": 0,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    for _ in range(8):
        receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
        size = len(_canonical(receipt))
        if receipt["returned_bytes"] == size:
            break
        receipt["returned_bytes"] = size
    while receipt["returned_bytes"] > max_output_bytes and receipt["matches"]:
        receipt["matches"].pop()
        receipt["match_count"] -= 1
        receipt["truncated"] = True
        receipt["returned_bytes"] = 0
        for _ in range(8):
            receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
            size = len(_canonical(receipt))
            if receipt["returned_bytes"] == size:
                break
            receipt["returned_bytes"] = size
    if receipt["returned_bytes"] > max_output_bytes:
        raise ValueError("max_output_bytes cannot contain the lookup receipt")
    validate_code_index_receipt(receipt, root=root)
    return receipt


def validate_code_index_receipt(value: Any, *, root: str | Path | None = None) -> None:
    if not isinstance(value, dict):
        raise ValueError("code index receipt is invalid")
    schema = value.get("schema_version")
    fields = _INDEX_FIELDS if schema == INDEX_SCHEMA_VERSION else _LOOKUP_FIELDS if schema == LOOKUP_SCHEMA_VERSION else None
    if fields is None or set(value) != fields:
        raise ValueError("code index receipt fields are invalid")
    if value["receipt_sha256"] != _digest(value, "receipt_sha256"):
        raise ValueError("code index receipt digest mismatch")
    for field in ("repository_revision", "index_revision", "receipt_sha256"):
        length = 40 if field == "repository_revision" else 64
        if not isinstance(value[field], str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value[field]):
            raise ValueError(f"{field} is invalid")
    if value["state_write_authority"] is not False or value["memory_authority"] is not False:
        raise ValueError("code index cannot carry authority")
    if schema == INDEX_SCHEMA_VERSION:
        if value["cache_status"] not in {"miss", "hit", "partial"}:
            raise ValueError("cache_status is invalid")
        for field in ("tracked_files", "indexed_files", "reused_files", "rehashed_files", "removed_files", "max_files"):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError(f"{field} is invalid")
        return
    if value["cache_status"] not in {"miss", "hit", "partial"}:
        raise ValueError("cache_status is invalid")
    matches = value["matches"]
    if not isinstance(matches, list) or value["match_count"] != len(matches):
        raise ValueError("lookup matches are invalid")
    if type(value["matched_before_limit"]) is not int or value["matched_before_limit"] < len(matches):
        raise ValueError("matched_before_limit is invalid")
    if value["truncated"] is not (value["matched_before_limit"] > len(matches)):
        raise ValueError("truncated is invalid")
    if value["returned_bytes"] != len(_canonical(value)) or value["returned_bytes"] > value["max_output_bytes"]:
        raise ValueError("lookup output budget is invalid")
    for match in matches:
        if not isinstance(match, dict) or set(match) != _MATCH_FIELDS:
            raise ValueError("lookup match fields are invalid")
        if type(match["line"]) is not int or match["line"] < 1:
            raise ValueError("lookup match line is invalid")
        for field in ("path", "kind", "name"):
            if not isinstance(match[field], str) or not match[field]:
                raise ValueError(f"lookup match {field} is invalid")
        if not isinstance(match["file_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", match["file_sha256"]):
            raise ValueError("lookup match file hash is invalid")
        if root is not None:
            path = (Path(root).resolve() / match["path"]).resolve()
            try:
                path.relative_to(Path(root).resolve())
                payload = path.read_bytes()
            except (OSError, ValueError) as exc:
                raise ValueError("lookup match path is invalid") from exc
            if hashlib.sha256(payload).hexdigest() != match["file_sha256"]:
                raise ValueError("lookup match file hash drifted")


__all__ = [
    "build_code_index",
    "default_code_index_path",
    "lookup_code_index",
    "validate_code_index_receipt",
]
