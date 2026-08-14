"""Deterministic scope ownership and side-effect authorization gate."""

from __future__ import annotations

from datetime import datetime
from typing import Any

_PATH_KINDS = {"repo", "directory", "file", "symbol"}
_SCOPE_KINDS = _PATH_KINDS | {"capability", "effect"}
_PATH_OPERATION_PREFIXES = ("write-",)
_CAPABILITY_OPERATIONS = {"record-correction"}
_EFFECT_OPERATION_PREFIXES = ("deploy", "external-")
_CURRENT_TYPED_STATE_VERSIONS = {
    "context.typed-state/v2alpha1",
    "context.typed-state/v3alpha1",
    "context.typed-state/v4alpha1",
}


def _path_identity(scope: dict[str, str]) -> tuple[str | None, tuple[str, ...], str | None]:
    """Return repository identity, relative path and optional symbol fragment."""
    kind = scope["scope_kind"]
    ref = scope["scope_ref"]
    path_ref, separator, symbol_ref = ref.partition("#")
    if kind == "symbol":
        if not separator or not path_ref or not symbol_ref or "#" in symbol_ref:
            raise ValueError("symbol scope requires one non-empty fragment")
    elif separator:
        raise ValueError("only symbol scope may contain a fragment")
    if "\\" in path_ref or path_ref.startswith("/"):
        raise ValueError("scope path must use a canonical relative POSIX form")

    repository_id: str | None
    relative_path: str
    if path_ref.startswith("repo://"):
        segments = path_ref[len("repo://") :].split("/")
        if not segments or not segments[0]:
            raise ValueError("repo scope requires a repository identity")
        repository_id = segments[0]
        relative_path = "/".join(segments[1:])
        if kind == "repo" and relative_path:
            raise ValueError("repo scope must name only its repository root")
        if kind != "repo" and not relative_path:
            raise ValueError("path scope requires a path below its repository root")
    else:
        if ":" in path_ref or path_ref.startswith("repo:"):
            raise ValueError("scope path uses an unsupported URI form")
        if kind == "repo":
            raise ValueError("repo scope requires a repo:// repository identity")
        repository_id = None
        relative_path = path_ref

    parts = tuple(relative_path.split("/")) if relative_path else ()
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("scope path is ambiguous or escapes its root")
    return repository_id, parts, symbol_ref if separator else None


def path_scope_repository_identity(scope: dict[str, str]) -> str | None:
    """Return the explicit repository identity, or None for a project-relative path."""
    validate_scope(scope)
    if scope["scope_kind"] not in _PATH_KINDS:
        return None
    return _path_identity(scope)[0]


def validate_scope(scope: dict[str, str]) -> dict[str, str]:
    """Validate the canonical scope wire form used by M3-04 ownership gates."""
    if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
        raise ValueError("scope fields do not match the contract")
    kind = scope["scope_kind"]
    ref = scope["scope_ref"]
    if kind not in _SCOPE_KINDS or not isinstance(ref, str) or not ref.strip():
        raise ValueError("scope kind or ref is invalid")
    if kind in _PATH_KINDS:
        repository_id, _, _ = _path_identity(scope)
        if repository_id is None:
            raise ValueError("path scope requires a repo:// repository identity")
    elif "#" in ref or ":" in ref:
        raise ValueError("capability and effect scopes cannot contain fragments or URI forms")
    return scope


def _path_contains(owner: dict[str, str], requested: dict[str, str]) -> bool:
    owner_repository, owner_parts, _ = _path_identity(owner)
    requested_repository, requested_parts, _ = _path_identity(requested)
    return (
        owner_repository == requested_repository
        and len(owner_parts) <= len(requested_parts)
        and requested_parts[: len(owner_parts)] == owner_parts
    )


def scope_covers(owner: dict[str, str], requested: dict[str, str]) -> bool:
    """Return whether one canonical owned scope grants a requested scope."""
    validate_scope(owner)
    validate_scope(requested)
    owner_kind = owner["scope_kind"]
    requested_kind = requested["scope_kind"]
    if owner_kind in {"capability", "effect"} or requested_kind in {
        "capability",
        "effect",
    }:
        return owner == requested
    if owner_kind == "repo":
        return requested_kind in _PATH_KINDS and _path_contains(owner, requested)
    if owner_kind == "directory":
        return requested_kind in {"directory", "file", "symbol"} and _path_contains(
            owner, requested
        )
    if owner_kind == "file":
        owner_repository, owner_path, _ = _path_identity(owner)
        requested_repository, requested_path, _ = _path_identity(requested)
        return (
            requested_kind in {"file", "symbol"}
            and owner_repository == requested_repository
            and owner_path == requested_path
        )
    return owner == requested


