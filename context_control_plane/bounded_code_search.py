"""Bounded current-worktree search for Agent-facing repository evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "context.bounded-code-search/v1alpha1"
MAX_QUERY_BYTES = 1024
MAX_EXCERPT_BYTES = 512
MAX_RESULTS = 200
MAX_OUTPUT_BYTES = 64 * 1024
_FIELDS = {
    "schema_version",
    "repository_revision",
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
_MATCH_FIELDS = {
    "path",
    "line",
    "excerpt",
    "line_sha256",
    "file_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(document: dict[str, Any]) -> str:
    body = copy.deepcopy(document)
    body.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )


def _excerpt(line: bytes) -> str | None:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_EXCERPT_BYTES:
        return text
    clipped = encoded[:MAX_EXCERPT_BYTES]
    while clipped:
        try:
            return clipped.decode("utf-8") + "…"
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "…"


def _seal(receipt: dict[str, Any]) -> int:
    for _ in range(8):
        receipt["receipt_sha256"] = _digest(receipt)
        size = len(_canonical(receipt))
        if receipt["returned_bytes"] == size:
            return size
        receipt["returned_bytes"] = size
    raise ValueError("receipt size did not converge")


def bounded_git_search(
    root: str | Path,
    *,
    query: str,
    max_results: int = 40,
    max_output_bytes: int = 8192,
) -> dict[str, Any]:
    """Search tracked current-worktree text and cap the complete JSON receipt."""
    root = Path(root).resolve()
    if (
        not isinstance(query, str)
        or not query
        or "\x00" in query
        or "\n" in query
        or "\r" in query
        or len(query.encode("utf-8")) > MAX_QUERY_BYTES
    ):
        raise ValueError("query is invalid")
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
        raise ValueError("max_results is invalid")
    if (
        type(max_output_bytes) is not int
        or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise ValueError("max_output_bytes is invalid")
    revision_result = _git(root, "rev-parse", "HEAD")
    if revision_result.returncode != 0:
        raise ValueError("repository revision is unavailable")
    revision = revision_result.stdout.decode("ascii").strip()
    search = _git(root, "grep", "-n", "-I", "-F", "-z", "--", query)
    if search.returncode not in {0, 1}:
        raise ValueError("repository search failed")
    candidates: list[dict[str, Any]] = []
    for raw in search.stdout.splitlines():
        parts = raw.split(b"\x00", 2)
        if len(parts) != 3:
            continue
        try:
            relative_text = parts[0].decode("utf-8")
            line_number = int(parts[1].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        relative = Path(relative_text)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
            payload = path.read_bytes()
        except (OSError, ValueError):
            continue
        lines = payload.splitlines()
        if line_number < 1 or line_number > len(lines):
            continue
        line = lines[line_number - 1]
        excerpt = _excerpt(line)
        if excerpt is None:
            continue
        candidates.append(
            {
                "path": relative.as_posix(),
                "line": line_number,
                "excerpt": excerpt,
                "line_sha256": hashlib.sha256(line).hexdigest(),
                "file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    candidates.sort(key=lambda item: (item["path"], item["line"]))
    selected = candidates[:max_results]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "repository_revision": revision,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "matches": selected,
        "match_count": len(selected),
        "matched_before_limit": len(candidates),
        "truncated": len(candidates) > len(selected),
        "max_results": max_results,
        "max_output_bytes": max_output_bytes,
        "returned_bytes": 0,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    while _seal(receipt) > max_output_bytes and receipt["matches"]:
        receipt["matches"].pop()
        receipt["match_count"] = len(receipt["matches"])
        receipt["truncated"] = True
    if _seal(receipt) > max_output_bytes:
        raise ValueError("max_output_bytes cannot contain the receipt metadata")
    validate_bounded_code_search_receipt(receipt, root=root)
    return receipt


def validate_bounded_code_search_receipt(
    value: Any, *, root: str | Path | None = None
) -> None:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("search receipt fields are invalid")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("search receipt schema is unsupported")
    for field in ("repository_revision", "query_sha256", "receipt_sha256"):
        item = value[field]
        expected_length = 40 if field == "repository_revision" else 64
        if not isinstance(item, str) or len(item) != expected_length:
            raise ValueError(f"{field} is invalid")
    if value["receipt_sha256"] != _digest(value):
        raise ValueError("search receipt digest mismatch")
    if value["returned_bytes"] != len(_canonical(value)):
        raise ValueError("returned_bytes is inaccurate")
    if value["returned_bytes"] > value["max_output_bytes"]:
        raise ValueError("search receipt exceeds its output budget")
    matches = value["matches"]
    if not isinstance(matches, list) or value["match_count"] != len(matches):
        raise ValueError("search matches are invalid")
    if value["matched_before_limit"] < len(matches):
        raise ValueError("matched_before_limit is inaccurate")
    if value["truncated"] is not (value["matched_before_limit"] > len(matches)):
        raise ValueError("truncated is inaccurate")
    if value["state_write_authority"] is not False or value["memory_authority"] is not False:
        raise ValueError("search receipt cannot carry authority")
    repository_root = Path(root).resolve() if root is not None else None
    for match in matches:
        if not isinstance(match, dict) or set(match) != _MATCH_FIELDS:
            raise ValueError("search match fields are invalid")
        if repository_root is None:
            continue
        path = (repository_root / match["path"]).resolve()
        try:
            path.relative_to(repository_root)
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ValueError("search match path is invalid") from exc
        if hashlib.sha256(payload).hexdigest() != match["file_sha256"]:
            raise ValueError("search match file hash drifted")
        lines = payload.splitlines()
        line_number = match["line"]
        if type(line_number) is not int or not 1 <= line_number <= len(lines):
            raise ValueError("search match line is invalid")
        if hashlib.sha256(lines[line_number - 1]).hexdigest() != match["line_sha256"]:
            raise ValueError("search match line hash drifted")


__all__ = ["bounded_git_search", "validate_bounded_code_search_receipt"]
