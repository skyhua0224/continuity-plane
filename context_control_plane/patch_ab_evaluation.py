"""Deterministic, provider-neutral patch A/B blind evaluation contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

EXPERIMENT_SCHEMA_VERSION = "context.patch-ab-experiment/v1alpha2"
BLIND_SET_SCHEMA_VERSION = "context.patch-ab-blind-set/v1alpha2"
VERIFIER_PACKET_SCHEMA_VERSION = "context.patch-ab-verifier-packet/v1alpha1"
SCORE_SCHEMA_VERSION = "context.patch-ab-verifier-score/v1alpha2"
RESULT_SCHEMA_VERSION = "context.patch-ab-result/v1alpha2"
BENCHMARK_SCHEMA_VERSION = "context.patch-ab-benchmark/v1alpha2"
RANDOMIZER_PROVENANCE_SCHEMA_VERSION = (
    "context.patch-ab-randomizer-provenance/v1alpha1"
)
SCORE_EVIDENCE_SCHEMA_VERSION = "context.patch-ab-score-evidence/v1alpha1"
QUALITY_EVIDENCE_SCHEMA_VERSION = "context.patch-ab-quality-evidence/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

_EXPERIMENT_FIELDS = {
    "schema_version",
    "experiment_id",
    "work_id",
    "frozen_experiment_sha256",
    "randomization_seed_sha256",
    "randomizer",
    "candidates",
    "verifier_policy",
    "state_write_authority",
    "completion_authority",
    "experiment_sha256",
}
_RANDOMIZER_FIELDS = {
    "randomizer_id",
    "entropy_ref",
    "provenance_ref",
    "frozen_experiment_sha256",
}
_CANDIDATE_FIELDS = {
    "candidate_id",
    "variant",
    "producer_id",
    "patch_ref",
    "run_context",
    "quality",
    "evidence_refs",
}
_RUN_CONTEXT_FIELDS = {"provider_id", "model_id", "fixture_ref", "budget"}
_BUDGET_FIELDS = {"input_tokens", "output_tokens", "tool_calls", "wall_time_ms"}
_QUALITY_FIELDS = {
    "build_passed",
    "test_passed",
    "mutation_score_basis_points",
    "rework_count",
}
_VERIFIER_POLICY_FIELDS = {
    "minimum_independent_verifiers",
    "rubric_ids",
    "score_min_basis_points",
    "score_max_basis_points",
}
_BLIND_SET_FIELDS = {
    "schema_version",
    "blind_set_id",
    "experiment_id",
    "experiment_sha256",
    "fixture_ref",
    "candidates",
    "order_sha256",
    "state_write_authority",
    "completion_authority",
    "blind_set_sha256",
}
_BLIND_CANDIDATE_FIELDS = {"blind_id", "patch_ref"}
_VERIFIER_PACKET_FIELDS = {
    "schema_version",
    "packet_id",
    "experiment_id",
    "experiment_sha256",
    "blind_set_sha256",
    "fixture_ref",
    "rubric_ids",
    "assignments",
    "state_write_authority",
    "completion_authority",
    "packet_sha256",
}
_ASSIGNMENT_FIELDS = {"assignment_token", "patch_ref"}
_SCORE_FIELDS = {
    "schema_version",
    "score_id",
    "experiment_id",
    "experiment_sha256",
    "verifier_packet_sha256",
    "verifier_id",
    "verifier_role",
    "assignment_token",
    "rubric_scores",
    "evidence_refs",
    "state_write_authority",
    "completion_authority",
    "score_sha256",
}
_RUBRIC_SCORE_FIELDS = {"rubric_id", "score_basis_points"}
_RESULT_FIELDS = {
    "schema_version",
    "result_id",
    "experiment_id",
    "experiment_sha256",
    "blind_set_sha256",
    "verifier_packet_sha256",
    "decision",
    "reasons",
    "candidate_results",
    "quality_deltas",
    "quality_not_degraded",
    "rework_reduced",
    "trust_status",
    "unresolved_trust_reasons",
    "external_calls",
    "state_write_authority",
    "completion_authority",
    "result_sha256",
}
_CANDIDATE_RESULT_FIELDS = {
    "candidate_id",
    "variant",
    "blind_id",
    "build_passed",
    "test_passed",
    "mutation_score_basis_points",
    "rework_count",
    "verifier_score_total_basis_points",
    "verifier_score_observation_count",
    "verifier_count",
    "evidence_refs",
}
_QUALITY_DELTA_FIELDS = {
    "mutation_score_basis_points",
    "rework_count",
    "verifier_score_delta_numerator_basis_points",
    "verifier_score_delta_denominator",
}
_REASONS = {
    "quality_not_degraded",
    "rework_reduced",
    "build_gate_failed",
    "test_gate_failed",
    "mutation_score_regressed",
    "verifier_score_regressed",
    "rework_not_reduced",
    "trust_unresolved",
}
_QUALITY_RESOLUTION_FIELDS = {
    "candidate_id",
    "patch_ref",
    "fixture_ref",
    "quality",
    "verification_decision_ref",
    "verification_run_refs",
    "claim_admission_ref",
    "verification_status",
    "run_status",
    "claim_decision",
}
_RANDOMIZER_PROVENANCE_FIELDS = {
    "schema_version",
    "provenance_id",
    "randomizer_id",
    "entropy_ref",
    "entropy_sha256",
    "frozen_experiment_sha256",
    "source_kind",
    "provenance_sha256",
}
_SCORE_EVIDENCE_FIELDS = {
    "schema_version",
    "evidence_id",
    "score_id",
    "experiment_id",
    "experiment_sha256",
    "verifier_packet_sha256",
    "verifier_id",
    "assignment_token",
    "rubric_scores",
    "evidence_sha256",
}
_QUALITY_EVIDENCE_FIELDS = {
    "schema_version",
    "evidence_kind",
    "evidence_id",
    "work_id",
    "candidate_id",
    "patch_ref",
    "fixture_ref",
    "quality",
    "status",
    "evidence_sha256",
}

PrincipalResolver = Callable[[str], str | None]
ArtifactResolver = Callable[[str], bytes | bytearray | memoryview | None]
QualityResolver = Callable[[dict[str, Any]], Mapping[str, Any] | None]


class PatchABContractError(ValueError):
    """Raised when a patch A/B contract cannot be trusted."""


def _canonical_bytes(document: dict[str, Any], digest_field: str) -> bytes:
    unsigned = copy.deepcopy(document)
    unsigned.pop(digest_field, None)
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(document: dict[str, Any], digest_field: str) -> str:
    return hashlib.sha256(_canonical_bytes(document, digest_field)).hexdigest()


def _seal(document: dict[str, Any], digest_field: str) -> dict[str, Any]:
    document[digest_field] = _digest(document, digest_field)
    return document


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise PatchABContractError(f"{label} fields are invalid")
    return value


def _objects(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PatchABContractError(f"{label} must be an object list")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PatchABContractError(f"{label} must be a bounded identifier")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PatchABContractError(f"{label} must be lowercase SHA-256")
    return value


def _artifact(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_RE.fullmatch(value):
        raise PatchABContractError(f"{label} must be an artifact ref")
    return value


def _artifact_list(
    value: Any,
    label: str,
    *,
    non_empty: bool = True,
    maximum: int = 128,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (non_empty and not value)
        or len(value) > maximum
        or len(value) != len(set(value))
    ):
        raise PatchABContractError(f"{label} must contain unique artifact refs")
    for item in value:
        _artifact(item, label)
    return value


def _resolved_artifact(ref: str, resolver: ArtifactResolver) -> bytes | None:
    try:
        value = resolver(ref)
    except Exception as exc:
        raise PatchABContractError("artifact resolver failed") from exc
    if value is None:
        return None
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise PatchABContractError("artifact resolver returned invalid content")
    payload = bytes(value)
    if hashlib.sha256(payload).hexdigest() != ref.rsplit("/", 1)[-1]:
        raise PatchABContractError("artifact resolver digest mismatch")
    return payload


def _canonical_principal(actor_ref: str, resolver: PrincipalResolver) -> str | None:
    try:
        principal = resolver(actor_ref)
    except Exception as exc:
        raise PatchABContractError("principal resolver failed") from exc
    if principal is None:
        return None
    return _identifier(principal, "canonical principal")


def _frozen_experiment_digest(experiment: dict[str, Any]) -> str:
    frozen = {
        "experiment_id": experiment["experiment_id"],
        "work_id": experiment["work_id"],
        "candidates": experiment["candidates"],
        "verifier_policy": experiment["verifier_policy"],
    }
    return hashlib.sha256(
        json.dumps(
            frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def finalize_patch_ab_experiment(
    draft: dict[str, Any],
    *,
    randomizer_id: str,
    entropy_ref: str,
    provenance_ref: str,
    artifact_resolver: ArtifactResolver,
) -> dict[str, Any]:
    """Freeze candidates before deriving ordering entropy."""
    document = copy.deepcopy(draft)
    document["schema_version"] = EXPERIMENT_SCHEMA_VERSION
    document.pop("experiment_sha256", None)
    document.pop("frozen_experiment_sha256", None)
    document.pop("randomization_seed_sha256", None)
    document.pop("randomizer", None)
    frozen = _frozen_experiment_digest(document)
    entropy = _resolved_artifact(_artifact(entropy_ref, "entropy_ref"), artifact_resolver)
    provenance = _resolved_artifact(
        _artifact(provenance_ref, "provenance_ref"), artifact_resolver
    )
    if entropy is None or provenance is None:
        raise PatchABContractError("randomizer artifacts must resolve before freezing")
    seed = hashlib.sha256(entropy + b":" + frozen.encode("ascii")).hexdigest()
    document["frozen_experiment_sha256"] = frozen
    document["randomization_seed_sha256"] = seed
    document["randomizer"] = {
        "randomizer_id": _identifier(randomizer_id, "randomizer_id"),
        "entropy_ref": entropy_ref,
        "provenance_ref": provenance_ref,
        "frozen_experiment_sha256": frozen,
    }
    _validate_randomizer_provenance(
        provenance,
        experiment=document,
        entropy=entropy,
    )
    _seal(document, "experiment_sha256")
    validate_patch_ab_experiment(document)
    return document


def _uint(
    value: Any, label: str, *, positive: bool = False, maximum: int | None = None
) -> int:
    minimum = 1 if positive else 0
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise PatchABContractError(f"{label} is outside its integer bounds")
    return value


def _authority_false(document: dict[str, Any], label: str) -> None:
    if document["state_write_authority"] is not False:
        raise PatchABContractError(f"{label} cannot write State")
    if document["completion_authority"] is not False:
        raise PatchABContractError(f"{label} cannot complete work")


def _validate_digest(document: dict[str, Any], field: str, label: str) -> None:
    _sha256(document[field], field)
    if document[field] != _digest(document, field):
        raise PatchABContractError(f"{label} digest mismatch")


def _json_bytes(document: dict[str, Any], digest_field: str) -> bytes:
    del digest_field
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_patch_ab_randomizer_provenance(
    *,
    provenance_id: str,
    randomizer_id: str,
    entropy_ref: str,
    entropy_sha256: str,
    frozen_experiment_sha256: str,
    source_kind: str = "synthetic-fixture",
) -> bytes:
    document = {
        "schema_version": RANDOMIZER_PROVENANCE_SCHEMA_VERSION,
        "provenance_id": provenance_id,
        "randomizer_id": randomizer_id,
        "entropy_ref": entropy_ref,
        "entropy_sha256": entropy_sha256,
        "frozen_experiment_sha256": frozen_experiment_sha256,
        "source_kind": source_kind,
        "provenance_sha256": "0" * 64,
    }
    _seal(document, "provenance_sha256")
    return _json_bytes(document, "provenance_sha256")


def build_patch_ab_score_evidence(score: dict[str, Any]) -> bytes:
    document = {
        "schema_version": SCORE_EVIDENCE_SCHEMA_VERSION,
        "evidence_id": "evidence/" + score["score_id"],
        "score_id": score["score_id"],
        "experiment_id": score["experiment_id"],
        "experiment_sha256": score["experiment_sha256"],
        "verifier_packet_sha256": score["verifier_packet_sha256"],
        "verifier_id": score["verifier_id"],
        "assignment_token": score["assignment_token"],
        "rubric_scores": copy.deepcopy(score["rubric_scores"]),
        "evidence_sha256": "0" * 64,
    }
    _seal(document, "evidence_sha256")
    return _json_bytes(document, "evidence_sha256")


def build_patch_ab_quality_evidence(
    *,
    evidence_kind: str,
    evidence_id: str,
    work_id: str,
    candidate_id: str,
    patch_ref: str,
    fixture_ref: str,
    quality: dict[str, Any],
    status: str,
) -> bytes:
    document = {
        "schema_version": QUALITY_EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": evidence_kind,
        "evidence_id": evidence_id,
        "work_id": work_id,
        "candidate_id": candidate_id,
        "patch_ref": patch_ref,
        "fixture_ref": fixture_ref,
        "quality": copy.deepcopy(quality),
        "status": status,
        "evidence_sha256": "0" * 64,
    }
    _seal(document, "evidence_sha256")
    return _json_bytes(document, "evidence_sha256")


def _typed_json(
    payload: bytes,
    fields: set[str],
    schema_version: str,
    label: str,
) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PatchABContractError(f"{label} is not typed JSON") from exc
    root = _object(value, fields, label)
    if root["schema_version"] != schema_version:
        raise PatchABContractError(f"{label} schema_version is invalid")
    return root


def _validate_randomizer_provenance(
    payload: bytes,
    *,
    experiment: dict[str, Any],
    entropy: bytes,
) -> None:
    root = _typed_json(
        payload,
        _RANDOMIZER_PROVENANCE_FIELDS,
        RANDOMIZER_PROVENANCE_SCHEMA_VERSION,
        "randomizer provenance",
    )
    _identifier(root["provenance_id"], "provenance_id")
    _identifier(root["randomizer_id"], "randomizer_id")
    _artifact(root["entropy_ref"], "entropy_ref")
    _sha256(root["entropy_sha256"], "entropy_sha256")
    _sha256(root["frozen_experiment_sha256"], "frozen_experiment_sha256")
    if root["source_kind"] not in {"synthetic-fixture", "attested-entropy"}:
        raise PatchABContractError("randomizer provenance source_kind is invalid")
    _validate_digest(root, "provenance_sha256", "randomizer provenance")
    randomizer = experiment["randomizer"]
    if (
        root["randomizer_id"] != randomizer["randomizer_id"]
        or root["entropy_ref"] != randomizer["entropy_ref"]
        or root["entropy_sha256"] != hashlib.sha256(entropy).hexdigest()
        or root["frozen_experiment_sha256"]
        != experiment["frozen_experiment_sha256"]
    ):
        raise PatchABContractError("randomizer provenance binding mismatch")


def _validate_score_evidence(
    payload: bytes,
    *,
    score: dict[str, Any],
) -> None:
    root = _typed_json(
        payload,
        _SCORE_EVIDENCE_FIELDS,
        SCORE_EVIDENCE_SCHEMA_VERSION,
        "score evidence",
    )
    for field in (
        "evidence_id",
        "score_id",
        "experiment_id",
        "verifier_id",
        "assignment_token",
    ):
        _identifier(root[field], field)
    for field in (
        "experiment_sha256",
        "verifier_packet_sha256",
        "evidence_sha256",
    ):
        _sha256(root[field], field)
    rubric_scores = _objects(root["rubric_scores"], "score evidence rubric_scores")
    for rubric_score in rubric_scores:
        _object(rubric_score, _RUBRIC_SCORE_FIELDS, "score evidence rubric score")
        _identifier(rubric_score["rubric_id"], "rubric_id")
        _uint(rubric_score["score_basis_points"], "score_basis_points", maximum=10000)
    _validate_digest(root, "evidence_sha256", "score evidence")
    if any(
        root[field] != score[field]
        for field in (
            "score_id",
            "experiment_id",
            "experiment_sha256",
            "verifier_packet_sha256",
            "verifier_id",
            "assignment_token",
            "rubric_scores",
        )
    ):
        raise PatchABContractError("score evidence binding mismatch")


def _validate_quality_evidence(
    payload: bytes,
    *,
    candidate: dict[str, Any],
    work_id: str,
    evidence_kind: str,
    status: str,
) -> None:
    root = _typed_json(
        payload,
        _QUALITY_EVIDENCE_FIELDS,
        QUALITY_EVIDENCE_SCHEMA_VERSION,
        "quality evidence",
    )
    for field in (
        "evidence_kind",
        "evidence_id",
        "work_id",
        "candidate_id",
        "status",
    ):
        _identifier(root[field], field)
    for field in ("patch_ref", "fixture_ref"):
        _artifact(root[field], field)
    quality = _object(root["quality"], _QUALITY_FIELDS, "quality evidence quality")
    if type(quality["build_passed"]) is not bool or type(
        quality["test_passed"]
    ) is not bool:
        raise PatchABContractError("quality evidence build/test metrics are invalid")
    _uint(quality["mutation_score_basis_points"], "quality evidence mutation", maximum=10000)
    _uint(quality["rework_count"], "quality evidence rework")
    _validate_digest(root, "evidence_sha256", "quality evidence")
    if (
        root["evidence_kind"] != evidence_kind
        or root["status"] != status
        or root["work_id"] != work_id
        or root["candidate_id"] != candidate["candidate_id"]
        or root["patch_ref"] != candidate["patch_ref"]
        or root["fixture_ref"] != candidate["run_context"]["fixture_ref"]
        or root["quality"] != candidate["quality"]
    ):
        raise PatchABContractError("quality evidence binding mismatch")


def validate_patch_ab_experiment(experiment: dict[str, Any]) -> None:
    """Validate an immutable same-context two-candidate A/B experiment."""
    root = _object(experiment, _EXPERIMENT_FIELDS, "experiment")
    if root["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
        raise PatchABContractError("experiment schema_version is invalid")
    _identifier(root["experiment_id"], "experiment_id")
    _identifier(root["work_id"], "work_id")
    _sha256(root["frozen_experiment_sha256"], "frozen_experiment_sha256")
    _sha256(root["randomization_seed_sha256"], "randomization_seed_sha256")
    randomizer = _object(root["randomizer"], _RANDOMIZER_FIELDS, "randomizer")
    _identifier(randomizer["randomizer_id"], "randomizer_id")
    _artifact(randomizer["entropy_ref"], "entropy_ref")
    _artifact(randomizer["provenance_ref"], "provenance_ref")
    _sha256(randomizer["frozen_experiment_sha256"], "randomizer frozen digest")
    _authority_false(root, "experiment")

    candidates = _objects(root["candidates"], "candidates")
    if len(candidates) != 2:
        raise PatchABContractError("experiment requires exactly two candidates")
    candidate_ids: set[str] = set()
    producer_ids: set[str] = set()
    patch_refs: set[str] = set()
    variants: set[str] = set()
    common_context: dict[str, Any] | None = None
    for candidate in candidates:
        _object(candidate, _CANDIDATE_FIELDS, "candidate")
        candidate_id = _identifier(candidate["candidate_id"], "candidate_id")
        producer_id = _identifier(candidate["producer_id"], "producer_id")
        variant = candidate["variant"]
        if variant not in {"control", "treatment"}:
            raise PatchABContractError("candidate variant is invalid")
        patch_ref = _artifact(candidate["patch_ref"], "patch_ref")
        if (
            candidate_id in candidate_ids
            or producer_id in producer_ids
            or patch_ref in patch_refs
        ):
            raise PatchABContractError(
                "candidate identity, producer, and patch must be unique"
            )
        candidate_ids.add(candidate_id)
        producer_ids.add(producer_id)
        patch_refs.add(patch_ref)
        variants.add(variant)

        context = _object(candidate["run_context"], _RUN_CONTEXT_FIELDS, "run_context")
        _identifier(context["provider_id"], "provider_id")
        _identifier(context["model_id"], "model_id")
        _artifact(context["fixture_ref"], "fixture_ref")
        budget = _object(context["budget"], _BUDGET_FIELDS, "budget")
        for field in sorted(_BUDGET_FIELDS):
            _uint(budget[field], f"budget.{field}", positive=True)
        if common_context is None:
            common_context = copy.deepcopy(context)
        elif context != common_context:
            raise PatchABContractError(
                "candidates must use the same provider, model, budget, and fixture"
            )

        quality = _object(candidate["quality"], _QUALITY_FIELDS, "quality")
        if (
            type(quality["build_passed"]) is not bool
            or type(quality["test_passed"]) is not bool
        ):
            raise PatchABContractError("build and test metrics must be boolean")
        _uint(
            quality["mutation_score_basis_points"],
            "mutation_score_basis_points",
            maximum=10000,
        )
        _uint(quality["rework_count"], "rework_count")
        _artifact_list(candidate["evidence_refs"], "candidate.evidence_refs")
    if variants != {"control", "treatment"}:
        raise PatchABContractError("control and treatment candidates are required")

    policy = _object(
        root["verifier_policy"], _VERIFIER_POLICY_FIELDS, "verifier_policy"
    )
    _uint(
        policy["minimum_independent_verifiers"],
        "minimum_independent_verifiers",
        positive=True,
        maximum=16,
    )
    if policy["minimum_independent_verifiers"] < 2:
        raise PatchABContractError("at least two independent verifiers are required")
    rubrics = policy["rubric_ids"]
    if (
        not isinstance(rubrics, list)
        or not rubrics
        or len(rubrics) > 32
        or len(rubrics) != len(set(rubrics))
    ):
        raise PatchABContractError("rubric_ids must be unique and bounded")
    for rubric_id in rubrics:
        _identifier(rubric_id, "rubric_id")
    if (
        policy["score_min_basis_points"] != 0
        or policy["score_max_basis_points"] != 10000
    ):
        raise PatchABContractError(
            "verifier score bounds must be 0..10000 basis points"
        )
    frozen = _frozen_experiment_digest(root)
    if (
        root["frozen_experiment_sha256"] != frozen
        or randomizer["frozen_experiment_sha256"] != frozen
    ):
        raise PatchABContractError("randomizer is not bound to frozen experiment")
    _validate_digest(root, "experiment_sha256", "experiment")


def _blind_id(experiment: dict[str, Any], candidate: dict[str, Any]) -> str:
    payload = (
        experiment["randomization_seed_sha256"]
        + ":"
        + experiment["experiment_sha256"]
        + ":"
        + candidate["candidate_id"]
    ).encode("utf-8")
    return "blind/" + hashlib.sha256(payload).hexdigest()[:32]


def _compose_blind_patch_set(experiment: dict[str, Any]) -> dict[str, Any]:
    by_variant = {
        candidate["variant"]: candidate for candidate in experiment["candidates"]
    }
    order = ["control", "treatment"]
    if int(experiment["randomization_seed_sha256"][-1], 16) % 2:
        order.reverse()
    candidates = [
        {
            "blind_id": _blind_id(experiment, by_variant[variant]),
            "patch_ref": by_variant[variant]["patch_ref"],
        }
        for variant in order
    ]
    order_sha256 = hashlib.sha256(
        json.dumps(
            [candidate["blind_id"] for candidate in candidates],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    document = {
        "schema_version": BLIND_SET_SCHEMA_VERSION,
        "blind_set_id": "blind-set/" + experiment["experiment_sha256"][:32],
        "experiment_id": experiment["experiment_id"],
        "experiment_sha256": experiment["experiment_sha256"],
        "fixture_ref": experiment["candidates"][0]["run_context"]["fixture_ref"],
        "candidates": candidates,
        "order_sha256": order_sha256,
        "state_write_authority": False,
        "completion_authority": False,
    }
    return _seal(document, "blind_set_sha256")


def prepare_blind_patch_set(experiment: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic ordering without exposing candidate roles."""
    validate_patch_ab_experiment(experiment)
    blind_set = _compose_blind_patch_set(experiment)
    validate_blind_patch_set(blind_set, expected_experiment=experiment)
    return blind_set


