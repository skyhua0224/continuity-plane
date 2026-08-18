"""Read-only Decision, Constraint, and Evidence views over signed State."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .assertion_provenance import (
    AssertionProvenanceError,
    validate_assertion_provenance,
)
from .claim_evidence_gate import (
    ClaimEvidenceError,
    evaluate_claim_evidence_gate,
    validate_claim_evidence_claim,
    validate_claim_evidence_verdict,
)
from .external_state_provider import (
    ExternalStateProjectionError,
    HMACExternalStateProjectionSigner,
    validate_external_state_projection,
)

DECISION_EVIDENCE_PROJECTION_SCHEMA_VERSION = (
    "context.decision-evidence-projection/v1alpha1"
)
PROVENANCE_BUNDLE_SCHEMA_VERSION = (
    "context.decision-evidence-provenance-bundle/v1alpha1"
)

_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "approval_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}
_TERMINAL_WORK_STATUSES = {"rejected", "reverted", "superseded"}
_TERMINAL_IDEA_STATUSES = {"expired", "rejected", "superseded"}
_MAX_ITEMS = 10_000
_MAX_EVIDENCE_REFERENCES = 50_000
_MAX_PROVENANCE_BINDINGS = 50_000
_MAX_PROVENANCE_NESTED_ITEMS = 50_000
_MAX_IDENTIFIER_LENGTH = 1024
_MAX_TEXT_LENGTH = 4096
_MAX_PROVENANCE_TEXT_LENGTH = 16_384


class DecisionEvidenceProjectionError(ValueError):
    """Raised when a Decision or Evidence projection cannot be trusted."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DecisionEvidenceProjectionError(
            "projection is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise DecisionEvidenceProjectionError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionEvidenceProjectionError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionEvidenceProjectionError(f"{field} must include timezone")
    return parsed


