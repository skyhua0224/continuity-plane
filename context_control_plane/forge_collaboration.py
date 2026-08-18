"""Provider-neutral, read-only projections of Git forge collaboration state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote


FORGE_PROJECTION_SCHEMA_VERSION = "context.forge-work-projection/v1alpha1"
FORGE_REF_UPDATE_INTENT_SCHEMA_VERSION = "context.forge-ref-update-intent/v1alpha1"
FORGE_UNPUBLISHED_WORK_SCHEMA_VERSION = "context.forge-unpublished-work/v1alpha1"
_SUPPORTED_PROVIDERS = frozenset({"github", "gitea", "gitlab"})
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://.+$")


class ForgeProjectionError(ValueError):
    """Raised when a forge snapshot cannot form a safe work projection."""


class ForgeConflictError(ForgeProjectionError):
    """Raised when a remote ref is no longer the value the caller observed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ForgeProjectionError("projection_not_canonical") from exc


def _projection_digest(projection: Mapping[str, Any]) -> str:
    body = {key: value for key, value in projection.items() if key != "projection_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForgeProjectionError(f"{field}_invalid")
    return value


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 4096
    ):
        raise ForgeProjectionError(f"{field}_invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    timestamp = _text(value, field)
    if _TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise ForgeProjectionError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForgeProjectionError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForgeProjectionError(f"{field}_invalid")
    return timestamp


def _instance_id(value: object) -> str:
    instance_id = _text(value, "instance_id")
    if _INSTANCE_ID_RE.fullmatch(instance_id) is None:
        raise ForgeProjectionError("instance_id_invalid")
    return instance_id


def _items(
    value: object, field: str, *, max_items: int = 10000
) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ForgeProjectionError(f"{field}_invalid")
    if len(value) > max_items:
        raise ForgeProjectionError(f"{field}_too_many")
    return [_mapping(item, field) for item in value]


def _number(record: Mapping[str, Any], provider: str, kind: str) -> int:
    fields = (
        ("number", "iid", "index")
        if provider in {"gitea", "gitlab"}
        else ("number", "iid")
    )
    for field in fields:
        value = record.get(field)
        if type(value) is int and value > 0:
            return value
    raise ForgeProjectionError(f"{kind}_number_invalid")


def _actor_ref(value: object, provider: str, instance_id: str) -> str:
    record = _mapping(value, "actor")
    for field in ("login", "username", "name"):
        candidate = record.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return (
                f"actor://{provider}/{quote(instance_id, safe='')}/"
                f"{quote(candidate.strip(), safe='')}"
            )
    raise ForgeProjectionError("actor_ref_invalid")


def _actor_refs(
    value: object, field: str, provider: str, instance_id: str
) -> list[str]:
    refs = {
        _actor_ref(item, provider, instance_id)
        for item in _items(value, field, max_items=256)
    }
    if any(len(ref) > 1024 for ref in refs):
        raise ForgeProjectionError(f"{field}_invalid")
    return sorted(refs)


def _branch_ref(pull_request: Mapping[str, Any]) -> str | None:
    head = pull_request.get("head")
    if not isinstance(head, Mapping):
        return None
    reference = head.get("ref")
    if not isinstance(reference, str) or not reference.strip():
        return None
    branch_ref = f"refs/heads/{reference.strip()}"
    if len(branch_ref) > 1024:
        raise ForgeProjectionError("branch_ref_invalid")
    return branch_ref


def _pull_for_issue(
    pull_requests: list[Mapping[str, Any]], issue_number: int
) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    for pull_request in pull_requests:
        linked_numbers = (
            pull_request.get(field)
            for field in ("issue_number", "issue_index", "issue_iid")
        )
        if any(type(value) is int and value == issue_number for value in linked_numbers):
            matches.append(pull_request)
    if len(matches) > 1:
        raise ForgeProjectionError("ambiguous_issue_pull_request")
    return matches[0] if matches else None


def _readiness(issue: Mapping[str, Any], pull_request: Mapping[str, Any] | None) -> str:
    issue_state = issue.get("state")
    if issue_state in {"closed", "merged"}:
        return "completion-candidate"
    if pull_request is not None:
        if pull_request.get("state") in {"closed", "merged"}:
            return "completion-candidate"
        return "verifying"
    return "ready"


def _review_evidence(
    pull_request: Mapping[str, Any] | None, provider: str, instance_id: str
) -> list[dict[str, str]]:
    if pull_request is None:
        return []
    evidence: list[dict[str, str]] = []
    for review in _items(pull_request.get("reviews", []), "reviews"):
        reviewer = _actor_ref(
            review.get("user", review.get("reviewer", {})), provider, instance_id
        )
        state = _text(review.get("state"), "review_state").lower()
        evidence.append(
            {
                "kind": "review",
                "ref": f"review://{reviewer.removeprefix('actor://')}/{state}",
            }
        )
    checks = pull_request.get("checks", pull_request.get("statuses", []))
    for check in _items(checks, "checks"):
        name = check.get("name", check.get("context"))
        state = check.get("conclusion", check.get("state"))
        evidence.append(
            {
                "kind": "ci",
                "ref": f"ci://{_text(name, 'check_name')}/{_text(state, 'check_state').lower()}",
            }
        )
    if len(evidence) > 10000 or any(len(item["ref"]) > 4096 for item in evidence):
        raise ForgeProjectionError("evidence_invalid")
    return sorted(evidence, key=lambda item: (item["kind"], item["ref"]))


def _normalize_refs(value: Mapping[str, Any]) -> dict[str, str]:
    if len(value) > 10000:
        raise ForgeProjectionError("refs_too_many")
    normalized: dict[str, str] = {}
    for raw_name, raw_oid in value.items():
        name = _text(raw_name, "ref_name")
        if len(name) > 1024:
            raise ForgeProjectionError("ref_name_invalid")
        if name in normalized:
            raise ForgeProjectionError("duplicate_ref_name")
        normalized[name] = _oid(raw_oid, "ref_oid")
    return dict(sorted(normalized.items()))


def project_forge_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project a pinned forge snapshot without granting state or effect authority."""
    source = _mapping(snapshot, "snapshot")
    provider = _text(source.get("provider"), "provider").lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ForgeProjectionError("provider_unsupported")
    instance_id = _instance_id(source.get("instance_id"))
    repository = _mapping(source.get("repository"), "repository")
    owner = _text(repository.get("owner"), "repository_owner")
    name = _text(repository.get("name"), "repository_name")
    source_revision = _text(source.get("source_revision"), "source_revision")
    observed_at = _timestamp(source.get("observed_at"), "observed_at")
    issues = _items(source.get("issues", []), "issues")
    pull_requests = _items(source.get("pull_requests", []), "pull_requests")
    refs = _normalize_refs(_mapping(source.get("refs", {}), "refs"))
    repository_ref = (
        f"forge://{provider}/{quote(instance_id, safe='')}/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}"
    )
    if len(repository_ref) > 1024:
        raise ForgeProjectionError("repository_ref_invalid")

    works: list[dict[str, Any]] = []
    seen_issue_numbers: set[int] = set()
    for issue in issues:
        issue_number = _number(issue, provider, "issue")
        if issue_number in seen_issue_numbers:
            raise ForgeProjectionError("duplicate_issue_number")
        seen_issue_numbers.add(issue_number)
        pull_request = _pull_for_issue(pull_requests, issue_number)
        actor_values = issue.get("assignees", [])
        if pull_request is not None and pull_request.get("assignees"):
            actor_values = pull_request["assignees"]
        branch = _branch_ref(pull_request) if pull_request is not None else None
        if branch is not None and branch not in refs:
            raise ForgeProjectionError("branch_ref_missing")
        reviewer_values = (
            pull_request.get("requested_reviewers", []) if pull_request else []
        )
        work_id = (
            f"forge-work:{provider}:{instance_id}:{quote(owner, safe='')}:"
            f"{quote(name, safe='')}:issue:{issue_number}"
        )
        work_ref = f"{repository_ref}/issues/{issue_number}"
        if len(work_id) > 1024 or len(work_ref) > 1024:
            raise ForgeProjectionError("work_ref_invalid")
        works.append(
            {
                "work_id": work_id,
                "source_kind": "issue-backed",
                "source_ref": work_ref,
                "source_revision": source_revision,
                "title": _text(issue.get("title"), "issue_title"),
                "readiness": _readiness(issue, pull_request),
                "branch_ref": branch,
                "candidate_claim": {
                    "visibility": "published",
                    "actor_refs": _actor_refs(
                        actor_values, "assignees", provider, instance_id
                    ),
                    "claim_uniqueness": "not-guaranteed",
                },
                "reviewer_refs": _actor_refs(
                    reviewer_values, "requested_reviewers", provider, instance_id
                ),
                "evidence": _review_evidence(pull_request, provider, instance_id),
            }
        )

    projection = {
        "schema_version": FORGE_PROJECTION_SCHEMA_VERSION,
        "provider": provider,
        "instance_id": instance_id,
        "repository_ref": repository_ref,
        "source_revision": source_revision,
        "observed_at": observed_at,
        "works": sorted(works, key=lambda item: item["work_id"]),
        "refs": refs,
        "authority": {
            "state_write_authority": False,
            "claim_authority": False,
            "effect_authority": False,
            "provider_invocations": 0,
            "external_services": 0,
        },
    }
    projection["projection_sha256"] = _projection_digest(projection)
    return projection


def replay_forge_snapshot(
    snapshot: Mapping[str, Any], *, expected_projection_sha256: str
) -> dict[str, Any]:
    """Replay a pinned snapshot and reject a divergent normalized projection."""
    expected = _text(expected_projection_sha256, "expected_projection_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ForgeProjectionError("expected_projection_sha256_invalid")
    projection = project_forge_snapshot(snapshot)
    if projection["projection_sha256"] != expected:
        raise ForgeProjectionError("projection_replay_mismatch")
    return projection


def _oid(value: object, field: str) -> str:
    oid = _text(value, field)
    if not _OID_RE.fullmatch(oid):
        raise ForgeProjectionError(f"{field}_invalid")
    return oid


def build_ref_update_intent(
    projection: Mapping[str, Any],
    *,
    branch_ref: str,
    expected_remote_oid: str,
    desired_oid: str,
) -> dict[str, Any]:
    """Create a CAS-bound remote ref intent without executing the remote effect."""
    source = _mapping(projection, "projection")
    if source.get("schema_version") != FORGE_PROJECTION_SCHEMA_VERSION:
        raise ForgeProjectionError("projection_schema_invalid")
    projection_sha256 = _text(
        source.get("projection_sha256"), "projection_sha256"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", projection_sha256):
        raise ForgeProjectionError("projection_sha256_invalid")
    if _projection_digest(source) != projection_sha256:
        raise ForgeProjectionError("projection_digest_mismatch")
    branch = _text(branch_ref, "branch_ref")
    if len(branch) > 1024 or not branch.startswith("refs/heads/"):
        raise ForgeProjectionError("branch_ref_invalid")
    expected = _oid(expected_remote_oid, "expected_remote_oid")
    desired = _oid(desired_oid, "desired_oid")
    refs = _mapping(source.get("refs"), "projection_refs")
    current = refs.get(branch)
    if current != expected:
        raise ForgeConflictError("remote_ref_mismatch")
    return {
        "schema_version": FORGE_REF_UPDATE_INTENT_SCHEMA_VERSION,
        "provider": _text(source.get("provider"), "provider"),
        "instance_id": _instance_id(source.get("instance_id")),
        "repository_ref": _text(source.get("repository_ref"), "repository_ref"),
        "source_revision": _text(source.get("source_revision"), "source_revision"),
        "projection_sha256": projection_sha256,
        "branch_ref": branch,
        "expected_remote_oid": expected,
        "desired_oid": desired,
        "status": "ready-for-authorized-dispatch",
        "authority": {
            "state_write_authority": False,
            "remote_effect_authority": False,
            "provider_invocations": 0,
            "external_services": 0,
        },
    }


def project_unpublished_work(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose an offline work record without implying a shared exclusive claim."""
    source = _mapping(record, "unpublished_work")
    work_id = _text(source.get("work_id"), "work_id")
    actor_ref = _text(source.get("actor_ref"), "actor_ref")
    local_ref = _text(source.get("local_ref"), "local_ref")
    if len(work_id) > 1024:
        raise ForgeProjectionError("work_id_invalid")
    if len(actor_ref) > 1024 or re.fullmatch(r"actor://.+", actor_ref) is None:
        raise ForgeProjectionError("actor_ref_invalid")
    if _URI_RE.fullmatch(local_ref) is None:
        raise ForgeProjectionError("local_ref_invalid")
    return {
        "schema_version": FORGE_UNPUBLISHED_WORK_SCHEMA_VERSION,
        "work_id": work_id,
        "actor_ref": actor_ref,
        "local_ref": local_ref,
        "observed_at": _timestamp(source.get("observed_at"), "observed_at"),
        "visibility": "local-only",
        "claim_uniqueness": "not-guaranteed",
        "required_resolution": "publish-or-state-mcp",
        "authority": {
            "state_write_authority": False,
            "claim_authority": False,
            "effect_authority": False,
        },
    }