def validate_blind_patch_set(
    blind_set: dict[str, Any], *, expected_experiment: dict[str, Any]
) -> None:
    root = _object(blind_set, _BLIND_SET_FIELDS, "blind_set")
    if root["schema_version"] != BLIND_SET_SCHEMA_VERSION:
        raise PatchABContractError("blind set schema_version is invalid")
    _identifier(root["blind_set_id"], "blind_set_id")
    _identifier(root["experiment_id"], "experiment_id")
    _sha256(root["experiment_sha256"], "experiment_sha256")
    _artifact(root["fixture_ref"], "fixture_ref")
    candidates = _objects(root["candidates"], "blind candidates")
    if len(candidates) != 2:
        raise PatchABContractError("blind set requires two candidates")
    blind_ids: set[str] = set()
    patch_refs: set[str] = set()
    for candidate in candidates:
        _object(candidate, _BLIND_CANDIDATE_FIELDS, "blind candidate")
        blind_id = _identifier(candidate["blind_id"], "blind_id")
        patch_ref = _artifact(candidate["patch_ref"], "patch_ref")
        if blind_id in blind_ids or patch_ref in patch_refs:
            raise PatchABContractError("blind candidate IDs and patches must be unique")
        blind_ids.add(blind_id)
        patch_refs.add(patch_ref)
    _sha256(root["order_sha256"], "order_sha256")
    _authority_false(root, "blind set")
    _validate_digest(root, "blind_set_sha256", "blind set")
    validate_patch_ab_experiment(expected_experiment)
    if root != _compose_blind_patch_set(expected_experiment):
        raise PatchABContractError("blind set does not match expected experiment")