def _bounded_string(value: Any, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DecisionEvidenceProjectionError(f"{field} exceeds the projection contract")


def _reference_state(
    object_kind: str,
    item: dict[str, Any],
    *,
    current_decision_ids: set[str],
    active_constraint_ids: set[str],
) -> str:
    if object_kind == "decision":
        if item["decision_id"] in current_decision_ids:
            return "current"
        return "candidate" if item["status"] == "proposed" else "superseded"
    if object_kind == "constraint":
        if item["constraint_id"] in active_constraint_ids:
            return "current"
        return (
            "superseded"
            if item["status"] in {"rejected", "superseded"}
            else "historical"
        )
    if object_kind == "work":
        return (
            "superseded"
            if item["status"] in _TERMINAL_WORK_STATUSES
            else "current"
        )
    if object_kind == "idea":
        if item["status"] in _TERMINAL_IDEA_STATUSES:
            return "superseded"
        return (
            "candidate"
            if item["status"] in {"candidate", "parked", "proposed"}
            else "current"
        )
    if object_kind == "blocker":
        if item["status"] == "open":
            return "current"
        return "superseded" if item["status"] == "superseded" else "historical"
    if object_kind == "effect":
        if item["status"] == "planned":
            return "candidate"
        if item["status"] in {"authorized", "started"}:
            return "current"
        return "historical"
    if object_kind == "experiment-promotion":
        return "candidate" if item["kind"] == "proposed" else "current"
    if object_kind == "correction-protection":
        if item["status"] == "active":
            return "current"
        return "superseded" if item["status"] == "superseded" else "historical"
    if object_kind == "idea-review":
        return "historical"
    return "current"


def _iter_evidence_references(
    snapshot: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    current_decision_ids = set(snapshot["project"]["current_decision_ids"])
    active_constraint_ids = set(snapshot["project"]["active_constraint_ids"])
    groups = (
        ("works", "work", "work_id", "status"),
        ("ideas", "idea", "idea_id", "status"),
        ("decisions", "decision", "decision_id", "status"),
        ("constraints", "constraint", "constraint_id", "status"),
        ("blockers", "blocker", "blocker_id", "status"),
        ("effects", "effect", "effect_id", "status"),
        (
            "idea_relationships",
            "idea-relationship",
            "relationship_id",
            "relationship_kind",
        ),
        ("idea_reviews", "idea-review", "review_id", "decision"),
    )
    for collection, object_kind, id_field, status_field in groups:
        for item in snapshot.get(collection, []):
            state = _reference_state(
                object_kind,
                item,
                current_decision_ids=current_decision_ids,
                active_constraint_ids=active_constraint_ids,
            )
            for evidence_id in item.get("evidence_ids", []):
                yield evidence_id, {
                    "object_kind": object_kind,
                    "object_id": item[id_field],
                    "reference_path": "evidence_ids",
                    "object_status": item[status_field],
                    "reference_state": state,
                }

    for item in snapshot.get("correction_protections", []):
        state = _reference_state(
            "correction-protection",
            item,
            current_decision_ids=current_decision_ids,
            active_constraint_ids=active_constraint_ids,
        )
        for field in ("evidence_ids", "release_evidence_ids"):
            for evidence_id in item[field]:
                yield evidence_id, {
                    "object_kind": "correction-protection",
                    "object_id": item["protection_id"],
                    "reference_path": field,
                    "object_status": item["status"],
                    "reference_state": state,
                }

    for item in snapshot.get("experiment_promotions", []):
        state = _reference_state(
            "experiment-promotion",
            item,
            current_decision_ids=current_decision_ids,
            active_constraint_ids=active_constraint_ids,
        )
        for criterion, evidence_ids in item["criterion_evidence"].items():
            for evidence_id in evidence_ids:
                yield evidence_id, {
                    "object_kind": "experiment-promotion",
                    "object_id": item["promotion_id"],
                    "reference_path": f"criterion_evidence.{criterion}",
                    "object_status": item["kind"],
                    "reference_state": state,
                }


def _validate_capacity(snapshot: dict[str, Any]) -> None:
    collections = (
        "works",
        "ideas",
        "decisions",
        "constraints",
        "evidence",
        "blockers",
        "effects",
        "experiment_promotions",
        "idea_relationships",
        "idea_reviews",
        "correction_protections",
    )
    if any(len(snapshot.get(collection, [])) > _MAX_ITEMS for collection in collections):
        raise DecisionEvidenceProjectionError("source object count exceeds the projection contract")
    for decision in snapshot["decisions"]:
        _bounded_string(decision["decision_id"], "decision_id", _MAX_IDENTIFIER_LENGTH)
        _bounded_string(decision["work_id"], "decision.work_id", _MAX_IDENTIFIER_LENGTH)
        _bounded_string(decision["statement"], "decision.statement", _MAX_TEXT_LENGTH)
    for constraint in snapshot["constraints"]:
        _bounded_string(
            constraint["constraint_id"], "constraint_id", _MAX_IDENTIFIER_LENGTH
        )
        _bounded_string(
            constraint["statement"], "constraint.statement", _MAX_TEXT_LENGTH
        )
    for evidence in snapshot["evidence"]:
        _bounded_string(evidence["evidence_id"], "evidence_id", _MAX_IDENTIFIER_LENGTH)
        _bounded_string(evidence["artifact_ref"], "evidence.artifact_ref", _MAX_TEXT_LENGTH)
    for reference_count, (evidence_id, reference) in enumerate(
        _iter_evidence_references(snapshot), start=1
    ):
        if reference_count > _MAX_EVIDENCE_REFERENCES:
            raise DecisionEvidenceProjectionError(
                "evidence references exceed the projection contract"
            )
        _bounded_string(evidence_id, "evidence reference", _MAX_IDENTIFIER_LENGTH)
        _bounded_string(
            reference["object_id"], "evidence reference object", _MAX_IDENTIFIER_LENGTH
        )
        _bounded_string(
            reference["reference_path"], "evidence reference path", _MAX_TEXT_LENGTH
        )


def _validate_chronology(snapshot: dict[str, Any]) -> None:
    snapshot_time = _timestamp(
        snapshot["project"]["updated_at"], "project.updated_at"
    )
    decisions = {item["decision_id"]: item for item in snapshot["decisions"]}
    for decision in snapshot["decisions"]:
        decided_at = _timestamp(decision["decided_at"], "decision.decided_at")
        if decided_at > snapshot_time:
            raise DecisionEvidenceProjectionError("Decision postdates the State snapshot")
        supersedes = decision["supersedes_decision_id"]
        if supersedes is not None:
            previous = decisions[supersedes]
            if decision["work_id"] != previous["work_id"]:
                raise DecisionEvidenceProjectionError(
                    "Decision supersedes crosses Work identity"
                )
            if decided_at < _timestamp(previous["decided_at"], "decision.decided_at"):
                raise DecisionEvidenceProjectionError(
                    "Decision supersedes chronology is reversed"
                )
    for evidence in snapshot["evidence"]:
        evidence_observed = _timestamp(
            evidence["observed_at"], "evidence.observed_at"
        )
        if evidence_observed > snapshot_time:
            raise DecisionEvidenceProjectionError("Evidence postdates the State snapshot")
        if evidence["verified_at"] is not None:
            verified = _timestamp(evidence["verified_at"], "evidence.verified_at")
            if verified < evidence_observed:
                raise DecisionEvidenceProjectionError(
                    "Evidence verification precedes observation"
                )
            if verified > snapshot_time:
                raise DecisionEvidenceProjectionError(
                    "Evidence verification postdates the State snapshot"
                )


def _collect_references(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = {
        item["evidence_id"]: [] for item in snapshot["evidence"]
    }
    for evidence_id, reference in _iter_evidence_references(snapshot):
        references[evidence_id].append(reference)
    for values in references.values():
        values.sort(
            key=lambda item: (
                item["object_kind"],
                item["object_id"],
                item["reference_path"],
            )
        )
    return references


def _display_status(
    evidence: dict[str, Any], references: list[dict[str, Any]]
) -> str:
    if evidence["validity"] == "rejected":
        return "rejected"
    if evidence["validity"] == "stale":
        return "stale"
    if not references:
        return "unreferenced"
    if all(item["reference_state"] == "superseded" for item in references):
        return "superseded"
    if evidence["validity"] == "candidate" or all(
        item["reference_state"] == "candidate" for item in references
    ):
        return "candidate"
    return "current"


def _claim_digest(claim: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(claim)).hexdigest()


def _validate_provenance_preflight(value: Any) -> None:
    nested_items = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 8:
            raise DecisionEvidenceProjectionError(
                "provenance bundle nesting exceeds the contract"
            )
        if isinstance(item, dict):
            if len(item) > 64:
                raise DecisionEvidenceProjectionError(
                    "provenance object fields exceed the contract"
                )
            for key, child in item.items():
                _bounded_string(key, "provenance field", _MAX_IDENTIFIER_LENGTH)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            nested_items += len(item)
            if nested_items > _MAX_PROVENANCE_NESTED_ITEMS:
                raise DecisionEvidenceProjectionError(
                    "provenance nested items exceed the contract"
                )
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > _MAX_PROVENANCE_TEXT_LENGTH:
                raise DecisionEvidenceProjectionError(
                    "provenance text exceeds the contract"
                )
        elif item is not None and type(item) not in {bool, int, float}:
            raise DecisionEvidenceProjectionError(
                "provenance bundle value is not canonical JSON"
            )


def _object_for_binding(
    snapshot: dict[str, Any], object_kind: str, object_id: str
) -> dict[str, Any]:
    definitions = {
        "decision": ("decisions", "decision_id"),
        "constraint": ("constraints", "constraint_id"),
        "work": ("works", "work_id"),
    }
    if object_kind not in definitions:
        raise DecisionEvidenceProjectionError("provenance object kind is unsupported")
    collection, id_field = definitions[object_kind]
    try:
        return next(item for item in snapshot[collection] if item[id_field] == object_id)
    except StopIteration as exc:
        raise DecisionEvidenceProjectionError(
            "provenance object does not exist in State"
        ) from exc


def _validate_object_claim_binding(
    *,
    object_kind: str,
    obj: dict[str, Any],
    claim: dict[str, Any],
    typed_evidence_id: str,
) -> None:
    if typed_evidence_id not in obj["evidence_ids"]:
        raise DecisionEvidenceProjectionError(
            "provenance evidence is not referenced by its State object"
        )
    if object_kind == "decision":
        valid = (
            claim["claim_kind"] == "decision"
            and claim["work_id"] == obj["work_id"]
            and claim["statement"] == obj["statement"]
        )
    elif object_kind == "constraint":
        valid = (
            claim["claim_kind"] == "constraint"
            and claim["work_id"] in obj["scope_work_ids"]
            and claim["statement"] == obj["statement"]
        )
    else:
        valid = (
            claim["claim_kind"] in {"completion", "path", "verification"}
            and claim["work_id"] == obj["work_id"]
        )
    if not valid:
        raise DecisionEvidenceProjectionError(
            "provenance claim does not bind its State object"
        )


def _validate_provenance_bundle(
    bundle: dict[str, Any],
    *,
    source: dict[str, Any],
    snapshot: dict[str, Any],
    observed_at: str,
    root: str | Path | None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None,
) -> tuple[
    dict[str, list[dict[str, Any]]], set[tuple[str, str]], str
]:
    fields = {
        "schema_version",
        "project_id",
        "state_revision",
        "state_sha256",
        "source_projection_sha256",
        "assertion_records",
        "claims",
        "claim_verdicts",
        "bindings",
        "bundle_sha256",
    }
    if not isinstance(bundle, dict) or set(bundle) != fields:
        raise DecisionEvidenceProjectionError("provenance bundle fields are invalid")
    if bundle["schema_version"] != PROVENANCE_BUNDLE_SCHEMA_VERSION:
        raise DecisionEvidenceProjectionError("provenance bundle version is invalid")
    for field in ("project_id", "state_revision", "state_sha256"):
        if bundle[field] != source[field]:
            raise DecisionEvidenceProjectionError(
                "provenance bundle State identity mismatch"
            )
    if bundle["source_projection_sha256"] != source["projection_sha256"]:
        raise DecisionEvidenceProjectionError(
            "provenance bundle source projection mismatch"
        )
    for field in ("assertion_records", "claims", "claim_verdicts"):
        if not isinstance(bundle[field], list) or len(bundle[field]) > _MAX_ITEMS:
            raise DecisionEvidenceProjectionError(
                "provenance bundle object count exceeds the contract"
            )
    if (
        not isinstance(bundle["bindings"], list)
        or len(bundle["bindings"]) > _MAX_PROVENANCE_BINDINGS
    ):
        raise DecisionEvidenceProjectionError(
            "provenance bindings exceed the contract"
        )
    _validate_provenance_preflight(bundle)
    unsigned = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if bundle["bundle_sha256"] != _digest(unsigned):
        raise DecisionEvidenceProjectionError("provenance bundle digest mismatch")

    assertions: dict[str, dict[str, Any]] = {}
    for record in bundle["assertion_records"]:
        try:
            validate_assertion_provenance(
                record,
                current_time=observed_at,
                root=root,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )
        except AssertionProvenanceError as exc:
            raise DecisionEvidenceProjectionError(
                "assertion provenance is invalid"
            ) from exc
        if _timestamp(record["asserted_at"], "assertion.asserted_at") > _timestamp(
            observed_at, "observed_at"
        ):
            raise DecisionEvidenceProjectionError(
                "assertion provenance postdates the projection"
            )
        if record["assertion_id"] in assertions:
            raise DecisionEvidenceProjectionError("assertion IDs must be unique")
        assertions[record["assertion_id"]] = record

    claims: dict[str, dict[str, Any]] = {}
    for claim in bundle["claims"]:
        try:
            validate_claim_evidence_claim(claim)
        except ClaimEvidenceError as exc:
            raise DecisionEvidenceProjectionError("claim evidence claim is invalid") from exc
        if claim["claim_id"] in claims:
            raise DecisionEvidenceProjectionError("claim IDs must be unique")
        claims[claim["claim_id"]] = claim

    verdicts: dict[str, dict[str, Any]] = {}
    for verdict in bundle["claim_verdicts"]:
        try:
            validate_claim_evidence_verdict(verdict)
        except ClaimEvidenceError as exc:
            raise DecisionEvidenceProjectionError("claim evidence verdict is invalid") from exc
        if _timestamp(verdict["evaluated_at"], "verdict.evaluated_at") > _timestamp(
            observed_at, "observed_at"
        ):
            raise DecisionEvidenceProjectionError(
                "claim evidence verdict postdates the projection"
            )
        claim = claims.get(verdict["claim_id"])
        if (
            claim is None
            or verdict["claim_kind"] != claim["claim_kind"]
            or verdict["claim_sha256"] != _claim_digest(claim)
        ):
            raise DecisionEvidenceProjectionError(
                "claim evidence verdict binding is invalid"
            )
        declared_records = [
            assertions[assertion_id]
            for assertion_id in claim["evidence_assertion_ids"]
            if assertion_id in assertions
        ]
        try:
            replayed = evaluate_claim_evidence_gate(
                claim,
                evidence_records=declared_records,
                current_time=verdict["evaluated_at"],
                root=root,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )
        except ClaimEvidenceError as exc:
            raise DecisionEvidenceProjectionError(
                "claim evidence verdict cannot be replayed"
            ) from exc
        if _canonical_bytes(replayed) != _canonical_bytes(verdict):
            raise DecisionEvidenceProjectionError(
                "claim evidence verdict replay mismatch"
            )
        if verdict["claim_id"] in verdicts:
            raise DecisionEvidenceProjectionError(
                "claim evidence verdict IDs must be unique"
            )
        verdicts[verdict["claim_id"]] = verdict

    typed_evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
    provenance: dict[str, list[dict[str, Any]]] = {
        evidence_id: [] for evidence_id in typed_evidence
    }
    supported_objects: set[tuple[str, str]] = set()
    seen_bindings: set[tuple[str, str, str, str, str]] = set()
    binding_fields = {
        "object_kind",
        "object_id",
        "typed_evidence_id",
        "assertion_id",
        "assertion_evidence_id",
        "assertion_record_sha256",
        "claim_id",
        "claim_sha256",
        "gate_id",
        "verdict_sha256",
    }
    for binding in bundle["bindings"]:
        if not isinstance(binding, dict) or set(binding) != binding_fields:
            raise DecisionEvidenceProjectionError("provenance binding fields are invalid")
        typed = typed_evidence.get(binding["typed_evidence_id"])
        assertion = assertions.get(binding["assertion_id"])
        claim = claims.get(binding["claim_id"])
        verdict = verdicts.get(binding["claim_id"])
        if typed is None or assertion is None or claim is None or verdict is None:
            raise DecisionEvidenceProjectionError("provenance binding target is missing")
        assertion_evidence = next(
            (
                item
                for item in assertion["evidence"]
                if item["evidence_id"] == binding["assertion_evidence_id"]
            ),
            None,
        )
        if (
            assertion_evidence is None
            or typed["content_sha256"] != assertion_evidence["sha256"]
            or assertion["record_sha256"] != binding["assertion_record_sha256"]
            or assertion["assertion_id"] not in claim["evidence_assertion_ids"]
            or verdict["claim_sha256"] != binding["claim_sha256"]
            or verdict["gate_id"] != binding["gate_id"]
            or verdict["verdict_sha256"] != binding["verdict_sha256"]
            or verdict["decision"] != "allow"
        ):
            raise DecisionEvidenceProjectionError(
                "provenance binding digest or evidence mismatch"
            )
        obj = _object_for_binding(
            snapshot, binding["object_kind"], binding["object_id"]
        )
        _validate_object_claim_binding(
            object_kind=binding["object_kind"],
            obj=obj,
            claim=claim,
            typed_evidence_id=binding["typed_evidence_id"],
        )
        identity = (
            binding["object_kind"],
            binding["object_id"],
            binding["typed_evidence_id"],
            binding["assertion_id"],
            binding["claim_id"],
        )
        if identity in seen_bindings:
            raise DecisionEvidenceProjectionError("provenance binding is duplicated")
        seen_bindings.add(identity)
        supported_objects.add((binding["object_kind"], binding["object_id"]))
        provenance[binding["typed_evidence_id"]].append(
            {
                "assertion_id": assertion["assertion_id"],
                "assertion_record_sha256": assertion["record_sha256"],
                "claim_id": claim["claim_id"],
                "claim_sha256": verdict["claim_sha256"],
                "claim_decision": verdict["decision"],
                "verdict_sha256": verdict["verdict_sha256"],
                "retrieval_receipt_ref": assertion_evidence[
                    "retrieval_receipt_ref"
                ],
            }
        )
    for values in provenance.values():
        values.sort(key=lambda item: (item["assertion_id"], item["claim_id"]))
    return provenance, supported_objects, bundle["bundle_sha256"]


def _support_status(
    *,
    object_kind: str,
    object_id: str,
    evidence_ids: list[str],
    evidence: dict[str, dict[str, Any]],
    supported_objects: set[tuple[str, str]],
) -> str:
    if not evidence_ids:
        return "missing"
    if any(evidence[evidence_id]["validity"] in {"stale", "rejected"} for evidence_id in evidence_ids):
        return "degraded"
    if (object_kind, object_id) in supported_objects:
        return "validated"
    if any(evidence[evidence_id]["validity"] == "candidate" for evidence_id in evidence_ids):
        return "candidate"
    return "metadata-only"


def _decision_timeline(
    snapshot: dict[str, Any],
    *,
    evidence: dict[str, dict[str, Any]],
    supported_objects: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    superseded_by: dict[str, list[str]] = {
        item["decision_id"]: [] for item in snapshot["decisions"]
    }
    for decision in snapshot["decisions"]:
        if decision["supersedes_decision_id"] is not None:
            superseded_by[decision["supersedes_decision_id"]].append(
                decision["decision_id"]
            )
    current_ids = set(snapshot["project"]["current_decision_ids"])
    return sorted(
        (
            {
                "decision_id": decision["decision_id"],
                "work_id": decision["work_id"],
                "status": decision["status"],
                "statement": decision["statement"],
                "decided_at": decision["decided_at"],
                "supersedes_decision_id": decision["supersedes_decision_id"],
                "superseded_by_decision_ids": sorted(
                    superseded_by[decision["decision_id"]]
                ),
                "evidence_ids": sorted(decision["evidence_ids"]),
                "is_current": decision["decision_id"] in current_ids,
                "support_status": _support_status(
                    object_kind="decision",
                    object_id=decision["decision_id"],
                    evidence_ids=decision["evidence_ids"],
                    evidence=evidence,
                    supported_objects=supported_objects,
                ),
            }
            for decision in snapshot["decisions"]
        ),
        key=lambda item: (
            _timestamp(item["decided_at"], "decision.decided_at"),
            item["decision_id"],
        ),
    )


def _constraint_matrix(
    snapshot: dict[str, Any],
    *,
    observed: datetime,
    evidence: dict[str, dict[str, Any]],
    supported_objects: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    superseded_by: dict[str, list[str]] = {
        item["constraint_id"]: [] for item in snapshot["constraints"]
    }
    for constraint in snapshot["constraints"]:
        if constraint["supersedes_constraint_id"] is not None:
            superseded_by[constraint["supersedes_constraint_id"]].append(
                constraint["constraint_id"]
            )
    active_ids = set(snapshot["project"]["active_constraint_ids"])
    return sorted(
        (
            {
                "constraint_id": constraint["constraint_id"],
                "status": constraint["status"],
                "statement": constraint["statement"],
                "scope_work_ids": sorted(constraint["scope_work_ids"]),
                "expires_at": constraint["expires_at"],
                "supersedes_constraint_id": constraint[
                    "supersedes_constraint_id"
                ],
                "superseded_by_constraint_ids": sorted(
                    superseded_by[constraint["constraint_id"]]
                ),
                "evidence_ids": sorted(constraint["evidence_ids"]),
                "is_current": constraint["constraint_id"] in active_ids,
                "expired_at_observation": (
                    constraint["expires_at"] is not None
                    and _timestamp(
                        constraint["expires_at"], "constraint.expires_at"
                    )
                    <= observed
                ),
                "support_status": _support_status(
                    object_kind="constraint",
                    object_id=constraint["constraint_id"],
                    evidence_ids=constraint["evidence_ids"],
                    evidence=evidence,
                    supported_objects=supported_objects,
                ),
            }
            for constraint in snapshot["constraints"]
        ),
        key=lambda item: item["constraint_id"],
    )


def build_decision_evidence_projection(
    source_projection: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    observed_at: str,
    provenance_bundle: dict[str, Any] | None = None,
    root: str | Path | None = None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
) -> dict[str, Any]:
    """Build one immutable Decision and Evidence view from authenticated State."""
    observed = _timestamp(observed_at, "observed_at")
    try:
        source = validate_external_state_projection(
            source_projection,
            signer=signer,
        )
    except (ExternalStateProjectionError, ValueError) as exc:
        raise DecisionEvidenceProjectionError(
            "external State projection is invalid"
        ) from exc
    snapshot = source["snapshot"]
    _validate_capacity(snapshot)
    _validate_chronology(snapshot)
    snapshot_updated_at = _timestamp(
        snapshot["project"]["updated_at"], "project.updated_at"
    )
    if observed < snapshot_updated_at:
        raise DecisionEvidenceProjectionError(
            "observed_at precedes the State snapshot"
        )

    evidence_by_id = {
        item["evidence_id"]: item for item in snapshot["evidence"]
    }
    if provenance_bundle is None:
        provenance = {evidence_id: [] for evidence_id in evidence_by_id}
        supported_objects: set[tuple[str, str]] = set()
        provenance_bundle_sha256 = None
        capabilities = {
            "typed_evidence": True,
            "assertion_provenance": False,
            "claim_evidence": False,
            "provenance_mode": "metadata-only",
        }
    else:
        provenance, supported_objects, provenance_bundle_sha256 = (
            _validate_provenance_bundle(
                provenance_bundle,
                source=source,
                snapshot=snapshot,
                observed_at=observed_at,
                root=root,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )
        )
        capabilities = {
            "typed_evidence": True,
            "assertion_provenance": True,
            "claim_evidence": True,
            "provenance_mode": "validated",
        }

    references = _collect_references(snapshot)
    matrix = sorted(
        (
            {
                "evidence_id": evidence["evidence_id"],
                "kind": evidence["kind"],
                "artifact_ref": evidence["artifact_ref"],
                "content_sha256": evidence["content_sha256"],
                "validity": evidence["validity"],
                "observed_at": evidence["observed_at"],
                "verified_at": evidence["verified_at"],
                "display_status": _display_status(
                    evidence, references[evidence["evidence_id"]]
                ),
                "referencing_objects": references[evidence["evidence_id"]],
                "provenance": provenance[evidence["evidence_id"]],
            }
            for evidence in snapshot["evidence"]
        ),
        key=lambda item: item["evidence_id"],
    )
    timeline = _decision_timeline(
        snapshot,
        evidence=evidence_by_id,
        supported_objects=supported_objects,
    )
    constraints = _constraint_matrix(
        snapshot,
        observed=observed,
        evidence=evidence_by_id,
        supported_objects=supported_objects,
    )
    display_index = {item["evidence_id"]: item["display_status"] for item in matrix}
    projection: dict[str, Any] = {
        "schema_version": DECISION_EVIDENCE_PROJECTION_SCHEMA_VERSION,
        "project_id": source["project_id"],
        "state_revision": source["state_revision"],
        "state_schema_version": snapshot["schema_version"],
        "state_sha256": source["state_sha256"],
        "source_projection_sha256": source["projection_sha256"],
        "governance_ref": snapshot["project"]["governance_ref"],
        "observed_at": observed_at,
        "provenance_bundle_sha256": provenance_bundle_sha256,
        "capabilities": capabilities,
        "decision_timeline": timeline,
        "constraint_matrix": constraints,
        "evidence_matrix": matrix,
        "health": {
            "current_decision_ids": sorted(
                snapshot["project"]["current_decision_ids"]
            ),
            "superseded_decision_ids": sorted(
                item["decision_id"]
                for item in snapshot["decisions"]
                if item["status"] == "superseded"
            ),
            "expired_active_constraint_ids": sorted(
                item["constraint_id"]
                for item in constraints
                if item["is_current"] and item["expired_at_observation"]
            ),
            "missing_support_object_ids": sorted(
                [
                    item["decision_id"]
                    for item in timeline
                    if item["is_current"] and item["support_status"] == "missing"
                ]
                + [
                    item["constraint_id"]
                    for item in constraints
                    if item["is_current"] and item["support_status"] == "missing"
                ]
            ),
            "candidate_evidence_ids": sorted(
                evidence_id
                for evidence_id, status in display_index.items()
                if status == "candidate"
            ),
            "stale_evidence_ids": sorted(
                evidence_id
                for evidence_id, status in display_index.items()
                if status == "stale"
            ),
            "rejected_evidence_ids": sorted(
                evidence_id
                for evidence_id, status in display_index.items()
                if status == "rejected"
            ),
            "unreferenced_evidence_ids": sorted(
                evidence_id
                for evidence_id, status in display_index.items()
                if status == "unreferenced"
            ),
            "m7_unbound_evidence_ids": sorted(
                evidence_id
                for evidence_id, values in provenance.items()
                if not values
            ),
        },
        "authority": copy.deepcopy(_AUTHORITY),
    }
    projection["projection_sha256"] = _digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def validate_decision_evidence_projection(
    projection: dict[str, Any],
    *,
    source_projection: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
    provenance_bundle: dict[str, Any] | None = None,
    root: str | Path | None = None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
) -> dict[str, Any]:
    """Rebuild and verify the complete view against signed State and M7 evidence."""
    if not isinstance(projection, dict):
        raise DecisionEvidenceProjectionError("projection must be an object")
    authority = projection.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("state_write_authority") is not False
        or authority.get("completion_authority") is not False
        or authority.get("approval_authority") is not False
        or type(authority.get("provider_authority")) is not int
        or authority["provider_authority"] != 0
        or type(authority.get("external_effect_authority")) is not int
        or authority["external_effect_authority"] != 0
    ):
        raise DecisionEvidenceProjectionError("projection authority must remain zero")
    try:
        expected = build_decision_evidence_projection(
            source_projection,
            signer=signer,
            observed_at=projection.get("observed_at"),
            provenance_bundle=provenance_bundle,
            root=root,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
    except (DecisionEvidenceProjectionError, TypeError) as exc:
        raise DecisionEvidenceProjectionError("projection cannot be rebuilt") from exc
    if _canonical_bytes(projection) != _canonical_bytes(expected):
        raise DecisionEvidenceProjectionError(
            "projection does not match signed State and provenance"
        )
    return copy.deepcopy(projection)


__all__ = [
    "DECISION_EVIDENCE_PROJECTION_SCHEMA_VERSION",
    "PROVENANCE_BUNDLE_SCHEMA_VERSION",
    "DecisionEvidenceProjectionError",
    "build_decision_evidence_projection",
    "validate_decision_evidence_projection",
]