def scopes_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    """Return whether two scopes can authorize a common side effect."""
    return scope_covers(left, right) or scope_covers(right, left)


def any_scope_covers(
    owned_scopes: list[dict[str, str]], requested_scope: dict[str, str]
) -> bool:
    return any(scope_covers(owner, requested_scope) for owner in owned_scopes)


def any_scope_overlap(
    left_scopes: list[dict[str, str]], right_scopes: list[dict[str, str]]
) -> bool:
    return any(
        scopes_overlap(left, right)
        for left in left_scopes
        for right in right_scopes
    )


def operation_scope_kinds(operation: str) -> set[str] | None:
    """Map an external operation to the only scope classes it can consume."""
    if not isinstance(operation, str) or not operation.strip():
        return None
    if operation.startswith(_PATH_OPERATION_PREFIXES):
        return _PATH_KINDS
    if operation.startswith("git-") or operation in _CAPABILITY_OPERATIONS:
        return {"capability"}
    if operation.startswith(_EFFECT_OPERATION_PREFIXES):
        return {"effect"}
    return None


def _is_current_scope_contract(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("schema_version") in _CURRENT_TYPED_STATE_VERSIONS


def _legacy_scope_covers(owner: dict[str, str], requested: dict[str, str]) -> bool:
    return owner == requested


def _snapshot_scope_covers(
    snapshot: dict[str, Any], owner: dict[str, str], requested: dict[str, str]
) -> bool:
    return (
        scope_covers(owner, requested)
        if _is_current_scope_contract(snapshot)
        else _legacy_scope_covers(owner, requested)
    )


def _snapshot_effect_scope_conflict(
    snapshot: dict[str, Any],
    *,
    requested_scope: dict[str, str],
    effect_id: str | None,
) -> bool:
    return any(
        effect["status"] in {"authorized", "started"}
        and effect["effect_id"] != effect_id
        and (
            scopes_overlap(effect["scope_ref"], requested_scope)
            if _is_current_scope_contract(snapshot)
            else effect["scope_ref"] == requested_scope
        )
        for effect in snapshot["effects"]
    )


def _verdict(*, decision: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "context.effect-scope-verdict/v1alpha1",
        "decision": decision,
        "read_only": decision != "allow",
        "reason": reason,
    }


def _effect_by_id(snapshot: dict[str, Any], effect_id: str | None) -> dict[str, Any] | None:
    if effect_id is None:
        return None
    return next(
        (item for item in snapshot["effects"] if item["effect_id"] == effect_id),
        None,
    )


def evaluate_effect_scope_gate(
    snapshot: dict[str, Any],
    *,
    actor_ref: str,
    work_id: str,
    claim_id: str,
    expected_revision: int,
    operation: str,
    requested_scope: dict[str, str],
    effect_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Evaluate pre-execution authority for one external side effect."""
    if snapshot["project"]["revision"] != expected_revision:
        return _verdict(decision="deny", reason="stale_revision")
    allowed_scope_kinds = operation_scope_kinds(operation)
    if allowed_scope_kinds is None:
        return _verdict(decision="deny", reason="operation_invalid")
    if _is_current_scope_contract(snapshot):
        try:
            validate_scope(requested_scope)
        except ValueError:
            return _verdict(decision="deny", reason="scope_invalid")
    if requested_scope["scope_kind"] not in allowed_scope_kinds:
        return _verdict(decision="deny", reason="operation_scope_mismatch")
    work = next((item for item in snapshot["works"] if item["work_id"] == work_id), None)
    if work is None or work["status"] != "active":
        return _verdict(decision="deny", reason="inactive_work")
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == claim_id), None
    )
    if claim is None or claim["status"] != "active" or claim["work_id"] != work_id:
        return _verdict(decision="deny", reason="claim_mismatch")
    if claim["expected_project_revision"] != expected_revision:
        return _verdict(decision="deny", reason="claim_revision_mismatch")
    if claim["actor_ref"] != actor_ref or actor_ref not in work["owner_refs"]:
        return _verdict(decision="deny", reason="actor_mismatch")
    if observed_at is not None:
        current = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(claim["lease_expires_at"].replace("Z", "+00:00"))
        if current >= expiry:
            return _verdict(decision="deny", reason="claim_expired")
    try:
        if not any(
            _snapshot_scope_covers(snapshot, owner, requested_scope)
            for owner in claim["scope_owners"]
        ):
            return _verdict(decision="deny", reason="scope_not_owned")
        if _snapshot_effect_scope_conflict(
            snapshot,
            requested_scope=requested_scope,
            effect_id=effect_id,
        ):
            return _verdict(decision="deny", reason="effect_scope_conflict")
    except ValueError:
        return _verdict(decision="deny", reason="scope_invalid")
    return _verdict(decision="allow", reason="authorized")


def evaluate_effect_completion_gate(
    snapshot: dict[str, Any],
    *,
    actor_ref: str,
    work_id: str,
    claim_id: str,
    expected_revision: int,
    effect_id: str,
    effect_key: str,
    operation: str,
    requested_scope: dict[str, str],
) -> dict[str, Any]:
    """Validate a completion receipt without re-authorizing the past effect."""
    if snapshot["project"]["revision"] != expected_revision:
        return _verdict(decision="deny", reason="stale_revision")
    existing = _effect_by_id(snapshot, effect_id)
    if existing is None:
        return _verdict(decision="deny", reason="effect_not_found")
    if existing["status"] not in {"authorized", "started"}:
        return _verdict(decision="deny", reason="effect_not_pending")
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == existing["claim_id"]),
        None,
    )
    if claim is None or claim["actor_ref"] != actor_ref:
        return _verdict(decision="deny", reason="completion_actor_mismatch")
    if (
        existing["effect_key"] != effect_key
        or existing["work_id"] != work_id
        or existing["claim_id"] != claim_id
        or existing["operation"] != operation
        or existing["scope_ref"] != requested_scope
    ):
        return _verdict(decision="deny", reason="effect_provenance_mismatch")
    return _verdict(decision="allow", reason="authorized")


def evaluate_claim_scope_gate(
    snapshot: dict[str, Any],
    *,
    actor_ref: str,
    work_id: str,
    expected_revision: int,
    requested_scopes: list[dict[str, str]],
) -> dict[str, Any]:
    """Evaluate authority and overlap before creating an active Claim."""
    if snapshot["project"]["revision"] != expected_revision:
        return _verdict(decision="deny", reason="stale_revision")
    work = next((item for item in snapshot["works"] if item["work_id"] == work_id), None)
    if work is None or work["status"] != "ready":
        return _verdict(decision="deny", reason="work_not_ready")
    if actor_ref not in work["owner_refs"]:
        return _verdict(decision="deny", reason="actor_not_owner")
    try:
        if not all(
            any(
                _snapshot_scope_covers(snapshot, owner, requested)
                for owner in work["scope_refs"]
            )
            for requested in requested_scopes
        ):
            return _verdict(decision="deny", reason="scope_outside_work")
        if claim_scope_conflict(
            snapshot,
            requested_scopes=requested_scopes,
            work_id=work_id,
        ):
            return _verdict(decision="deny", reason="scope_conflict")
    except ValueError:
        return _verdict(decision="deny", reason="scope_invalid")
    return _verdict(decision="allow", reason="authorized")


def claim_scope_conflict(
    snapshot: dict[str, Any],
    *,
    requested_scopes: list[dict[str, str]],
    work_id: str,
) -> bool:
    return any(
        claim["status"] == "active"
        and claim["work_id"] != work_id
        and (
            any_scope_overlap(claim["scope_owners"], requested_scopes)
            if _is_current_scope_contract(snapshot)
            else any(
                owned == requested
                for owned in claim["scope_owners"]
                for requested in requested_scopes
            )
        )
        for claim in snapshot["claims"]
    )


def effect_scope_conflict(
    snapshot: dict[str, Any],
    *,
    requested_scope: dict[str, str],
    claim_id: str,
    effect_id: str | None = None,
) -> bool:
    return any(
        effect["status"] in {"authorized", "started"}
        and effect["effect_id"] != effect_id
        and scopes_overlap(effect["scope_ref"], requested_scope)
        for effect in snapshot["effects"]
    )