def _unblind_candidate_ids(
    experiment: dict[str, Any], blind_set: dict[str, Any]
) -> dict[str, str]:
    validate_blind_patch_set(blind_set, expected_experiment=experiment)
    blind_by_patch = {
        candidate["patch_ref"]: candidate["blind_id"]
        for candidate in blind_set["candidates"]
    }
    return {
        candidate["variant"]: blind_by_patch[candidate["patch_ref"]]
        for candidate in experiment["candidates"]
    }


def _assignment_token(
    experiment: dict[str, Any], blind_set: dict[str, Any], blind_id: str
) -> str:
    payload = (
        experiment["randomization_seed_sha256"]
        + ":"
        + blind_set["blind_set_sha256"]
        + ":"
        + blind_id
    ).encode("utf-8")
    return "assignment/" + hashlib.sha256(payload).hexdigest()


def _compose_verifier_packet(
    experiment: dict[str, Any], blind_set: dict[str, Any]
) -> dict[str, Any]:
    assignments = [
        {
            "assignment_token": _assignment_token(
                experiment, blind_set, candidate["blind_id"]
            ),
            "patch_ref": candidate["patch_ref"],
        }
        for candidate in blind_set["candidates"]
    ]
    packet = {
        "schema_version": VERIFIER_PACKET_SCHEMA_VERSION,
        "packet_id": "verifier-packet/" + blind_set["blind_set_sha256"][:32],
        "experiment_id": experiment["experiment_id"],
        "experiment_sha256": experiment["experiment_sha256"],
        "blind_set_sha256": blind_set["blind_set_sha256"],
        "fixture_ref": blind_set["fixture_ref"],
        "rubric_ids": list(experiment["verifier_policy"]["rubric_ids"]),
        "assignments": assignments,
        "state_write_authority": False,
        "completion_authority": False,
    }
    return _seal(packet, "packet_sha256")


