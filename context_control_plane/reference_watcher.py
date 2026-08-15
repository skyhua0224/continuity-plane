"""Provider-neutral reference freshness, change, and supersedes decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .assertion_provenance import (
    AssertionProvenanceError,
    validate_assertion_provenance,
)

OBSERVATION_SCHEMA_VERSION = "context.reference-watch-observation/v1alpha1"
DECISION_SCHEMA_VERSION = "context.reference-watch-decision/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/(?P<sha256>[0-9a-f]{64})$")
_AUTHORITIES = {
    "current_code",
    "current_state",
    "industry_standard",
    "os_official",
    "software_official",
}
_TRUSTED_TIME_KINDS = {"state-mcp", "rfc3161", "provider-signed", "fixture-clock"}
_OUTCOMES = {"unchanged", "changed", "unavailable", "expired"}

# Direct verifier callables are retained only for deterministic fixture
# callers. Live observations must resolve this metadata from a trusted
# registry before the callable is invoked.
_FIXTURE_COMPAT_VERIFIER_ID = "verifier/fixture-direct"
_FIXTURE_COMPAT_VERIFIER_VERSION = "v1"
_FIXTURE_COMPAT_TRUST_ANCHOR_SHA256 = hashlib.sha256(
    b"context-control-plane/reference-watcher/fixture-direct/v1"
).hexdigest()

_WATCH_FIELDS = {
    "watch_id",
    "source_id",
    "source_ref",
    "authority_kind",
    "watch_revision",
    "baseline_revision",
    "baseline_sha256",
    "valid_until",
    "assertion_ids",
}
_TRUSTED_TIME_FIELDS = {"kind", "trusted_at", "evidence"}
_OBSERVATION_FIELDS = {
    "schema_version",
    "observation_id",
    "watch_id",
    "watch_revision",
    "watch_sha256",
    "mode",
    "source_id",
    "source_ref",
    "source_identity_sha256",
    "authority_kind",
    "baseline_revision",
    "baseline_sha256",
    "availability_status",
    "observed_revision",
    "observed_sha256",
    "current_evidence_ref",
    "current_evidence_sha256",
    "valid_until",
    "trusted_at",
    "trusted_time_kind",
    "trusted_time_evidence_ref",
    "trusted_time_evidence_sha256",
    "trusted_time_verifier_id",
    "trusted_time_verifier_version",
    "trusted_time_trust_anchor_sha256",
    "trusted_time_registry_entry_sha256",
    "freshness_status",
    "watch_outcome",
    "affected_assertion_ids",
    "state_write_authority",
    "completion_authority",
    "provider_native_authority",
    "observation_sha256",
}
_DECISION_FIELDS = {
    "schema_version",
    "decision_id",
    "observation_id",
    "observation_sha256",
    "watch_id",
    "source_id",
    "source_identity_sha256",
    "watch_outcome",
    "assertion_status",
    "decision",
    "reason_code",
    "affected_assertion_ids",
    "stale_assertion_ids",
    "quarantined_assertion_ids",
    "superseded_assertion_ids",
    "completion_eligible_assertion_ids",
    "replacement_assertion_id",
    "replacement_record_sha256",
    "evaluated_at",
    "state_write_authority",
    "completion_authority",
    "provider_native_authority",
    "decision_sha256",
}


class ReferenceWatcherError(ValueError):
    """Raised when reference evidence cannot support a freshness decision."""


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
        raise ReferenceWatcherError(
            "reference watch data must be canonical JSON"
        ) from exc


def _unsigned_digest(value: Mapping[str, Any], digest_field: str) -> str:
    body = {key: item for key, item in value.items() if key != digest_field}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReferenceWatcherError(f"{field} is invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ReferenceWatcherError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise ReferenceWatcherError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReferenceWatcherError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReferenceWatcherError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReferenceWatcherError(f"{field} requires a timezone")
    return parsed


def _ids(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > 256
    ):
        raise ReferenceWatcherError(f"{field} is invalid")
    normalized = [_id(item, field) for item in value]
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        raise ReferenceWatcherError(f"{field} must be unique and sorted")
    return normalized


def _bytes(value: Any, field: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ReferenceWatcherError(f"{field} is invalid")
    payload = bytes(value)
    if not payload:
        raise ReferenceWatcherError(f"{field} is empty")
    return payload


def _artifact_ref(digest: str) -> str:
    return f"artifact://sha256/{digest}"


def _validate_artifact(ref: Any, digest: Any, field: str) -> None:
    digest = _sha(digest, f"{field}_sha256")
    if not isinstance(ref, str):
        raise ReferenceWatcherError(f"{field}_ref is invalid")
    match = _ARTIFACT_RE.fullmatch(ref)
    if match is None or match.group("sha256") != digest:
        raise ReferenceWatcherError(f"{field}_ref does not bind its digest")


def _source_identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "authority_kind": value["authority_kind"],
                "source_id": value["source_id"],
                "source_ref": value["source_ref"],
            }
        )
    ).hexdigest()


def _watch_digest(watch: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(watch))).hexdigest()


def _resolved_bytes(
    resolver: Callable[[str], bytes | bytearray | memoryview | None] | None,
    ref: str,
    field: str,
) -> bytes:
    if resolver is None:
        raise ReferenceWatcherError(f"{field} requires an artifact resolver")
    try:
        value = resolver(ref)
    except Exception as exc:
        raise ReferenceWatcherError(f"{field} resolver failed") from exc
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ReferenceWatcherError(f"{field} artifact is missing or invalid")
    payload = bytes(value)
    if not payload:
        raise ReferenceWatcherError(f"{field} artifact is empty")
    return payload


def _verify_artifact(
    *,
    ref: str,
    digest: str,
    field: str,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None,
) -> bytes:
    _validate_artifact(ref, digest, field)
    payload = _resolved_bytes(artifact_resolver, ref, field)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ReferenceWatcherError(f"{field} artifact digest mismatch")
    return payload


def trusted_time_registry_entry_sha256(entry: Mapping[str, Any]) -> str:
    """Return the digest of registry metadata, excluding code and the digest."""
    if not isinstance(entry, Mapping):
        raise ReferenceWatcherError("trusted time verifier registry entry is invalid")
    metadata = {
        key: entry[key]
        for key in (
            "verifier_id",
            "verifier_version",
            "trust_anchor_sha256",
            "supported_kinds",
        )
        if key in entry
    }
    if set(metadata) != {
        "verifier_id",
        "verifier_version",
        "trust_anchor_sha256",
        "supported_kinds",
    }:
        raise ReferenceWatcherError("trusted time verifier registry metadata is invalid")
    return hashlib.sha256(_canonical(metadata)).hexdigest()


def _validate_trusted_time_registry_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ReferenceWatcherError("trusted time verifier registry entry is missing")
    if set(entry) != {
        "verifier_id",
        "verifier_version",
        "trust_anchor_sha256",
        "supported_kinds",
        "registry_entry_sha256",
        "verify",
    }:
        raise ReferenceWatcherError("trusted time verifier registry fields are invalid")
    normalized = dict(entry)
    _id(normalized["verifier_id"], "trusted_time_verifier_id")
    _text(normalized["verifier_version"], "trusted_time_verifier_version", maximum=256)
    _sha(normalized["trust_anchor_sha256"], "trusted_time_trust_anchor_sha256")
    supported = normalized["supported_kinds"]
    if (
        not isinstance(supported, list)
        or not supported
        or len(supported) > len(_TRUSTED_TIME_KINDS)
        or any(kind not in _TRUSTED_TIME_KINDS for kind in supported)
        or supported != sorted(set(supported))
    ):
        raise ReferenceWatcherError("trusted time verifier supported kinds are invalid")
    _sha(normalized["registry_entry_sha256"], "trusted_time_registry_entry_sha256")
    if normalized["registry_entry_sha256"] != trusted_time_registry_entry_sha256(
        normalized
    ):
        raise ReferenceWatcherError("trusted time verifier registry digest mismatch")
    if not callable(normalized["verify"]):
        raise ReferenceWatcherError("trusted time verifier is invalid")
    return normalized


def _fixture_compat_registry_entry(
    verifier: Callable[[str, str, bytes], bool],
) -> dict[str, Any]:
    entry = {
        "verifier_id": _FIXTURE_COMPAT_VERIFIER_ID,
        "verifier_version": _FIXTURE_COMPAT_VERIFIER_VERSION,
        "trust_anchor_sha256": _FIXTURE_COMPAT_TRUST_ANCHOR_SHA256,
        "supported_kinds": sorted(_TRUSTED_TIME_KINDS),
        "verify": verifier,
    }
    entry["registry_entry_sha256"] = trusted_time_registry_entry_sha256(entry)
    return entry


def _resolve_trusted_time_verifier(
    *,
    mode: str,
    kind: str,
    verifier: Callable[[str, str, bytes], bool] | None,
    resolver: Callable[[str, str], Mapping[str, Any] | None] | None,
    verifier_id: str | None,
    verifier_version: str | None,
    persisted_binding: Mapping[str, Any] | None = None,
) -> tuple[Callable[[str, str, bytes], bool], dict[str, Any]]:
    """Resolve and validate the registry entry before attestation verification."""
    persisted = dict(persisted_binding or {})
    resolved_id = verifier_id or persisted.get("verifier_id")
    resolved_version = verifier_version or persisted.get("verifier_version")
    if resolver is None:
        if mode == "live":
            raise ReferenceWatcherError(
                "live trusted time requires a registry verifier resolver"
            )
        if verifier is None:
            raise ReferenceWatcherError("trusted time requires an attestation verifier")
        entry = _fixture_compat_registry_entry(verifier)
    else:
        if not isinstance(resolved_id, str) or not isinstance(resolved_version, str):
            raise ReferenceWatcherError(
                "trusted time registry binding requires verifier id and version"
            )
        try:
            candidate = resolver(resolved_id, resolved_version)
        except Exception as exc:
            raise ReferenceWatcherError("trusted time verifier resolver failed") from exc
        entry = _validate_trusted_time_registry_entry(candidate)
        if (
            entry["verifier_id"] != resolved_id
            or entry["verifier_version"] != resolved_version
        ):
            raise ReferenceWatcherError("trusted time verifier registry identity mismatch")
    if kind not in entry["supported_kinds"]:
        raise ReferenceWatcherError("trusted time kind is unsupported by verifier")
    expected = {
        "verifier_id": entry["verifier_id"],
        "verifier_version": entry["verifier_version"],
        "trust_anchor_sha256": entry["trust_anchor_sha256"],
        "registry_entry_sha256": entry["registry_entry_sha256"],
    }
    for field, value in expected.items():
        persisted_key = {
            "verifier_id": "trusted_time_verifier_id",
            "verifier_version": "trusted_time_verifier_version",
            "trust_anchor_sha256": "trusted_time_trust_anchor_sha256",
            "registry_entry_sha256": "trusted_time_registry_entry_sha256",
        }[field]
        if persisted_key in persisted and persisted[persisted_key] != value:
            raise ReferenceWatcherError("trusted time verifier binding mismatch")
    return entry["verify"], entry


def _verify_trusted_time(
    *,
    kind: str,
    trusted_at: str,
    evidence: bytes,
    verifier: Callable[[str, str, bytes], bool] | None,
) -> None:
    if verifier is None:
        raise ReferenceWatcherError("trusted time requires an attestation verifier")
    try:
        verified = verifier(kind, trusted_at, evidence)
    except Exception as exc:
        raise ReferenceWatcherError("trusted time attestation verifier failed") from exc
    if verified is not True:
        raise ReferenceWatcherError("trusted time attestation is invalid")


def _resolve_expected_watch(
    observation: Mapping[str, Any],
    *,
    expected_watch: Mapping[str, Any] | None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None,
) -> dict[str, Any]:
    if expected_watch is None:
        if watch_resolver is None:
            raise ReferenceWatcherError("reference observation requires a trusted watch")
        try:
            expected_watch = watch_resolver(
                observation["watch_id"], observation["watch_revision"]
            )
        except Exception as exc:
            raise ReferenceWatcherError("trusted watch resolver failed") from exc
    if not isinstance(expected_watch, Mapping):
        raise ReferenceWatcherError("trusted watch is missing or invalid")
    normalized = _validate_watch(expected_watch)
    if _watch_digest(normalized) != observation["watch_sha256"]:
        raise ReferenceWatcherError("reference observation watch digest mismatch")
    expected_projection = {
        "watch_id": normalized["watch_id"],
        "watch_revision": normalized["watch_revision"],
        "source_id": normalized["source_id"],
        "source_ref": normalized["source_ref"],
        "source_identity_sha256": _source_identity(normalized),
        "authority_kind": normalized["authority_kind"],
        "baseline_revision": normalized["baseline_revision"],
        "baseline_sha256": normalized["baseline_sha256"],
        "valid_until": normalized["valid_until"],
        "affected_assertion_ids": normalized["assertion_ids"],
    }
    if any(observation[field] != value for field, value in expected_projection.items()):
        raise ReferenceWatcherError("reference observation does not match trusted watch")
    return normalized


def _validate_watch(watch: Any) -> dict[str, Any]:
    if not isinstance(watch, Mapping) or set(watch) != _WATCH_FIELDS:
        raise ReferenceWatcherError("watch fields are invalid")
    normalized = copy.deepcopy(dict(watch))
    _id(normalized["watch_id"], "watch_id")
    _id(normalized["source_id"], "source_id")
    _text(normalized["source_ref"], "source_ref")
    if normalized["authority_kind"] not in _AUTHORITIES:
        raise ReferenceWatcherError("authority_kind is invalid")
    if (
        type(normalized["watch_revision"]) is not int
        or normalized["watch_revision"] < 0
    ):
        raise ReferenceWatcherError("watch_revision is invalid")
    _text(normalized["baseline_revision"], "baseline_revision", maximum=512)
    _sha(normalized["baseline_sha256"], "baseline_sha256")
    _timestamp(normalized["valid_until"], "valid_until")
    normalized["assertion_ids"] = _ids(
        normalized["assertion_ids"], "assertion_ids", allow_empty=False
    )
    return normalized


def observe_reference(
    *,
    watch: Mapping[str, Any],
    mode: str,
    availability_status: str,
    observed_revision: str | None,
    observed_content: bytes | bytearray | memoryview | None,
    unavailability_evidence: bytes | bytearray | memoryview | None,
    trusted_time: Mapping[str, Any],
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    trusted_time_verifier_id: str | None = None,
    trusted_time_verifier_version: str | None = None,
) -> dict[str, Any]:
    """Create a hash-bound observation without fetching or reading the system clock."""
    normalized_watch = _validate_watch(watch)
    if mode not in {"live", "fixture"}:
        raise ReferenceWatcherError("mode is invalid")
    if not isinstance(trusted_time, Mapping) or not _TRUSTED_TIME_FIELDS.issubset(
        trusted_time
    ) or set(trusted_time) - _TRUSTED_TIME_FIELDS - {
        "verifier_id",
        "verifier_version",
    }:
        raise ReferenceWatcherError("trusted_time fields are invalid")
    time_kind = trusted_time["kind"]
    if time_kind not in _TRUSTED_TIME_KINDS:
        raise ReferenceWatcherError("trusted_time kind is invalid")
    if mode == "live" and time_kind == "fixture-clock":
        raise ReferenceWatcherError("live observations cannot use a fixture clock")
    trusted_at = trusted_time["trusted_at"]
    trusted_datetime = _timestamp(trusted_at, "trusted_at")
    time_evidence = _bytes(trusted_time["evidence"], "trusted_time.evidence")
    verifier, verifier_entry = _resolve_trusted_time_verifier(
        mode=mode,
        kind=time_kind,
        verifier=trusted_time_verifier,
        resolver=trusted_time_verifier_resolver,
        verifier_id=trusted_time_verifier_id or trusted_time.get("verifier_id"),
        verifier_version=trusted_time_verifier_version
        or trusted_time.get("verifier_version"),
    )
    _verify_trusted_time(
        kind=time_kind,
        trusted_at=trusted_at,
        evidence=time_evidence,
        verifier=verifier,
    )
    time_sha256 = hashlib.sha256(time_evidence).hexdigest()

    if availability_status == "available":
        observed_revision = _text(observed_revision, "observed_revision", maximum=512)
        content = _bytes(observed_content, "observed_content")
        if unavailability_evidence is not None:
            raise ReferenceWatcherError(
                "available observation cannot carry unavailability evidence"
            )
        evidence_sha256 = hashlib.sha256(content).hexdigest()
        observed_sha256: str | None = evidence_sha256
    elif availability_status == "unavailable":
        if observed_revision is not None or observed_content is not None:
            raise ReferenceWatcherError(
                "unavailable observation cannot carry current content"
            )
        unavailable = _bytes(unavailability_evidence, "unavailability_evidence")
        evidence_sha256 = hashlib.sha256(unavailable).hexdigest()
        observed_sha256 = None
    else:
        raise ReferenceWatcherError("availability_status is invalid")

    expires = _timestamp(normalized_watch["valid_until"], "valid_until")
    freshness_status = "expired" if trusted_datetime > expires else "current"
    if availability_status == "unavailable":
        outcome = "unavailable"
    elif (
        observed_revision != normalized_watch["baseline_revision"]
        or observed_sha256 != normalized_watch["baseline_sha256"]
    ):
        outcome = "changed"
    elif freshness_status == "expired":
        outcome = "expired"
    else:
        outcome = "unchanged"

    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_id": (
            f"observation/{normalized_watch['watch_id']}/"
            f"revision-{normalized_watch['watch_revision']}"
        ),
        "watch_id": normalized_watch["watch_id"],
        "watch_revision": normalized_watch["watch_revision"],
        "watch_sha256": _watch_digest(normalized_watch),
        "mode": mode,
        "source_id": normalized_watch["source_id"],
        "source_ref": normalized_watch["source_ref"],
        "source_identity_sha256": _source_identity(normalized_watch),
        "authority_kind": normalized_watch["authority_kind"],
        "baseline_revision": normalized_watch["baseline_revision"],
        "baseline_sha256": normalized_watch["baseline_sha256"],
        "availability_status": availability_status,
        "observed_revision": observed_revision,
        "observed_sha256": observed_sha256,
        "current_evidence_ref": _artifact_ref(evidence_sha256),
        "current_evidence_sha256": evidence_sha256,
        "valid_until": normalized_watch["valid_until"],
        "trusted_at": trusted_at,
        "trusted_time_kind": time_kind,
        "trusted_time_evidence_ref": _artifact_ref(time_sha256),
        "trusted_time_evidence_sha256": time_sha256,
        "trusted_time_verifier_id": verifier_entry["verifier_id"],
        "trusted_time_verifier_version": verifier_entry["verifier_version"],
        "trusted_time_trust_anchor_sha256": verifier_entry["trust_anchor_sha256"],
        "trusted_time_registry_entry_sha256": verifier_entry[
            "registry_entry_sha256"
        ],
        "freshness_status": freshness_status,
        "watch_outcome": outcome,
        "affected_assertion_ids": normalized_watch["assertion_ids"],
        "state_write_authority": False,
        "completion_authority": False,
        "provider_native_authority": False,
        "observation_sha256": "",
    }
    observation["observation_sha256"] = _unsigned_digest(
        observation, "observation_sha256"
    )
    artifacts = {
        observation["current_evidence_ref"]: (
            content if availability_status == "available" else unavailable
        ),
        observation["trusted_time_evidence_ref"]: time_evidence,
    }
    validate_reference_watch_observation(
        observation,
        expected_watch=normalized_watch,
        artifact_resolver=artifacts.get,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
    )
    return copy.deepcopy(observation)


def validate_reference_watch_observation(
    observation: Any,
    *,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
) -> None:
    if not isinstance(observation, Mapping) or set(observation) != _OBSERVATION_FIELDS:
        raise ReferenceWatcherError("reference watch observation fields are invalid")
    if observation["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        raise ReferenceWatcherError("reference watch observation version is invalid")
    for field in ("observation_id", "watch_id", "source_id"):
        _id(observation[field], field)
    if (
        type(observation["watch_revision"]) is not int
        or observation["watch_revision"] < 0
    ):
        raise ReferenceWatcherError("watch_revision is invalid")
    _sha(observation["watch_sha256"], "watch_sha256")
    if observation["mode"] not in {"live", "fixture"}:
        raise ReferenceWatcherError("mode is invalid")
    _text(observation["source_ref"], "source_ref")
    if observation["authority_kind"] not in _AUTHORITIES:
        raise ReferenceWatcherError("authority_kind is invalid")
    _sha(observation["source_identity_sha256"], "source_identity_sha256")
    if observation["source_identity_sha256"] != _source_identity(observation):
        raise ReferenceWatcherError("source identity digest mismatch")
    _text(observation["baseline_revision"], "baseline_revision", maximum=512)
    _sha(observation["baseline_sha256"], "baseline_sha256")
    availability = observation["availability_status"]
    observed_revision = observation["observed_revision"]
    observed_sha256 = observation["observed_sha256"]
    if availability == "available":
        _text(observed_revision, "observed_revision", maximum=512)
        _sha(observed_sha256, "observed_sha256")
        if observation["current_evidence_sha256"] != observed_sha256:
            raise ReferenceWatcherError(
                "current evidence does not bind observed content"
            )
    elif availability == "unavailable":
        if observed_revision is not None or observed_sha256 is not None:
            raise ReferenceWatcherError(
                "unavailable observation contains current content"
            )
    else:
        raise ReferenceWatcherError("availability_status is invalid")
    current_evidence = _verify_artifact(
        ref=observation["current_evidence_ref"],
        digest=observation["current_evidence_sha256"],
        field="current_evidence",
        artifact_resolver=artifact_resolver,
    )
    trusted = _timestamp(observation["trusted_at"], "trusted_at")
    expires = _timestamp(observation["valid_until"], "valid_until")
    if observation["trusted_time_kind"] not in _TRUSTED_TIME_KINDS:
        raise ReferenceWatcherError("trusted_time_kind is invalid")
    if (
        observation["mode"] == "live"
        and observation["trusted_time_kind"] == "fixture-clock"
    ):
        raise ReferenceWatcherError("live observations cannot use a fixture clock")
    trusted_time_evidence = _verify_artifact(
        ref=observation["trusted_time_evidence_ref"],
        digest=observation["trusted_time_evidence_sha256"],
        field="trusted_time_evidence",
        artifact_resolver=artifact_resolver,
    )
    verifier, _ = _resolve_trusted_time_verifier(
        mode=observation["mode"],
        kind=observation["trusted_time_kind"],
        verifier=trusted_time_verifier,
        resolver=trusted_time_verifier_resolver,
        verifier_id=observation["trusted_time_verifier_id"],
        verifier_version=observation["trusted_time_verifier_version"],
        persisted_binding={
            "trusted_time_verifier_id": observation[
                "trusted_time_verifier_id"
            ],
            "trusted_time_verifier_version": observation[
                "trusted_time_verifier_version"
            ],
            "trusted_time_trust_anchor_sha256": observation[
                "trusted_time_trust_anchor_sha256"
            ],
            "trusted_time_registry_entry_sha256": observation[
                "trusted_time_registry_entry_sha256"
            ],
        },
    )
    _verify_trusted_time(
        kind=observation["trusted_time_kind"],
        trusted_at=observation["trusted_at"],
        evidence=trusted_time_evidence,
        verifier=verifier,
    )
    if availability == "available" and hashlib.sha256(current_evidence).hexdigest() != observed_sha256:
        raise ReferenceWatcherError("observed content does not match current evidence")
    expected_freshness = "expired" if trusted > expires else "current"
    if observation["freshness_status"] != expected_freshness:
        raise ReferenceWatcherError("freshness status is invalid")
    if availability == "unavailable":
        expected_outcome = "unavailable"
    elif (
        observed_revision != observation["baseline_revision"]
        or observed_sha256 != observation["baseline_sha256"]
    ):
        expected_outcome = "changed"
    elif expected_freshness == "expired":
        expected_outcome = "expired"
    else:
        expected_outcome = "unchanged"
    if (
        observation["watch_outcome"] not in _OUTCOMES
        or observation["watch_outcome"] != expected_outcome
    ):
        raise ReferenceWatcherError("watch outcome is invalid")
    _ids(
        observation["affected_assertion_ids"],
        "affected_assertion_ids",
        allow_empty=False,
    )
    if any(
        observation[field] is not False
        for field in (
            "state_write_authority",
            "completion_authority",
            "provider_native_authority",
        )
    ):
        raise ReferenceWatcherError("reference observations have no authority")
    _sha(observation["observation_sha256"], "observation_sha256")
    if observation["observation_sha256"] != _unsigned_digest(
        observation, "observation_sha256"
    ):
        raise ReferenceWatcherError("observation digest mismatch")
    _resolve_expected_watch(
        observation,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
    )


def canonical_reference_watch_observation_bytes(
    observation: Mapping[str, Any],
    *,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
) -> bytes:
    validate_reference_watch_observation(
        observation,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
    )
    return _canonical(dict(observation))


def _matching_current_evidence(
    replacement: Mapping[str, Any], observation: Mapping[str, Any]
) -> bool:
    return any(
        evidence["authority_kind"] == observation["authority_kind"]
        and evidence["source_ref"] == observation["source_ref"]
        and evidence["revision"] == observation["observed_revision"]
        and evidence["sha256"] == observation["observed_sha256"]
        for evidence in replacement["evidence"]
    )


def _verify_replacement_assertion(
    replacement: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None,
) -> None:
    if evidence_resolver is None:
        raise ReferenceWatcherError("replacement evidence requires a resolver")
    if artifact_resolver is None:
        raise ReferenceWatcherError("replacement retrieval receipt requires a resolver")
    try:
        validate_assertion_provenance(
            dict(replacement), current_time=replacement.get("asserted_at")
        )
    except (AssertionProvenanceError, AttributeError, TypeError) as exc:
        raise ReferenceWatcherError("replacement assertion provenance is invalid") from exc
    if not replacement["bearing"] or not _matching_current_evidence(
        replacement, observation
    ):
        raise ReferenceWatcherError("replacement assertion does not bind current evidence")

    from .retrieval_routing import RetrievalContractError, validate_retrieval_receipt

    for evidence in replacement["evidence"]:
        try:
            resolved = evidence_resolver(evidence["source_ref"], evidence["revision"])
        except Exception as exc:
            raise ReferenceWatcherError("replacement evidence resolver failed") from exc
        if not isinstance(resolved, (bytes, bytearray, memoryview)):
            raise ReferenceWatcherError("replacement evidence is missing or invalid")
        if hashlib.sha256(bytes(resolved)).hexdigest() != evidence["sha256"]:
            raise ReferenceWatcherError("replacement evidence digest mismatch")

        receipt_ref = evidence["retrieval_receipt_ref"]
        receipt_digest = receipt_ref.rsplit("/", 1)[-1]
        receipt_bytes = _verify_artifact(
            ref=receipt_ref,
            digest=receipt_digest,
            field="replacement_retrieval_receipt",
            artifact_resolver=artifact_resolver,
        )
        try:
            receipt = json.loads(receipt_bytes)
            validate_retrieval_receipt(receipt)
        except (json.JSONDecodeError, UnicodeDecodeError, RetrievalContractError, TypeError) as exc:
            raise ReferenceWatcherError("replacement retrieval receipt is invalid") from exc
        if not any(
            item["source_ref"] == evidence["source_ref"]
            and item["revision"] == evidence["revision"]
            and item["sha256"] == evidence["sha256"]
            for item in receipt["evidence"]
        ):
            raise ReferenceWatcherError(
                "replacement retrieval receipt does not bind evidence"
            )


def _resolve_expected_observation(
    decision: Mapping[str, Any],
    *,
    expected_observation: Mapping[str, Any] | None,
    observation_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None,
) -> Mapping[str, Any]:
    if expected_observation is None:
        if observation_resolver is None:
            raise ReferenceWatcherError("reference decision requires a trusted observation")
        try:
            expected_observation = observation_resolver(
                decision["observation_id"], decision["observation_sha256"]
            )
        except Exception as exc:
            raise ReferenceWatcherError("trusted observation resolver failed") from exc
    if not isinstance(expected_observation, Mapping):
        raise ReferenceWatcherError("trusted observation is missing or invalid")
    return expected_observation


def _resolve_replacement_assertion(
    decision: Mapping[str, Any],
    *,
    expected_replacement_assertion: Mapping[str, Any] | None,
    replacement_assertion_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None,
) -> Mapping[str, Any]:
    if expected_replacement_assertion is None:
        if replacement_assertion_resolver is None:
            raise ReferenceWatcherError(
                "current reverification requires a trusted replacement assertion"
            )
        try:
            expected_replacement_assertion = replacement_assertion_resolver(
                decision["replacement_assertion_id"],
                decision["replacement_record_sha256"],
            )
        except Exception as exc:
            raise ReferenceWatcherError("replacement assertion resolver failed") from exc
    if not isinstance(expected_replacement_assertion, Mapping):
        raise ReferenceWatcherError("replacement assertion is missing or invalid")
    return expected_replacement_assertion


def decide_reference_watch(
    observation: Mapping[str, Any],
    *,
    replacement_assertion: Mapping[str, Any] | None = None,
    supersedes_assertion_ids: Sequence[str] | None = None,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None = None,
) -> dict[str, Any]:
    """Derive assertion admission; the returned receipt cannot mutate or complete State."""
    validate_reference_watch_observation(
        observation,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
    )
    affected = list(observation["affected_assertion_ids"])
    supersedes = (
        [] if supersedes_assertion_ids is None else sorted(supersedes_assertion_ids)
    )
    if len(supersedes) != len(set(supersedes)):
        raise ReferenceWatcherError("supersedes_assertion_ids must be unique")
    replacement_id: str | None = None
    replacement_digest: str | None = None

    if replacement_assertion is not None:
        if (
            observation["watch_outcome"] != "changed"
            or observation["freshness_status"] != "current"
        ):
            raise ReferenceWatcherError(
                "only a current changed observation can be reverified"
            )
        if supersedes != affected:
            raise ReferenceWatcherError(
                "reverification must supersede every affected assertion"
            )
        _verify_replacement_assertion(
            replacement_assertion,
            observation,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        replacement_id = replacement_assertion["assertion_id"]
        if replacement_id in affected:
            raise ReferenceWatcherError("replacement assertion must use a new ID")
        asserted_at = _timestamp(replacement_assertion["asserted_at"], "asserted_at")
        if asserted_at < _timestamp(observation["trusted_at"], "trusted_at"):
            raise ReferenceWatcherError(
                "replacement assertion predates the watch observation"
            )
        replacement_digest = replacement_assertion["record_sha256"]
        assertion_status = "current"
        decision_name = "allow-current"
        reason = "current-reverification"
        stale = affected
        quarantined: list[str] = []
        eligible = [replacement_id]
    elif supersedes:
        raise ReferenceWatcherError("supersedes requires a replacement assertion")
    elif observation["watch_outcome"] == "unchanged":
        assertion_status = "current"
        decision_name = "allow-current"
        reason = "source-unchanged"
        stale = []
        quarantined = []
        eligible = affected
    elif observation["watch_outcome"] == "changed":
        assertion_status = "stale"
        decision_name = "reverify-required"
        reason = "source-changed"
        stale = affected
        quarantined = []
        eligible = []
    else:
        assertion_status = "quarantined"
        decision_name = "reverify-required"
        reason = f"source-{observation['watch_outcome']}"
        stale = []
        quarantined = affected
        eligible = []

    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": f"decision/{observation['observation_id']}",
        "observation_id": observation["observation_id"],
        "observation_sha256": observation["observation_sha256"],
        "watch_id": observation["watch_id"],
        "source_id": observation["source_id"],
        "source_identity_sha256": observation["source_identity_sha256"],
        "watch_outcome": observation["watch_outcome"],
        "assertion_status": assertion_status,
        "decision": decision_name,
        "reason_code": reason,
        "affected_assertion_ids": affected,
        "stale_assertion_ids": stale,
        "quarantined_assertion_ids": quarantined,
        "superseded_assertion_ids": supersedes,
        "completion_eligible_assertion_ids": eligible,
        "replacement_assertion_id": replacement_id,
        "replacement_record_sha256": replacement_digest,
        "evaluated_at": (
            replacement_assertion["asserted_at"]
            if replacement_assertion is not None
            else observation["trusted_at"]
        ),
        "state_write_authority": False,
        "completion_authority": False,
        "provider_native_authority": False,
        "decision_sha256": "",
    }
    decision["decision_sha256"] = _unsigned_digest(decision, "decision_sha256")
    validate_reference_watch_decision(
        decision,
        expected_observation=observation,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
        expected_replacement_assertion=replacement_assertion,
        evidence_resolver=evidence_resolver,
    )
    return copy.deepcopy(decision)


def validate_reference_watch_decision(
    decision: Any,
    *,
    expected_observation: Mapping[str, Any] | None = None,
    observation_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_replacement_assertion: Mapping[str, Any] | None = None,
    replacement_assertion_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None = None,
) -> None:
    if not isinstance(decision, Mapping) or set(decision) != _DECISION_FIELDS:
        raise ReferenceWatcherError("reference watch decision fields are invalid")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise ReferenceWatcherError("reference watch decision version is invalid")
    for field in ("decision_id", "observation_id", "watch_id", "source_id"):
        _id(decision[field], field)
    for field in ("observation_sha256", "source_identity_sha256"):
        _sha(decision[field], field)
    if decision["watch_outcome"] not in _OUTCOMES:
        raise ReferenceWatcherError("watch_outcome is invalid")
    if decision["assertion_status"] not in {"current", "stale", "quarantined"}:
        raise ReferenceWatcherError("assertion_status is invalid")
    if decision["decision"] not in {"allow-current", "reverify-required"}:
        raise ReferenceWatcherError("decision is invalid")
    if decision["reason_code"] not in {
        "source-unchanged",
        "source-changed",
        "source-unavailable",
        "source-expired",
        "current-reverification",
    }:
        raise ReferenceWatcherError("reason_code is invalid")
    list_fields = (
        "affected_assertion_ids",
        "stale_assertion_ids",
        "quarantined_assertion_ids",
        "superseded_assertion_ids",
        "completion_eligible_assertion_ids",
    )
    lists = {field: _ids(decision[field], field) for field in list_fields}
    if not lists["affected_assertion_ids"]:
        raise ReferenceWatcherError("affected_assertion_ids cannot be empty")
    replacement_id = decision["replacement_assertion_id"]
    replacement_digest = decision["replacement_record_sha256"]
    if replacement_id is None:
        if replacement_digest is not None:
            raise ReferenceWatcherError("replacement digest requires an assertion")
    else:
        _id(replacement_id, "replacement_assertion_id")
        _sha(replacement_digest, "replacement_record_sha256")
    _timestamp(decision["evaluated_at"], "evaluated_at")
    if any(
        decision[field] is not False
        for field in (
            "state_write_authority",
            "completion_authority",
            "provider_native_authority",
        )
    ):
        raise ReferenceWatcherError("reference watch decisions have no authority")
    _sha(decision["decision_sha256"], "decision_sha256")
    if decision["decision_sha256"] != _unsigned_digest(decision, "decision_sha256"):
        raise ReferenceWatcherError("decision digest mismatch")

    observation = _resolve_expected_observation(
        decision,
        expected_observation=expected_observation,
        observation_resolver=observation_resolver,
    )
    validate_reference_watch_observation(
        observation,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
    )
    projection = {
        "decision_id": f"decision/{observation['observation_id']}",
        "observation_id": observation["observation_id"],
        "observation_sha256": observation["observation_sha256"],
        "watch_id": observation["watch_id"],
        "source_id": observation["source_id"],
        "source_identity_sha256": observation["source_identity_sha256"],
        "watch_outcome": observation["watch_outcome"],
        "affected_assertion_ids": observation["affected_assertion_ids"],
    }
    if any(decision[field] != value for field, value in projection.items()):
        raise ReferenceWatcherError("reference decision does not match observation")

    affected = list(observation["affected_assertion_ids"])
    outcome = observation["watch_outcome"]
    if outcome == "unchanged":
        expected = {
            "assertion_status": "current",
            "decision": "allow-current",
            "reason_code": "source-unchanged",
            "stale_assertion_ids": [],
            "quarantined_assertion_ids": [],
            "superseded_assertion_ids": [],
            "completion_eligible_assertion_ids": affected,
            "replacement_assertion_id": None,
            "replacement_record_sha256": None,
            "evaluated_at": observation["trusted_at"],
        }
    elif outcome == "changed" and replacement_id is not None:
        replacement = _resolve_replacement_assertion(
            decision,
            expected_replacement_assertion=expected_replacement_assertion,
            replacement_assertion_resolver=replacement_assertion_resolver,
        )
        _verify_replacement_assertion(
            replacement,
            observation,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        if (
            replacement["assertion_id"] != replacement_id
            or replacement["record_sha256"] != replacement_digest
        ):
            raise ReferenceWatcherError("reference decision replacement binding mismatch")
        if replacement_id in affected:
            raise ReferenceWatcherError("replacement assertion must use a new ID")
        if _timestamp(replacement["asserted_at"], "asserted_at") < _timestamp(
            observation["trusted_at"], "trusted_at"
        ):
            raise ReferenceWatcherError(
                "replacement assertion predates the watch observation"
            )
        expected = {
            "assertion_status": "current",
            "decision": "allow-current",
            "reason_code": "current-reverification",
            "stale_assertion_ids": affected,
            "quarantined_assertion_ids": [],
            "superseded_assertion_ids": affected,
            "completion_eligible_assertion_ids": [replacement_id],
            "replacement_assertion_id": replacement_id,
            "replacement_record_sha256": replacement_digest,
            "evaluated_at": replacement["asserted_at"],
        }
    elif outcome == "changed":
        expected = {
            "assertion_status": "stale",
            "decision": "reverify-required",
            "reason_code": "source-changed",
            "stale_assertion_ids": affected,
            "quarantined_assertion_ids": [],
            "superseded_assertion_ids": [],
            "completion_eligible_assertion_ids": [],
            "replacement_assertion_id": None,
            "replacement_record_sha256": None,
            "evaluated_at": observation["trusted_at"],
        }
    else:
        expected = {
            "assertion_status": "quarantined",
            "decision": "reverify-required",
            "reason_code": f"source-{outcome}",
            "stale_assertion_ids": [],
            "quarantined_assertion_ids": affected,
            "superseded_assertion_ids": [],
            "completion_eligible_assertion_ids": [],
            "replacement_assertion_id": None,
            "replacement_record_sha256": None,
            "evaluated_at": observation["trusted_at"],
        }
    if any(decision[field] != value for field, value in expected.items()):
        raise ReferenceWatcherError("reference watch decision is inconsistent")


def canonical_reference_watch_decision_bytes(
    decision: Mapping[str, Any],
    *,
    expected_observation: Mapping[str, Any] | None = None,
    observation_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_replacement_assertion: Mapping[str, Any] | None = None,
    replacement_assertion_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None = None,
) -> bytes:
    validate_reference_watch_decision(
        decision,
        expected_observation=expected_observation,
        observation_resolver=observation_resolver,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
        expected_replacement_assertion=expected_replacement_assertion,
        replacement_assertion_resolver=replacement_assertion_resolver,
        evidence_resolver=evidence_resolver,
    )
    return _canonical(dict(decision))


def assertion_is_completion_eligible(
    decision: Mapping[str, Any],
    assertion_id: str,
    *,
    expected_observation: Mapping[str, Any] | None = None,
    observation_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_watch: Mapping[str, Any] | None = None,
    watch_resolver: Callable[[str, int], Mapping[str, Any] | None] | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    expected_replacement_assertion: Mapping[str, Any] | None = None,
    replacement_assertion_resolver: Callable[
        [str, str], Mapping[str, Any] | None
    ]
    | None = None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None = None,
) -> bool:
    """Return watcher admission only; State MCP remains the completion authority."""
    validate_reference_watch_decision(
        decision,
        expected_observation=expected_observation,
        observation_resolver=observation_resolver,
        expected_watch=expected_watch,
        watch_resolver=watch_resolver,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
        expected_replacement_assertion=expected_replacement_assertion,
        replacement_assertion_resolver=replacement_assertion_resolver,
        evidence_resolver=evidence_resolver,
    )
    _id(assertion_id, "assertion_id")
    return assertion_id in decision["completion_eligible_assertion_ids"]
