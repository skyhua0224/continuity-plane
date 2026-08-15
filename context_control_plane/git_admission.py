"""Offline Git collaboration and staged-admission contracts.

Git retains a reviewable artifact boundary.  These checks intentionally do not
claim, write, or derive runtime Typed State; every receipt says so explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from context_control_plane.replay_fixture import (
    ReplayFixtureError,
    validate_replay_fixture,
)
from context_control_plane.sanitizer import sanitize_text

PACKET_SCHEMA_VERSION = "context.git-collaboration-packet/v1alpha1"
RECEIPT_SCHEMA_VERSION = "context.git-admission-receipt/v1alpha1"

_CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^(?:feat|fix|docs|test|refactor|perf|chore|build|ci)"
    r"(?:\([a-z][a-z0-9-]*\))?: [^\n.]{1,72}$"
)
_BRANCH_RE = re.compile(
    r"^(?:work|experiment|fix|docs)/[A-Z][0-9]+-[0-9]+/"
    r"[a-z0-9]+(?:-[a-z0-9]+)*(?:--[a-z0-9]{2,32})?$"
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab]"
    r"[0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_INTEGRITY_METADATA_RE = re.compile(
    r"(?:"
    r"artifact://sha256/"
    r"|(?:verification-run|evidence-bundle)://repository/"
    r"|\"?[A-Za-z][A-Za-z0-9_.-]*sha256\"?\s*[:=]\s*\"?"
    r"|\"?(?:sha256|master_digest|content_hash)\"?\s*[:=]\s*\"?"
    r"|\"?git_commit\"?\s*[:=]\s*\"?"
    r"|\"?(?:registry_revision|source_revision)\"?\s*[:=]\s*\"?"
    r")(?P<digest>[0-9a-f]{64}|[0-9a-f]{40})",
    re.IGNORECASE,
)
_PUBLIC_MEASUREMENT_METADATA_RE = re.compile(
    r"\"?paired_p_value\"?\s*[:=]\s*(?P<value>[0-9]\.[0-9]+e-[0-9]+)",
    re.IGNORECASE,
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:/(?:home|Users|private|var/folders)/|[A-Za-z]:\\Users\\)",
    re.IGNORECASE,
)
_ARTIFACT_REF_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_TRAILER_FIELDS = ("Task", "State-Revision", "Evidence", "Tests")
_PR_SECTIONS = (
    "Why:",
    "Scope:",
    "State and ownership:",
    "Evidence:",
    "Validation:",
    "Risk and rollback:",
    "Open questions:",
)
_PR_FIELDS = (
    "work_id",
    "parent_id",
    "scope",
    "return_point",
    "exit_criteria",
    "attempt_budget",
    "expiry",
    "promotion_target",
    "mainline_authority",
    "state_revision",
    "claim_ref",
    "path_owner",
    "evidence_refs",
    "verification_profile",
)
_RAW_TRANSCRIPT_SUFFIXES = {".jsonl", ".rollout", ".transcript"}
_REPLAY_FIXTURE_DIRECTORY = "replay/fixtures/"
_REPLAY_RECEIPT_PATH = "replay/fixtures/validation-receipts.json"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GitAdmissionError(ValueError):
    """Raised when a Git collaboration artifact violates the admission contract."""


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitAdmissionError(f"{field} must be a non-empty string")
    return value


def _reject_private_material(value: str, field: str) -> None:
    if _UUID_RE.search(value) or _PRIVATE_PATH_RE.search(value):
        raise GitAdmissionError(f"{field} contains a private provider identity or path")
    # Only values in explicit integrity fields or protocol references are
    # public metadata. Preserve byte positions so date-only PII exemptions
    # are still evaluated against their original substring.
    scan_value = _INTEGRITY_METADATA_RE.sub(
        lambda match: match.group(0).replace(
            match.group("digest"), "x" * len(match.group("digest"))
        ),
        value,
    )
    scan_value = _PUBLIC_MEASUREMENT_METADATA_RE.sub(
        lambda match: match.group(0).replace(
            match.group("value"), "x" * len(match.group("value"))
        ),
        scan_value,
    )
    findings = sanitize_text(scan_value).findings
    disallowed = [
        finding
        for finding in findings
        if not (
            finding.category == "pii"
            and _DATE_ONLY_RE.fullmatch(value[finding.start : finding.end])
        )
    ]
    if disallowed:
        raise GitAdmissionError(f"{field} contains secret, PII, provider, or machine material")


def _parse_rfc3339(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitAdmissionError(f"{field} must be RFC3339 or null") from exc
    if parsed.tzinfo is None:
        raise GitAdmissionError(f"{field} must include a timezone")


def _validate_branch(branch: Any) -> None:
    expected = {
        "branch_id",
        "parent_id",
        "scope",
        "return_point",
        "exit_criteria",
        "attempt_budget",
        "expiry",
        "promotion_target",
        "mainline_authority",
    }
    if not isinstance(branch, dict) or set(branch) != expected:
        raise GitAdmissionError("branch fields do not match the contract")
    branch_id = _require_text(branch["branch_id"], "branch_id")
    _reject_private_material(branch_id, "branch_id")
    if not _BRANCH_RE.fullmatch(branch_id):
        raise GitAdmissionError("branch_id must use a provider-neutral work ref")
    for field in ("parent_id", "scope", "return_point"):
        value = _require_text(branch[field], f"branch.{field}")
        _reject_private_material(value, f"branch.{field}")
    if not isinstance(branch["exit_criteria"], list) or not branch["exit_criteria"]:
        raise GitAdmissionError("branch.exit_criteria must be a non-empty array")
    for item in branch["exit_criteria"]:
        _reject_private_material(_require_text(item, "branch.exit_criteria"), "branch.exit_criteria")
    if (
        not isinstance(branch["attempt_budget"], int)
        or isinstance(branch["attempt_budget"], bool)
        or branch["attempt_budget"] < 1
    ):
        raise GitAdmissionError("branch.attempt_budget must be a positive integer")
    if branch["expiry"] is not None:
        expiry = _require_text(branch["expiry"], "branch.expiry")
        _reject_private_material(expiry, "branch.expiry")
        _parse_rfc3339(expiry, "branch.expiry")
    if branch["promotion_target"] is not None:
        _reject_private_material(
            _require_text(branch["promotion_target"], "branch.promotion_target"),
            "branch.promotion_target",
        )
    if branch["mainline_authority"] is not False:
        raise GitAdmissionError("branch.mainline_authority must be false")


def _validate_commit(subject: Any, body: Any) -> dict[str, str]:
    subject = _require_text(subject, "commit subject")
    body = _require_text(body, "commit body")
    if not _CONVENTIONAL_SUBJECT_RE.fullmatch(subject):
        raise GitAdmissionError("commit subject must use Conventional Commit syntax")
    _reject_private_material(subject, "commit subject")
    _reject_private_material(body, "commit body")
    why = _field_value(body, "Why")
    if why is None:
        raise GitAdmissionError("commit body must contain a Why statement")

    trailers: dict[str, str] = {}
    for line in body.splitlines():
        for field in _TRAILER_FIELDS:
            prefix = f"{field}:"
            if line.startswith(prefix):
                trailers[field] = line.removeprefix(prefix).strip()
    if set(trailers) != set(_TRAILER_FIELDS) or not all(trailers.values()):
        raise GitAdmissionError("commit body must contain Task, State-Revision, Evidence, and Tests")
    if not trailers["Task"].startswith("M"):
        raise GitAdmissionError("commit Task trailer must identify a work item")
    if not trailers["State-Revision"].isdigit() or int(trailers["State-Revision"]) < 1:
        raise GitAdmissionError("commit State-Revision trailer must be a positive integer")
    if not _ARTIFACT_REF_RE.fullmatch(trailers["Evidence"]) and not trailers[
        "Evidence"
    ].startswith("docs/"):
        raise GitAdmissionError("commit Evidence trailer must be an artifact ref or repository path")
    return trailers


def _field_value(body: str, field: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(field)}:\s*(.+?)\s*$", body)
    return match.group(1).strip() if match else None


def _validate_pull_request(title: Any, body: Any) -> dict[str, str]:
    title = _require_text(title, "pull_request title")
    body = _require_text(body, "pull_request body")
    if not _CONVENTIONAL_SUBJECT_RE.fullmatch(title):
        raise GitAdmissionError("pull_request title must use Conventional Commit syntax")
    _reject_private_material(title, "pull_request title")
    _reject_private_material(body, "pull_request body")
    if any(section not in body for section in _PR_SECTIONS):
        raise GitAdmissionError("pull_request body is missing a required section")
    values: dict[str, str] = {}
    for field in _PR_FIELDS:
        value = _field_value(body, field)
        if value is None:
            raise GitAdmissionError(f"pull_request body is missing {field}")
        values[field] = value
    if values["mainline_authority"] != "false":
        raise GitAdmissionError("pull_request mainline_authority must be false")
    if not values["attempt_budget"].isdigit() or int(values["attempt_budget"]) < 1:
        raise GitAdmissionError("pull_request attempt_budget must be a positive integer")
    if not values["state_revision"].isdigit() or int(values["state_revision"]) < 1:
        raise GitAdmissionError("pull_request state_revision must be a positive integer")
    if values["expiry"] != "null":
        _parse_rfc3339(values["expiry"], "pull_request expiry")
    evidence_values = re.findall(r"artifact://sha256/[0-9a-f]{64}", values["evidence_refs"])
    if not evidence_values:
        raise GitAdmissionError("pull_request evidence_refs must contain an artifact reference")
    return values


def validate_git_collaboration_packet(packet: Any) -> None:
    """Validate provider-neutral branch, commit, and PR metadata before review."""
    expected = {"schema_version", "branch", "commit", "pull_request"}
    if not isinstance(packet, dict) or set(packet) != expected:
        raise GitAdmissionError("packet fields do not match the contract")
    if packet["schema_version"] != PACKET_SCHEMA_VERSION:
        raise GitAdmissionError("unsupported Git collaboration packet schema_version")
    _validate_branch(packet["branch"])
    commit = packet["commit"]
    if not isinstance(commit, dict) or set(commit) != {"subject", "body"}:
        raise GitAdmissionError("commit fields do not match the contract")
    trailers = _validate_commit(commit["subject"], commit["body"])
    pull_request = packet["pull_request"]
    if not isinstance(pull_request, dict) or set(pull_request) != {"title", "body"}:
        raise GitAdmissionError("pull_request fields do not match the contract")
    pr_values = _validate_pull_request(pull_request["title"], pull_request["body"])
    branch_work_id = packet["branch"]["branch_id"].split("/", 2)[1]
    if trailers["Task"] != branch_work_id:
        raise GitAdmissionError("commit Task does not match branch work_id")
    if pr_values["work_id"] != branch_work_id:
        raise GitAdmissionError("pull_request work_id does not match branch work_id")
    if pull_request["title"] != commit["subject"]:
        raise GitAdmissionError("pull_request title does not match commit subject")
    if pr_values["state_revision"] != trailers["State-Revision"]:
        raise GitAdmissionError("pull_request state_revision does not match commit State-Revision")
    for pr_field, branch_field in (
        ("parent_id", "parent_id"),
        ("scope", "scope"),
        ("return_point", "return_point"),
        ("attempt_budget", "attempt_budget"),
    ):
        if pr_values[pr_field] != str(packet["branch"][branch_field]):
            raise GitAdmissionError(
                f"pull_request {pr_field} does not match branch metadata"
            )
    expected_expiry = packet["branch"]["expiry"]
    expected_pr_expiry = "null" if expected_expiry is None else expected_expiry
    if pr_values["expiry"] != expected_pr_expiry:
        raise GitAdmissionError("pull_request expiry does not match branch metadata")
    expected_promotion_target = packet["branch"]["promotion_target"]
    expected_pr_target = (
        "null" if expected_promotion_target is None else expected_promotion_target
    )
    if pr_values["promotion_target"] != expected_pr_target:
        raise GitAdmissionError(
            "pull_request promotion_target does not match branch metadata"
        )
    expected_exit_criteria = "[" + ", ".join(packet["branch"]["exit_criteria"]) + "]"
    if pr_values["exit_criteria"] != expected_exit_criteria:
        raise GitAdmissionError(
            "pull_request exit_criteria does not match branch metadata"
        )


def _git(root: Path | str, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    root = Path(root)
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitAdmissionError(f"Git audit failed: {detail or arguments[0]}")
    return completed.stdout


def _staged_paths(root: Path | str) -> list[str]:
    output = _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    paths = [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]
    return sorted(paths)


def _read_staged_blob(root: Path | str, path: str) -> bytes:
    return _git(root, "show", f":{path}")


def _validate_staged_path(path: str) -> None:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if (
        "raw-conversations" in parts
        or Path(normalized).suffix.lower() in _RAW_TRANSCRIPT_SUFFIXES
        or "transcript" in Path(normalized).stem.lower()
    ):
        raise GitAdmissionError(f"raw transcript staged path is forbidden: {path}")
    _reject_private_material(path, "staged path")


def audit_staged_admission(root: Path | str) -> dict[str, Any]:
    """Audit only blobs staged in the Git index, never unstaged worktree files."""
    paths = _staged_paths(root)
    if not paths:
        raise GitAdmissionError("staged admission requires one or more staged paths")
    staged_blobs = {path: _read_staged_blob(root, path) for path in paths}
    _audit_blob_set(paths, lambda path: staged_blobs[path])
    _validate_replay_fixture_admission(staged_blobs)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "admission_kind": "staged",
        "admitted_paths": paths,
        "staged_set_sha256": _canonical_digest(paths),
        "runtime_state_authority": False,
    }


def _tree_paths(root: Path | str, commit: str) -> list[str]:
    output = _git(root, "ls-tree", "-r", "-z", "--name-only", commit)
    paths = [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]
    return sorted(paths)


def _read_tree_blob(root: Path | str, commit: str, path: str) -> bytes:
    return _git(root, "show", f"{commit}:{path}")


def _audit_blob_set(paths: list[str], read_blob: Any) -> None:
    for path in paths:
        _validate_staged_path(path)
        content = read_blob(path)
        if len(content) > 8 * 1024 * 1024:
            raise GitAdmissionError(f"staged artifact exceeds 8 MiB admission bound: {path}")
        text = content.decode("utf-8", errors="replace")
        _reject_private_material(text, f"staged content {path}")


def _json_blob(blob: bytes, label: str) -> Any:
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitAdmissionError(f"{label} must be valid UTF-8 JSON") from exc


def _validate_replay_fixture_admission(blobs: dict[str, bytes]) -> None:
    fixture_paths = sorted(
        path
        for path in blobs
        if path.startswith(_REPLAY_FIXTURE_DIRECTORY)
        and path.endswith(".json")
        and path != _REPLAY_RECEIPT_PATH
    )
    if not fixture_paths:
        return
    receipt_blob = blobs.get(_REPLAY_RECEIPT_PATH)
    if receipt_blob is None:
        raise GitAdmissionError(
            "replay fixture provenance receipt must be staged with every fixture"
        )
    manifest = _json_blob(receipt_blob, "replay fixture provenance receipt")
    if not isinstance(manifest, dict):
        raise GitAdmissionError("replay fixture provenance receipt must be an object")
    evidence_refs = manifest.get("evidence_refs")
    receipts = manifest.get("receipts")
    if (
        not isinstance(evidence_refs, list)
        or not all(isinstance(item, str) for item in evidence_refs)
        or not isinstance(receipts, list)
    ):
        raise GitAdmissionError("replay fixture provenance receipt fields are invalid")
    receipts_by_id = {
        receipt.get("fixture_id"): receipt
        for receipt in receipts
        if isinstance(receipt, dict) and isinstance(receipt.get("fixture_id"), str)
    }
    for path in fixture_paths:
        fixture = _json_blob(blobs[path], f"replay fixture {path}")
        fixture_id = fixture.get("fixture_id") if isinstance(fixture, dict) else None
        receipt = receipts_by_id.get(fixture_id)
        if receipt is None:
            raise GitAdmissionError(
                f"replay fixture provenance receipt is missing: {path}"
            )
        try:
            validated = validate_replay_fixture(
                fixture,
                current_evidence_refs=set(evidence_refs),
                validated_at=receipt["validated_at"],
            )
        except (KeyError, TypeError, ReplayFixtureError) as exc:
            raise GitAdmissionError(
                f"replay fixture provenance validation failed: {path}"
            ) from exc
        for field, value in asdict(validated).items():
            if receipt.get(field) != value:
                raise GitAdmissionError(
                    f"replay fixture provenance receipt mismatch: {path}"
                )


def _commit_message(root: Path | str, commit: str) -> tuple[str, str]:
    output = _git(root, "show", "-s", "--format=%s%x00%b", commit)
    subject, separator, body = output.partition(b"\0")
    if not separator:
        raise GitAdmissionError("Git audit could not read commit message")
    return (
        subject.decode("utf-8", errors="replace"),
        body.decode("utf-8", errors="replace").rstrip("\n"),
    )


def audit_regular_merge(root: Path | str, merge_commit: str) -> dict[str, Any]:
    """Return a receipt only for a two-parent regular merge commit."""
    parents = _git(root, "rev-list", "--parents", "-n", "1", merge_commit).decode(
        "ascii", errors="strict"
    ).split()
    if len(parents) != 3:
        raise GitAdmissionError("regular merge audit requires exactly two parent commits")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "admission_kind": "regular-merge",
        "merge_commit": parents[0],
        "target_parent": parents[1],
        "source_parent": parents[2],
        "runtime_state_authority": False,
    }


def audit_first_commit(root: Path | str) -> dict[str, Any]:
    """Replay the root commit identity and machine-parseable admission trailers."""
    roots = _git(root, "rev-list", "--max-parents=0", "HEAD").decode(
        "ascii", errors="strict"
    ).split()
    if len(roots) != 1:
        raise GitAdmissionError("first-commit audit requires exactly one root commit")
    root_commit = roots[0]
    subject, body = _commit_message(root, root_commit)
    _validate_commit(subject, body)
    root_paths = _tree_paths(root, root_commit)
    root_blobs = {
        path: _read_tree_blob(root, root_commit, path) for path in root_paths
    }
    _audit_blob_set(root_paths, lambda path: root_blobs[path])
    _validate_replay_fixture_admission(root_blobs)
    author = _git(root, "show", "-s", "--format=%an%x00%ae", root_commit)
    author_name, separator, author_email = author.partition(b"\0")
    if not separator or not author_name.strip() or not author_email.strip():
        raise GitAdmissionError("first-commit audit requires author name and email")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "admission_kind": "first-commit",
        "root_commit": root_commit,
        "commit_count_before_root": 0,
        "author_name": author_name.decode("utf-8", errors="replace"),
        "author_email": author_email.decode("utf-8", errors="replace"),
        "runtime_state_authority": False,
    }