def prepare_verifier_packet(
    experiment: dict[str, Any], blind_set: dict[str, Any]
) -> dict[str, Any]:
    """Return the only candidate packet exposed to a verifier."""
    validate_blind_patch_set(blind_set, expected_experiment=experiment)
    packet = _compose_verifier_packet(experiment, blind_set)
    validate_verifier_packet(
        packet, expected_experiment=experiment, expected_blind_set=blind_set
    )
    return packet


def validate_verifier_packet(
    packet: dict[str, Any],
    *,
    expected_experiment: dict[str, Any],
    expected_blind_set: dict[str, Any],
) -> None:
    root = _object(packet, _VERIFIER_PACKET_FIELDS, "verifier packet")
    if root["schema_version"] != VERIFIER_PACKET_SCHEMA_VERSION:
        raise PatchABContractError("verifier packet schema_version is invalid")
    for field in ("packet_id", "experiment_id"):
        _identifier(root[field], field)
    for field in ("experiment_sha256", "blind_set_sha256", "packet_sha256"):
        _sha256(root[field], field)
    _artifact(root["fixture_ref"], "fixture_ref")
    rubrics = root["rubric_ids"]
    if not isinstance(rubrics, list) or not rubrics or len(rubrics) > 32:
        raise PatchABContractError("verifier packet rubrics are invalid")
    for rubric in rubrics:
        _identifier(rubric, "rubric_id")
    assignments = _objects(root["assignments"], "assignments")
    if len(assignments) != 2:
        raise PatchABContractError("verifier packet requires two assignments")
    tokens: set[str] = set()
    patches: set[str] = set()
    for assignment in assignments:
        _object(assignment, _ASSIGNMENT_FIELDS, "assignment")
        token = _identifier(assignment["assignment_token"], "assignment_token")
        patch = _artifact(assignment["patch_ref"], "assignment patch_ref")
        if token in tokens or patch in patches:
            raise PatchABContractError("assignments must be unique")
        tokens.add(token)
        patches.add(patch)
    _authority_false(root, "verifier packet")
    _validate_digest(root, "packet_sha256", "verifier packet")
    validate_blind_patch_set(
        expected_blind_set, expected_experiment=expected_experiment
    )
    if root != _compose_verifier_packet(expected_experiment, expected_blind_set):
        raise PatchABContractError("verifier packet does not match blind set")


def _blind_by_assignment(
    experiment: dict[str, Any],
    blind_set: dict[str, Any],
    verifier_packet: dict[str, Any],
) -> dict[str, str]:
    validate_verifier_packet(
        verifier_packet,
        expected_experiment=experiment,
        expected_blind_set=blind_set,
    )
    return {
        _assignment_token(experiment, blind_set, candidate["blind_id"]): candidate[
            "blind_id"
        ]
        for candidate in blind_set["candidates"]
    }


def _validate_verifier_score(
    score: dict[str, Any],
    *,
    experiment: dict[str, Any],
    blind_set: dict[str, Any],
    verifier_packet: dict[str, Any],
) -> None:
    root = _object(score, _SCORE_FIELDS, "verifier score")
    if root["schema_version"] != SCORE_SCHEMA_VERSION:
        raise PatchABContractError("verifier score schema_version is invalid")
    for field in ("score_id", "experiment_id", "verifier_id", "assignment_token"):
        _identifier(root[field], field)
    for field in ("experiment_sha256", "verifier_packet_sha256"):
        _sha256(root[field], field)
    if root["verifier_role"] != "independent":
        raise PatchABContractError("verifier role must be independent")
    _artifact_list(root["evidence_refs"], "score.evidence_refs")
    _authority_false(root, "verifier score")
    _validate_digest(root, "score_sha256", "verifier score")
    if (
        root["experiment_id"] != experiment["experiment_id"]
        or root["experiment_sha256"] != experiment["experiment_sha256"]
        or root["verifier_packet_sha256"] != verifier_packet["packet_sha256"]
    ):
        raise PatchABContractError("verifier score experiment binding mismatch")
    if root["assignment_token"] not in _blind_by_assignment(
        experiment, blind_set, verifier_packet
    ):
        raise PatchABContractError(
            "verifier score references an unknown assignment"
        )
    if root["verifier_id"] in {
        candidate["producer_id"] for candidate in experiment["candidates"]
    }:
        raise PatchABContractError("candidate producer cannot verify the experiment")
    rubric_scores = _objects(root["rubric_scores"], "rubric_scores")
    rubric_ids: list[str] = []
    for rubric_score in rubric_scores:
        _object(rubric_score, _RUBRIC_SCORE_FIELDS, "rubric score")
        rubric_id = _identifier(rubric_score["rubric_id"], "rubric_id")
        rubric_ids.append(rubric_id)
        _uint(
            rubric_score["score_basis_points"],
            "score_basis_points",
            maximum=10000,
        )
    if len(rubric_ids) != len(set(rubric_ids)) or set(rubric_ids) != set(
        experiment["verifier_policy"]["rubric_ids"]
    ):
        raise PatchABContractError("verifier score rubric coverage is incomplete")


def _validated_score_matrix(
    experiment: dict[str, Any],
    blind_set: dict[str, Any],
    verifier_packet: dict[str, Any],
    scores: list[dict[str, Any]],
    *,
    principal_resolver: PrincipalResolver | None = None,
) -> tuple[dict[str, list[int]], int, bool]:
    if not isinstance(scores, list) or not scores:
        raise PatchABContractError("verifier scores are required")
    score_ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    verifier_ids: set[str] = set()
    principals_resolved = principal_resolver is not None
    producer_principals: set[str] = set()
    randomizer_principal: str | None = None
    if principal_resolver is not None:
        for candidate in experiment["candidates"]:
            principal = _canonical_principal(
                candidate["producer_id"], principal_resolver
            )
            if principal is None:
                principals_resolved = False
            else:
                producer_principals.add(principal)
        randomizer_principal = _canonical_principal(
            experiment["randomizer"]["randomizer_id"], principal_resolver
        )
        if randomizer_principal is None:
            principals_resolved = False
        elif randomizer_principal in producer_principals:
            raise PatchABContractError("randomizer must be independent of producers")
    blind_by_assignment = _blind_by_assignment(
        experiment, blind_set, verifier_packet
    )
    values_by_blind: dict[str, list[int]] = {
        candidate["blind_id"]: [] for candidate in blind_set["candidates"]
    }
    for score in scores:
        _validate_verifier_score(
            score,
            experiment=experiment,
            blind_set=blind_set,
            verifier_packet=verifier_packet,
        )
        if score["score_id"] in score_ids:
            raise PatchABContractError("verifier score IDs must be unique")
        score_ids.add(score["score_id"])
        verifier_identity = score["verifier_id"]
        if principal_resolver is not None:
            principal = _canonical_principal(verifier_identity, principal_resolver)
            if principal is None:
                principals_resolved = False
            else:
                verifier_identity = principal
                if principal in producer_principals:
                    raise PatchABContractError(
                        "candidate producer cannot verify the experiment"
                    )
                if principal == randomizer_principal:
                    raise PatchABContractError(
                        "randomizer cannot verify the experiment"
                    )
        blind_id = blind_by_assignment[score["assignment_token"]]
        pair = (verifier_identity, blind_id)
        if pair in pairs:
            raise PatchABContractError("verifier can score a candidate only once")
        pairs.add(pair)
        verifier_ids.add(verifier_identity)
        values_by_blind[blind_id].extend(
            item["score_basis_points"] for item in score["rubric_scores"]
        )
    minimum = experiment["verifier_policy"]["minimum_independent_verifiers"]
    if len(verifier_ids) < minimum:
        raise PatchABContractError("independent verifier count is below policy")
    if len(verifier_ids) > 16:
        raise PatchABContractError("independent verifier count exceeds contract")
    expected_blind_ids = set(values_by_blind)
    for verifier_id in verifier_ids:
        covered = {
            blind_id
            for candidate_verifier, blind_id in pairs
            if candidate_verifier == verifier_id
        }
        if covered != expected_blind_ids:
            raise PatchABContractError("each verifier must score both blind candidates")
    expected_values = len(verifier_ids) * len(
        experiment["verifier_policy"]["rubric_ids"]
    )
    if any(len(values) != expected_values for values in values_by_blind.values()):
        raise PatchABContractError("verifier score matrix is incomplete")
    return values_by_blind, len(verifier_ids), principals_resolved


def _trust_resolution_reasons(
    experiment: dict[str, Any],
    scores: list[dict[str, Any]],
    *,
    principals_resolved: bool,
    artifact_resolver: ArtifactResolver | None,
    quality_resolver: QualityResolver | None,
) -> list[str]:
    reasons: set[str] = set()
    if not principals_resolved:
        reasons.add("principal_unresolved")
    artifact_refs = {
        experiment["randomizer"]["entropy_ref"],
        experiment["randomizer"]["provenance_ref"],
    }
    for candidate in experiment["candidates"]:
        artifact_refs.add(candidate["patch_ref"])
        artifact_refs.add(candidate["run_context"]["fixture_ref"])
        artifact_refs.update(candidate["evidence_refs"])
    for score in scores:
        artifact_refs.update(score["evidence_refs"])
    resolved_artifacts: dict[str, bytes] = {}
    if artifact_resolver is None:
        reasons.add("artifact_unresolved")
    else:
        for ref in sorted(artifact_refs):
            payload = _resolved_artifact(ref, artifact_resolver)
            if payload is None:
                reasons.add("artifact_unresolved")
            else:
                resolved_artifacts[ref] = payload
        entropy = resolved_artifacts.get(experiment["randomizer"]["entropy_ref"])
        provenance = resolved_artifacts.get(experiment["randomizer"]["provenance_ref"])
        if entropy is not None:
            expected_seed = hashlib.sha256(
                entropy
                + b":"
                + experiment["frozen_experiment_sha256"].encode("ascii")
            ).hexdigest()
            if experiment["randomization_seed_sha256"] != expected_seed:
                raise PatchABContractError(
                    "randomizer seed does not match frozen experiment entropy"
                )
        if entropy is not None and provenance is not None:
            _validate_randomizer_provenance(
                provenance,
                experiment=experiment,
                entropy=entropy,
            )
        for score in scores:
            if any(ref not in resolved_artifacts for ref in score["evidence_refs"]):
                continue
            typed_evidence = False
            for ref in score["evidence_refs"]:
                payload = resolved_artifacts[ref]
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(parsed, dict)
                    and parsed.get("schema_version")
                    == SCORE_EVIDENCE_SCHEMA_VERSION
                ):
                    _validate_score_evidence(payload, score=score)
                    typed_evidence = True
            if not typed_evidence:
                raise PatchABContractError(
                    "score evidence is not typed and bound"
                )
    if quality_resolver is None:
        reasons.add("quality_unresolved")
    else:
        for candidate in experiment["candidates"]:
            try:
                resolution_value = quality_resolver(copy.deepcopy(candidate))
            except Exception as exc:
                raise PatchABContractError("quality resolver failed") from exc
            if resolution_value is None:
                reasons.add("quality_unresolved")
                continue
            resolution = _object(
                dict(resolution_value),
                _QUALITY_RESOLUTION_FIELDS,
                "quality resolution",
            )
            run_refs = _artifact_list(
                resolution["verification_run_refs"],
                "verification_run_refs",
            )
            proof_refs = {
                _artifact(
                    resolution["verification_decision_ref"],
                    "verification_decision_ref",
                ),
                _artifact(
                    resolution["claim_admission_ref"], "claim_admission_ref"
                ),
                *run_refs,
            }
            if (
                resolution["candidate_id"] != candidate["candidate_id"]
                or resolution["patch_ref"] != candidate["patch_ref"]
                or resolution["fixture_ref"]
                != candidate["run_context"]["fixture_ref"]
                or resolution["quality"] != candidate["quality"]
                or not proof_refs <= set(candidate["evidence_refs"])
                or resolution["verification_status"] != "satisfied"
                or resolution["run_status"] != "passed"
                or resolution["claim_decision"] != "allow"
            ):
                raise PatchABContractError(
                    "quality resolution does not bind M7-02/M7-03 evidence"
                )
            if artifact_resolver is not None and not any(
                ref not in resolved_artifacts for ref in proof_refs
            ):
                _validate_quality_evidence(
                    resolved_artifacts[resolution["verification_decision_ref"]],
                    candidate=candidate,
                    work_id=experiment["work_id"],
                    evidence_kind="verification-decision",
                    status="satisfied",
                )
                for run_ref in run_refs:
                    _validate_quality_evidence(
                        resolved_artifacts[run_ref],
                        candidate=candidate,
                        work_id=experiment["work_id"],
                        evidence_kind="verification-run",
                        status="passed",
                    )
                _validate_quality_evidence(
                    resolved_artifacts[resolution["claim_admission_ref"]],
                    candidate=candidate,
                    work_id=experiment["work_id"],
                    evidence_kind="claim-admission",
                    status="allow",
                )
    return sorted(reasons)


def _compose_result(
    experiment: dict[str, Any],
    blind_set: dict[str, Any],
    verifier_packet: dict[str, Any],
    scores: list[dict[str, Any]],
    *,
    principal_resolver: PrincipalResolver | None,
    artifact_resolver: ArtifactResolver | None,
    quality_resolver: QualityResolver | None,
) -> dict[str, Any]:
    values_by_blind, verifier_count, principals_resolved = _validated_score_matrix(
        experiment,
        blind_set,
        verifier_packet,
        scores,
        principal_resolver=principal_resolver,
    )
    unresolved = _trust_resolution_reasons(
        experiment,
        scores,
        principals_resolved=principals_resolved,
        artifact_resolver=artifact_resolver,
        quality_resolver=quality_resolver,
    )
    blind_by_variant = _unblind_candidate_ids(experiment, blind_set)
    candidate_by_variant = {
        candidate["variant"]: candidate for candidate in experiment["candidates"]
    }
    candidate_results = []
    for variant in ("control", "treatment"):
        candidate = candidate_by_variant[variant]
        quality = candidate["quality"]
        blind_id = blind_by_variant[variant]
        values = values_by_blind[blind_id]
        candidate_results.append(
            {
                "candidate_id": candidate["candidate_id"],
                "variant": variant,
                "blind_id": blind_id,
                "build_passed": quality["build_passed"],
                "test_passed": quality["test_passed"],
                "mutation_score_basis_points": quality["mutation_score_basis_points"],
                "rework_count": quality["rework_count"],
                "verifier_score_total_basis_points": sum(values),
                "verifier_score_observation_count": len(values),
                "verifier_count": verifier_count,
                "evidence_refs": sorted(candidate["evidence_refs"]),
            }
        )
    control, treatment = candidate_results
    score_denominator = control["verifier_score_observation_count"]
    if treatment["verifier_score_observation_count"] != score_denominator:
        raise PatchABContractError("verifier score denominators do not match")
    deltas = {
        "mutation_score_basis_points": treatment["mutation_score_basis_points"]
        - control["mutation_score_basis_points"],
        "rework_count": treatment["rework_count"] - control["rework_count"],
        "verifier_score_delta_numerator_basis_points": treatment[
            "verifier_score_total_basis_points"
        ]
        - control["verifier_score_total_basis_points"],
        "verifier_score_delta_denominator": score_denominator,
    }
    reasons: list[str] = []
    if not (control["build_passed"] and treatment["build_passed"]):
        reasons.append("build_gate_failed")
    if not (control["test_passed"] and treatment["test_passed"]):
        reasons.append("test_gate_failed")
    if deltas["mutation_score_basis_points"] < 0:
        reasons.append("mutation_score_regressed")
    control_score_total = sum(values_by_blind[blind_by_variant["control"]])
    treatment_score_total = sum(values_by_blind[blind_by_variant["treatment"]])
    if treatment_score_total < control_score_total:
        reasons.append("verifier_score_regressed")
    quality_not_degraded = not reasons
    rework_reduced = deltas["rework_count"] < 0
    if not rework_reduced:
        reasons.append("rework_not_reduced")
    decision = "admit" if quality_not_degraded and rework_reduced else "reject"
    if decision == "admit" and unresolved:
        decision = "provisional"
        reasons = ["trust_unresolved"]
    if decision == "admit":
        reasons = ["quality_not_degraded", "rework_reduced"]
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "result_id": "result/" + experiment["experiment_sha256"][:32],
        "experiment_id": experiment["experiment_id"],
        "experiment_sha256": experiment["experiment_sha256"],
        "blind_set_sha256": blind_set["blind_set_sha256"],
        "verifier_packet_sha256": verifier_packet["packet_sha256"],
        "decision": decision,
        "reasons": reasons,
        "candidate_results": candidate_results,
        "quality_deltas": deltas,
        "quality_not_degraded": quality_not_degraded,
        "rework_reduced": rework_reduced,
        "trust_status": "unresolved" if unresolved else "trusted",
        "unresolved_trust_reasons": unresolved,
        "external_calls": 0,
        "state_write_authority": False,
        "completion_authority": False,
    }
    return _seal(result, "result_sha256")


def evaluate_patch_ab(
    experiment: dict[str, Any],
    blind_set: dict[str, Any],
    scores: list[dict[str, Any]],
    *,
    verifier_packet: dict[str, Any] | None = None,
    principal_resolver: PrincipalResolver | None = None,
    artifact_resolver: ArtifactResolver | None = None,
    quality_resolver: QualityResolver | None = None,
) -> dict[str, Any]:
    """Evaluate build/test/mutation/rework gates after blind scoring."""
    validate_patch_ab_experiment(experiment)
    validate_blind_patch_set(blind_set, expected_experiment=experiment)
    packet = (
        prepare_verifier_packet(experiment, blind_set)
        if verifier_packet is None
        else verifier_packet
    )
    validate_verifier_packet(
        packet, expected_experiment=experiment, expected_blind_set=blind_set
    )
    result = _compose_result(
        experiment,
        blind_set,
        packet,
        scores,
        principal_resolver=principal_resolver,
        artifact_resolver=artifact_resolver,
        quality_resolver=quality_resolver,
    )
    validate_patch_ab_result(
        result,
        expected_experiment=experiment,
        expected_blind_set=blind_set,
        expected_verifier_packet=packet,
        expected_scores=scores,
        principal_resolver=principal_resolver,
        artifact_resolver=artifact_resolver,
        quality_resolver=quality_resolver,
    )
    return result


def validate_patch_ab_result(
    result: dict[str, Any],
    *,
    expected_experiment: dict[str, Any],
    expected_blind_set: dict[str, Any],
    expected_verifier_packet: dict[str, Any],
    expected_scores: list[dict[str, Any]],
    principal_resolver: PrincipalResolver | None = None,
    artifact_resolver: ArtifactResolver | None = None,
    quality_resolver: QualityResolver | None = None,
) -> None:
    """Validate a result against the trusted experiment and verifier receipts."""
    root = _object(result, _RESULT_FIELDS, "result")
    if root["schema_version"] != RESULT_SCHEMA_VERSION:
        raise PatchABContractError("result schema_version is invalid")
    for field in ("result_id", "experiment_id"):
        _identifier(root[field], field)
    for field in (
        "experiment_sha256",
        "blind_set_sha256",
        "verifier_packet_sha256",
        "result_sha256",
    ):
        _sha256(root[field], field)
    if root["decision"] not in {"admit", "reject", "provisional"}:
        raise PatchABContractError("result decision is invalid")
    if (
        not isinstance(root["reasons"], list)
        or not root["reasons"]
        or len(root["reasons"]) != len(set(root["reasons"]))
        or any(reason not in _REASONS for reason in root["reasons"])
    ):
        raise PatchABContractError("result reasons are invalid")
    candidate_results = _objects(root["candidate_results"], "candidate_results")
    if len(candidate_results) != 2:
        raise PatchABContractError("result requires two candidate results")
    variants: set[str] = set()
    for candidate in candidate_results:
        _object(candidate, _CANDIDATE_RESULT_FIELDS, "candidate result")
        _identifier(candidate["candidate_id"], "candidate_id")
        _identifier(candidate["blind_id"], "blind_id")
        if candidate["variant"] not in {"control", "treatment"}:
            raise PatchABContractError("candidate result variant is invalid")
        variants.add(candidate["variant"])
        for field in ("build_passed", "test_passed"):
            if type(candidate[field]) is not bool:
                raise PatchABContractError(f"{field} must be boolean")
        _uint(
            candidate["mutation_score_basis_points"],
            "mutation_score_basis_points",
            maximum=10000,
        )
        _uint(
            candidate["verifier_score_total_basis_points"],
            "verifier_score_total_basis_points",
            maximum=16 * 32 * 10000,
        )
        _uint(candidate["rework_count"], "rework_count")
        _uint(
            candidate["verifier_score_observation_count"],
            "verifier_score_observation_count",
            positive=True,
        )
        _uint(
            candidate["verifier_count"],
            "verifier_count",
            positive=True,
            maximum=16,
        )
        _artifact_list(candidate["evidence_refs"], "candidate result evidence")
    if variants != {"control", "treatment"}:
        raise PatchABContractError("result must contain control and treatment")
    deltas = _object(root["quality_deltas"], _QUALITY_DELTA_FIELDS, "quality_deltas")
    if any(type(deltas[field]) is not int for field in _QUALITY_DELTA_FIELDS):
        raise PatchABContractError("quality deltas must be integers")
    _uint(
        deltas["verifier_score_delta_denominator"],
        "verifier_score_delta_denominator",
        positive=True,
    )
    if (
        type(root["quality_not_degraded"]) is not bool
        or type(root["rework_reduced"]) is not bool
    ):
        raise PatchABContractError("result gate flags must be boolean")
    if root["trust_status"] not in {"trusted", "unresolved"}:
        raise PatchABContractError("result trust_status is invalid")
    unresolved = root["unresolved_trust_reasons"]
    if (
        not isinstance(unresolved, list)
        or len(unresolved) != len(set(unresolved))
        or any(
            item not in {"artifact_unresolved", "principal_unresolved", "quality_unresolved"}
            for item in unresolved
        )
    ):
        raise PatchABContractError("unresolved trust reasons are invalid")
    if (root["trust_status"] == "trusted") is bool(unresolved):
        raise PatchABContractError("result trust status and reasons mismatch")
    if root["external_calls"] != 0:
        raise PatchABContractError(
            "patch A/B evaluation must not call external services"
        )
    _authority_false(root, "result")
    _validate_digest(root, "result_sha256", "result")
    validate_patch_ab_experiment(expected_experiment)
    validate_blind_patch_set(
        expected_blind_set, expected_experiment=expected_experiment
    )
    validate_verifier_packet(
        expected_verifier_packet,
        expected_experiment=expected_experiment,
        expected_blind_set=expected_blind_set,
    )
    expected = _compose_result(
        expected_experiment,
        expected_blind_set,
        expected_verifier_packet,
        expected_scores,
        principal_resolver=principal_resolver,
        artifact_resolver=artifact_resolver,
        quality_resolver=quality_resolver,
    )
    if root != expected:
        raise PatchABContractError("result does not match trusted evaluation inputs")


# Acceptance builders keep all synthetic trust inputs explicit. They remain
# fixture-only and do not grant production authority.
_BENCHMARK_SCENARIOS = ("valid", "build", "test", "mutation", "rework", "verifier")
_BENCHMARK_FIELDS_V2 = {
    "schema_version",
    "benchmark_id",
    "samples",
    "successful_samples",
    "scenario_counts",
    "expected_admit_count",
    "actual_admit_count",
    "expected_reject_count",
    "actual_reject_count",
    "false_admit_count",
    "false_reject_count",
    "control_first_count",
    "treatment_first_count",
    "admitted_quality_nondegradation_count",
    "admitted_rework_reduction_count",
    "outcomes_artifact_ref",
    "outcomes_sha256",
    "fixture_sha256",
    "implementation_sha256",
    "external_calls",
    "state_write_authority",
    "completion_authority",
    "benchmark_sha256",
}


def _store_fixture_artifact(
    artifacts: dict[str, bytes], label: str
) -> str:
    return _store_fixture_payload(artifacts, label.encode("utf-8"))


def _store_fixture_payload(artifacts: dict[str, bytes], payload: bytes) -> str:
    ref = "artifact://sha256/" + hashlib.sha256(payload).hexdigest()
    artifacts[ref] = payload
    return ref


def _fixture_resolvers(
    artifacts: dict[str, bytes],
    principals: dict[str, str],
    quality_resolutions: dict[str, dict[str, Any]],
) -> tuple[ArtifactResolver, PrincipalResolver, QualityResolver]:
    def artifact_resolver(ref: str) -> bytes | None:
        return artifacts.get(ref)

    def principal_resolver(actor_ref: str) -> str | None:
        return principals.get(actor_ref)

    def quality_resolver(candidate: dict[str, Any]) -> dict[str, Any] | None:
        value = quality_resolutions.get(candidate["candidate_id"])
        return copy.deepcopy(value) if value is not None else None

    return artifact_resolver, principal_resolver, quality_resolver


def _build_benchmark_case(
    index: int, scenario: str
) -> tuple[
    dict[str, Any],
    dict[str, bytes],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    artifacts: dict[str, bytes] = {}
    fixture_ref = _store_fixture_artifact(artifacts, f"fixture:{index}")
    entropy_ref = _store_fixture_artifact(artifacts, f"entropy:{index}")
    common_context = {
        "provider_id": "provider/fixture",
        "model_id": "model/fixture-v1",
        "fixture_ref": fixture_ref,
        "budget": {
            "input_tokens": 8192,
            "output_tokens": 4096,
            "tool_calls": 24,
            "wall_time_ms": 600000,
        },
    }
    treatment_quality = {
        "build_passed": scenario != "build",
        "test_passed": scenario != "test",
        "mutation_score_basis_points": 8199 if scenario == "mutation" else 8400,
        "rework_count": 4 if scenario == "rework" else 2,
    }
    qualities = {
        "control": {
            "build_passed": True,
            "test_passed": True,
            "mutation_score_basis_points": 8200,
            "rework_count": 4,
        },
        "treatment": treatment_quality,
    }
    candidates: list[dict[str, Any]] = []
    quality_resolutions: dict[str, dict[str, Any]] = {}
    for variant in ("control", "treatment"):
        candidate_id = f"candidate/{variant}/{index}"
        patch_ref = _store_fixture_artifact(
            artifacts, f"patch:{variant}:{index}"
        )
        quality = copy.deepcopy(qualities[variant])
        decision_ref = _store_fixture_payload(
            artifacts,
            build_patch_ab_quality_evidence(
                evidence_kind="verification-decision",
                evidence_id=f"evidence/decision/{variant}/{index}",
                work_id="M7-04",
                candidate_id=candidate_id,
                patch_ref=patch_ref,
                fixture_ref=fixture_ref,
                quality=quality,
                status="satisfied",
            ),
        )
        run_ref = _store_fixture_payload(
            artifacts,
            build_patch_ab_quality_evidence(
                evidence_kind="verification-run",
                evidence_id=f"evidence/run/{variant}/{index}",
                work_id="M7-04",
                candidate_id=candidate_id,
                patch_ref=patch_ref,
                fixture_ref=fixture_ref,
                quality=quality,
                status="passed",
            ),
        )
        admission_ref = _store_fixture_payload(
            artifacts,
            build_patch_ab_quality_evidence(
                evidence_kind="claim-admission",
                evidence_id=f"evidence/claim/{variant}/{index}",
                work_id="M7-04",
                candidate_id=candidate_id,
                patch_ref=patch_ref,
                fixture_ref=fixture_ref,
                quality=quality,
                status="allow",
            ),
        )
        evidence_refs = [decision_ref, run_ref, admission_ref]
        candidate = {
            "candidate_id": candidate_id,
            "variant": variant,
            "producer_id": f"producer/{variant}/{index}",
            "patch_ref": patch_ref,
            "run_context": copy.deepcopy(common_context),
            "quality": quality,
            "evidence_refs": evidence_refs,
        }
        candidates.append(candidate)
        quality_resolutions[candidate_id] = {
            "candidate_id": candidate_id,
            "patch_ref": patch_ref,
            "fixture_ref": fixture_ref,
            "quality": copy.deepcopy(quality),
            "verification_decision_ref": decision_ref,
            "verification_run_refs": [run_ref],
            "claim_admission_ref": admission_ref,
            "verification_status": "satisfied",
            "run_status": "passed",
            "claim_decision": "allow",
        }
    draft = {
        "experiment_id": f"experiment/m7-04/benchmark/{index}",
        "work_id": "M7-04",
        "candidates": candidates,
        "verifier_policy": {
            "minimum_independent_verifiers": 2,
            "rubric_ids": ["correctness", "maintainability", "scope_discipline"],
            "score_min_basis_points": 0,
            "score_max_basis_points": 10000,
        },
        "state_write_authority": False,
        "completion_authority": False,
    }
    frozen = _frozen_experiment_digest(draft)
    entropy_payload = artifacts[entropy_ref]
    provenance_ref = _store_fixture_payload(
        artifacts,
        build_patch_ab_randomizer_provenance(
            provenance_id=f"provenance/randomizer/{index}",
            randomizer_id=f"randomizer/{index}",
            entropy_ref=entropy_ref,
            entropy_sha256=hashlib.sha256(entropy_payload).hexdigest(),
            frozen_experiment_sha256=frozen,
        ),
    )
    artifact_resolver = lambda ref: artifacts.get(ref)
    experiment = finalize_patch_ab_experiment(
        draft,
        randomizer_id=f"randomizer/{index}",
        entropy_ref=entropy_ref,
        provenance_ref=provenance_ref,
        artifact_resolver=artifact_resolver,
    )
    principals = {
        f"producer/control/{index}": f"principal/producer/control/{index}",
        f"producer/treatment/{index}": f"principal/producer/treatment/{index}",
        f"randomizer/{index}": f"principal/randomizer/{index}",
        "verifier/1": "principal/verifier/1",
        "verifier/2": "principal/verifier/2",
    }
    return experiment, artifacts, principals, quality_resolutions


def _build_benchmark_scores_v2(
    experiment: dict[str, Any],
    verifier_packet: dict[str, Any],
    scenario: str,
    artifacts: dict[str, bytes],
) -> list[dict[str, Any]]:
    treatment_patch = next(
        item["patch_ref"]
        for item in experiment["candidates"]
        if item["variant"] == "treatment"
    )
    scores: list[dict[str, Any]] = []
    for verifier_number in (1, 2):
        verifier_id = f"verifier/{verifier_number}"
        for assignment in verifier_packet["assignments"]:
            is_treatment = assignment["patch_ref"] == treatment_patch
            value = (
                8499
                if scenario == "verifier" and is_treatment
                else 8500
                if is_treatment
                else 8500
                if scenario == "verifier"
                else 8100
            )
            score = {
                "schema_version": SCORE_SCHEMA_VERSION,
                "score_id": (
                    f"score/{verifier_number}/"
                    f"{assignment['assignment_token'].split('/')[-1][:24]}"
                ),
                "experiment_id": experiment["experiment_id"],
                "experiment_sha256": experiment["experiment_sha256"],
                "verifier_packet_sha256": verifier_packet["packet_sha256"],
                "verifier_id": verifier_id,
                "verifier_role": "independent",
                "assignment_token": assignment["assignment_token"],
                "rubric_scores": [
                    {"rubric_id": rubric_id, "score_basis_points": value}
                    for rubric_id in verifier_packet["rubric_ids"]
                ],
                "evidence_refs": [],
                "state_write_authority": False,
                "completion_authority": False,
            }
            evidence_ref = _store_fixture_payload(
                artifacts, build_patch_ab_score_evidence(score)
            )
            score["evidence_refs"] = [evidence_ref]
            scores.append(_seal(score, "score_sha256"))
    return scores


def _outcomes_bytes(outcomes: list[dict[str, Any]]) -> bytes:
    return (
        json.dumps(outcomes, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _benchmark_fixture_sha256() -> str:
    return hashlib.sha256(
        json.dumps(_BENCHMARK_SCENARIOS, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_patch_ab_benchmark(
    *, samples: int = 1000
) -> tuple[dict[str, Any], bytes]:
    _uint(samples, "samples", positive=True, maximum=10000)
    outcomes: list[dict[str, Any]] = []
    for index in range(samples):
        scenario = _BENCHMARK_SCENARIOS[index % len(_BENCHMARK_SCENARIOS)]
        expected = "admit" if scenario == "valid" else "reject"
        experiment, artifacts, principals, quality = _build_benchmark_case(
            index, scenario
        )
        blind_set = prepare_blind_patch_set(experiment)
        verifier_packet = prepare_verifier_packet(experiment, blind_set)
        scores = _build_benchmark_scores_v2(
            experiment, verifier_packet, scenario, artifacts
        )
        artifact_resolver, principal_resolver, quality_resolver = _fixture_resolvers(
            artifacts, principals, quality
        )
        result = evaluate_patch_ab(
            experiment,
            blind_set,
            scores,
            verifier_packet=verifier_packet,
            principal_resolver=principal_resolver,
            artifact_resolver=artifact_resolver,
            quality_resolver=quality_resolver,
        )
        first_patch = blind_set["candidates"][0]["patch_ref"]
        first_variant = next(
            item["variant"]
            for item in experiment["candidates"]
            if item["patch_ref"] == first_patch
        )
        outcomes.append(
            {
                "sample": index,
                "scenario": scenario,
                "expected": expected,
                "actual": result["decision"],
                "first_variant": first_variant,
                "quality_not_degraded": result["quality_not_degraded"],
                "rework_reduced": result["rework_reduced"],
                "result_sha256": result["result_sha256"],
            }
        )
    payload = _outcomes_bytes(outcomes)
    outcome_sha256 = hashlib.sha256(payload).hexdigest()
    scenario_counts = {
        scenario: sum(item["scenario"] == scenario for item in outcomes)
        for scenario in _BENCHMARK_SCENARIOS
    }
    expected_admit = sum(item["expected"] == "admit" for item in outcomes)
    actual_admit = sum(item["actual"] == "admit" for item in outcomes)
    false_admit = sum(
        item["expected"] == "reject" and item["actual"] == "admit"
        for item in outcomes
    )
    false_reject = sum(
        item["expected"] == "admit" and item["actual"] != "admit"
        for item in outcomes
    )
    benchmark = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": "benchmark/m7-04/decision-branches",
        "samples": samples,
        "successful_samples": samples - false_admit - false_reject,
        "scenario_counts": scenario_counts,
        "expected_admit_count": expected_admit,
        "actual_admit_count": actual_admit,
        "expected_reject_count": samples - expected_admit,
        "actual_reject_count": samples - actual_admit,
        "false_admit_count": false_admit,
        "false_reject_count": false_reject,
        "control_first_count": sum(
            item["first_variant"] == "control" for item in outcomes
        ),
        "treatment_first_count": sum(
            item["first_variant"] == "treatment" for item in outcomes
        ),
        "admitted_quality_nondegradation_count": sum(
            item["actual"] == "admit" and item["quality_not_degraded"]
            for item in outcomes
        ),
        "admitted_rework_reduction_count": sum(
            item["actual"] == "admit" and item["rework_reduced"]
            for item in outcomes
        ),
        "outcomes_artifact_ref": "artifact://sha256/" + outcome_sha256,
        "outcomes_sha256": outcome_sha256,
        "fixture_sha256": _benchmark_fixture_sha256(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "external_calls": 0,
        "state_write_authority": False,
        "completion_authority": False,
    }
    _seal(benchmark, "benchmark_sha256")
    validate_patch_ab_benchmark(benchmark, outcomes_bytes=payload)
    return benchmark, payload


def validate_patch_ab_benchmark(
    benchmark: dict[str, Any], *, outcomes_bytes: bytes
) -> None:
    root = _object(benchmark, _BENCHMARK_FIELDS_V2, "benchmark")
    if root["schema_version"] != BENCHMARK_SCHEMA_VERSION:
        raise PatchABContractError("benchmark schema_version is invalid")
    _identifier(root["benchmark_id"], "benchmark_id")
    samples = _uint(root["samples"], "samples", positive=True, maximum=10000)
    for field in _BENCHMARK_FIELDS_V2 & {
        "successful_samples",
        "expected_admit_count",
        "actual_admit_count",
        "expected_reject_count",
        "actual_reject_count",
        "false_admit_count",
        "false_reject_count",
        "control_first_count",
        "treatment_first_count",
        "admitted_quality_nondegradation_count",
        "admitted_rework_reduction_count",
    }:
        _uint(root[field], field, maximum=samples)
    _artifact(root["outcomes_artifact_ref"], "outcomes_artifact_ref")
    for field in (
        "outcomes_sha256",
        "fixture_sha256",
        "implementation_sha256",
        "benchmark_sha256",
    ):
        _sha256(root[field], field)
    if not isinstance(outcomes_bytes, bytes):
        raise PatchABContractError("benchmark outcomes must be bytes")
    outcomes_sha256 = hashlib.sha256(outcomes_bytes).hexdigest()
    if (
        root["outcomes_sha256"] != outcomes_sha256
        or root["outcomes_artifact_ref"].rsplit("/", 1)[-1] != outcomes_sha256
    ):
        raise PatchABContractError("benchmark outcomes digest mismatch")
    try:
        outcomes = json.loads(outcomes_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PatchABContractError("benchmark outcomes are invalid") from exc
    if not isinstance(outcomes, list) or len(outcomes) != samples:
        raise PatchABContractError("benchmark outcome count mismatch")
    for index, outcome in enumerate(outcomes):
        if not isinstance(outcome, dict) or set(outcome) != {
            "sample",
            "scenario",
            "expected",
            "actual",
            "first_variant",
            "quality_not_degraded",
            "rework_reduced",
            "result_sha256",
        }:
            raise PatchABContractError("benchmark outcome fields are invalid")
        scenario = _BENCHMARK_SCENARIOS[index % len(_BENCHMARK_SCENARIOS)]
        if (
            outcome["sample"] != index
            or outcome["scenario"] != scenario
            or outcome["expected"] != ("admit" if scenario == "valid" else "reject")
            or outcome["actual"] not in {"admit", "reject", "provisional"}
            or outcome["first_variant"] not in {"control", "treatment"}
            or type(outcome["quality_not_degraded"]) is not bool
            or type(outcome["rework_reduced"]) is not bool
        ):
            raise PatchABContractError("benchmark outcome value is invalid")
        if outcome["actual"] == "admit" and not (
            outcome["quality_not_degraded"] and outcome["rework_reduced"]
        ):
            raise PatchABContractError(
                "admitted outcome must satisfy quality and rework gates"
            )
        _sha256(outcome["result_sha256"], "result_sha256")
    expected_admit = sum(item["expected"] == "admit" for item in outcomes)
    actual_admit = sum(item["actual"] == "admit" for item in outcomes)
    false_admit = sum(
        item["expected"] == "reject" and item["actual"] == "admit"
        for item in outcomes
    )
    false_reject = sum(
        item["expected"] == "admit" and item["actual"] != "admit"
        for item in outcomes
    )
    expected_counts = {
        "successful_samples": samples - false_admit - false_reject,
        "expected_admit_count": expected_admit,
        "actual_admit_count": actual_admit,
        "expected_reject_count": samples - expected_admit,
        "actual_reject_count": samples - actual_admit,
        "false_admit_count": false_admit,
        "false_reject_count": false_reject,
        "control_first_count": sum(
            item["first_variant"] == "control" for item in outcomes
        ),
        "treatment_first_count": sum(
            item["first_variant"] == "treatment" for item in outcomes
        ),
        "admitted_quality_nondegradation_count": sum(
            item["actual"] == "admit" and item["quality_not_degraded"]
            for item in outcomes
        ),
        "admitted_rework_reduction_count": sum(
            item["actual"] == "admit" and item["rework_reduced"]
            for item in outcomes
        ),
    }
    if any(root[field] != value for field, value in expected_counts.items()):
        raise PatchABContractError("benchmark aggregate mismatch")
    expected_scenarios = {
        scenario: sum(item["scenario"] == scenario for item in outcomes)
        for scenario in _BENCHMARK_SCENARIOS
    }
    if root["scenario_counts"] != expected_scenarios:
        raise PatchABContractError("benchmark scenario counts mismatch")
    if root["fixture_sha256"] != _benchmark_fixture_sha256():
        raise PatchABContractError("benchmark fixture digest mismatch")
    if root["implementation_sha256"] != hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest():
        raise PatchABContractError("benchmark implementation digest mismatch")
    if root["external_calls"] != 0:
        raise PatchABContractError("benchmark external_calls is invalid")
    _authority_false(root, "benchmark")
    _validate_digest(root, "benchmark_sha256", "benchmark")


def benchmark_patch_ab(*, samples: int = 1000) -> dict[str, Any]:
    benchmark, _ = build_patch_ab_benchmark(samples=samples)
    return benchmark


def build_fixed_patch_ab_fixture() -> dict[str, Any]:
    experiment, artifacts, principals, quality = _build_benchmark_case(0, "valid")
    blind_set = prepare_blind_patch_set(experiment)
    verifier_packet = prepare_verifier_packet(experiment, blind_set)
    scores = _build_benchmark_scores_v2(
        experiment, verifier_packet, "valid", artifacts
    )
    artifact_resolver, principal_resolver, quality_resolver = _fixture_resolvers(
        artifacts, principals, quality
    )
    result = evaluate_patch_ab(
        experiment,
        blind_set,
        scores,
        verifier_packet=verifier_packet,
        principal_resolver=principal_resolver,
        artifact_resolver=artifact_resolver,
        quality_resolver=quality_resolver,
    )
    return {
        "schema_version": "context.patch-ab-fixture/v1alpha2",
        "experiment": experiment,
        "blind_set": blind_set,
        "verifier_packet": verifier_packet,
        "verifier_scores": scores,
        "result": result,
        "artifact_payloads_hex": {
            ref: payload.hex() for ref, payload in sorted(artifacts.items())
        },
        "principal_bindings": dict(sorted(principals.items())),
        "quality_resolutions": copy.deepcopy(quality),
    }
