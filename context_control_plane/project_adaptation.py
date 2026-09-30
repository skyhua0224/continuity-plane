"""Provider-neutral ProjectAdaptation lifecycle and replay contract.

The implementation is intentionally local and deterministic.  It models the
State MCP admission boundary for an adaptation proposal without changing task,
claim, ownership, authorization, validation, evidence, or effect authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

OBSERVATION_SCHEMA_VERSION = "context.project-adaptation-observation/v1alpha1"
PROPOSAL_SCHEMA_VERSION = "context.project-adaptation-proposal/v1alpha1"
REPLAY_SCHEMA_VERSION = "context.project-adaptation-replay/v1alpha1"
TRANSITION_SCHEMA_VERSION = "context.project-adaptation-transition/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_VETO_KEYS = ("E1", "E2", "E4", "E6", "E8", "E9")
_STATUSES = {"candidate", "shadow", "approved", "active", "quarantined", "rejected", "superseded"}
_SCOPES = {"project", "user", "provider-adapter"}
_APPLICABILITY_KINDS = {"project", "user", "repo", "path", "operation", "provider"}
_PREFERENCE_KEYS = {"language", "progress_reporting", "response_density", "response_format", "verbosity"}

_METRIC_FIELDS = {"bytes_read", "bytes_emitted", "repeated_read_bytes", "verification_failures"}
_BUDGET_FIELDS = {"input_tokens", "output_tokens", "tool_calls"}
_CHANGE_FIELDS = {
    "retrieval_order",
    "common_path_refs",
    "common_command_refs",
    "verification_hint_refs",
    "skill_applicability",
    "presentation_preferences",
}
_PROPOSAL_FIELDS = {
    "schema_version",
    "adaptation_id",
    "profile_id",
    "version",
    "proposal_revision",
    "content_sha256",
    "scope",
    "inputs",
    "applicability",
    "changes",
    "metrics_before",
    "metrics_after",
    "safety_veto_results",
    "replay_receipts",
    "expiry",
    "rollback_to",
    "status",
    "approval_round",
    "approval_ref",
    "approved_at",
    "activation_revision",
}


class ProjectAdaptationError(ValueError):
    """Raised when an adaptation proposal or lifecycle transition is unsafe."""


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
        raise ProjectAdaptationError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return value
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ProjectAdaptationError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ProjectAdaptationError(f"{field} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProjectAdaptationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectAdaptationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectAdaptationError(f"{field} requires a timezone")
    return value


def _positive(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ProjectAdaptationError(f"{field} must be a positive integer")
    return value


def _non_negative(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProjectAdaptationError(f"{field} must be a non-negative integer")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise ProjectAdaptationError(f"{field} must be a bounded string list")
    if not allow_empty and not value:
        raise ProjectAdaptationError(f"{field} must not be empty")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProjectAdaptationError(f"{field} contains an invalid string")
    if len(value) != len(set(value)):
        raise ProjectAdaptationError(f"{field} must be unique")
    return value


def _metrics(value: Any, field: str = "metrics") -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _METRIC_FIELDS:
        raise ProjectAdaptationError(f"{field} fields are invalid")
    normalized = dict(value)
    for name, item in normalized.items():
        _non_negative(item, f"{field}.{name}")
    return normalized


def _budget(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _BUDGET_FIELDS:
        raise ProjectAdaptationError("budget fields are invalid")
    normalized = dict(value)
    for name, item in normalized.items():
        _non_negative(item, f"budget.{name}")
    return normalized


def _vetoes(value: Any, field: str = "safety_veto_results") -> dict[str, bool]:
    if type(value) is bool:
        return {key: value for key in _VETO_KEYS}
    if not isinstance(value, Mapping) or set(value) != set(_VETO_KEYS):
        raise ProjectAdaptationError(f"{field} fields are invalid")
    normalized = dict(value)
    if any(type(item) is not bool for item in normalized.values()):
        raise ProjectAdaptationError(f"{field} values must be boolean")
    return normalized


def _changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CHANGE_FIELDS:
        raise ProjectAdaptationError("changes fields are invalid")
    normalized = copy.deepcopy(dict(value))
    for field in (
        "retrieval_order",
        "common_path_refs",
        "common_command_refs",
        "verification_hint_refs",
        "skill_applicability",
    ):
        _strings(normalized[field], f"changes.{field}")
    preferences = normalized["presentation_preferences"]
    if not isinstance(preferences, Mapping):
        raise ProjectAdaptationError("presentation_preferences must be an object")
    if any(
        not isinstance(key, str)
        or key not in _PREFERENCE_KEYS
        or not isinstance(item, str)
        or not item.strip()
        for key, item in preferences.items()
    ):
        raise ProjectAdaptationError("presentation_preferences contains a protected key")
    normalized["presentation_preferences"] = dict(preferences)
    return normalized


def _adaptation_payload(proposal: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        field: copy.deepcopy(proposal[field])
        for field in (
            "profile_id",
            "version",
            "proposal_revision",
            "scope",
            "inputs",
            "applicability",
            "changes",
        )
    }
    payload["inputs"] = sorted(payload["inputs"])
    payload["applicability"] = sorted(
        payload["applicability"], key=lambda item: (item["kind"], item["ref"])
    )
    for field in (
        "retrieval_order",
        "common_path_refs",
        "common_command_refs",
        "verification_hint_refs",
        "skill_applicability",
    ):
        payload["changes"][field] = sorted(payload["changes"][field])
    return payload


def adaptation_content_sha256(proposal: Mapping[str, Any]) -> str:
    """Return the immutable content digest shared with the M2-07 contract."""
    return _digest(_adaptation_payload(proposal))


def validate_adaptation_proposal(proposal: Any) -> None:
    if not isinstance(proposal, Mapping) or set(proposal) != _PROPOSAL_FIELDS:
        raise ProjectAdaptationError("proposal fields are invalid")
    if proposal["schema_version"] != PROPOSAL_SCHEMA_VERSION:
        raise ProjectAdaptationError("proposal schema version is invalid")
    _id(proposal["adaptation_id"], "adaptation_id")
    _id(proposal["profile_id"], "profile_id")
    if not isinstance(proposal["version"], str) or _SEMVER_RE.fullmatch(proposal["version"]) is None:
        raise ProjectAdaptationError("proposal version is invalid")
    _positive(proposal["proposal_revision"], "proposal_revision")
    _sha(proposal["content_sha256"], "content_sha256")
    if proposal["scope"] not in _SCOPES:
        raise ProjectAdaptationError("proposal scope is invalid")
    _strings(proposal["inputs"], "inputs", allow_empty=False)
    applicability = proposal["applicability"]
    if not isinstance(applicability, list) or not applicability:
        raise ProjectAdaptationError("applicability must be non-empty")
    seen: set[tuple[str, str]] = set()
    for item in applicability:
        if not isinstance(item, Mapping) or set(item) != {"kind", "ref"}:
            raise ProjectAdaptationError("applicability item is invalid")
        kind = item["kind"]
        if kind not in _APPLICABILITY_KINDS:
            raise ProjectAdaptationError("applicability kind is invalid")
        ref = _id(item["ref"], "applicability.ref")
        key = (kind, ref)
        if key in seen:
            raise ProjectAdaptationError("applicability must be unique")
        seen.add(key)
    _changes(proposal["changes"])
    _metrics(proposal["metrics_before"], "metrics_before")
    _metrics(proposal["metrics_after"], "metrics_after")
    _vetoes(proposal["safety_veto_results"])
    _strings(proposal["replay_receipts"], "replay_receipts")
    for receipt in proposal["replay_receipts"]:
        if not isinstance(receipt, str) or not receipt.startswith("artifact://sha256/"):
            raise ProjectAdaptationError("replay receipt is invalid")
        _sha(receipt.removeprefix("artifact://sha256/"), "replay receipt digest")
    if proposal["expiry"] is not None:
        _timestamp(proposal["expiry"], "expiry")
    if proposal["rollback_to"] is not None:
        if not isinstance(proposal["rollback_to"], str) or _SEMVER_RE.fullmatch(proposal["rollback_to"]) is None:
            raise ProjectAdaptationError("rollback_to is invalid")
    if proposal["status"] not in _STATUSES:
        raise ProjectAdaptationError("proposal status is invalid")
    _non_negative(proposal["approval_round"], "approval_round")
    if proposal["approval_ref"] is not None:
        _id(proposal["approval_ref"], "approval_ref")
    if proposal["approved_at"] is not None:
        _timestamp(proposal["approved_at"], "approved_at")
    if proposal["activation_revision"] is not None:
        _positive(proposal["activation_revision"], "activation_revision")
    if proposal["content_sha256"] != adaptation_content_sha256(proposal):
        raise ProjectAdaptationError("proposal content hash does not match immutable payload")
    if proposal["status"] in {"approved", "active"}:
        if proposal["approval_round"] < 1 or proposal["approval_ref"] is None or proposal["approved_at"] is None:
            raise ProjectAdaptationError("approved proposal requires complete approval")
        if not proposal["replay_receipts"] or not all(proposal["safety_veto_results"].values()):
            raise ProjectAdaptationError("approved proposal requires replay and safety vetoes")
    if proposal["status"] == "active" and proposal["activation_revision"] is None:
        raise ProjectAdaptationError("active proposal requires activation revision")
    if proposal["status"] not in {"active", "superseded"} and proposal["activation_revision"] is not None:
        raise ProjectAdaptationError("inactive proposal cannot carry activation revision")


def _receipt_ref(receipt: Mapping[str, Any]) -> str:
    return f"artifact://sha256/{receipt['replay_sha256']}"


def _default_evaluator(
    variant: str,
    proposal: Mapping[str, Any],
    fixture_ref: str,
    provider_id: str,
    budget: Mapping[str, int],
) -> dict[str, Any]:
    if variant == "baseline":
        metrics = proposal["metrics_before"]
    else:
        metrics = proposal["metrics_after"]
    return {
        "projection": {
            "variant": variant,
            "fixture_ref": fixture_ref,
            "provider_id": provider_id,
            "budget": dict(budget),
            "changes": proposal["changes"] if variant == "candidate" else {},
        },
        "metrics": metrics,
        "vetoes": _vetoes(True),
    }


class ProjectAdaptationLoop:
    """In-memory observe/propose/shadow/approve/activate/rollback loop."""

    def __init__(
        self,
        *,
        project_id: str,
        profile_id: str,
        clock: Callable[[], str],
    ) -> None:
        self.project_id = _id(project_id, "project_id")
        self.profile_id = _id(profile_id, "profile_id")
        self.clock = clock
        self.profile_revision = 1
        self.observations: dict[str, dict[str, Any]] = {}
        self.proposals: dict[str, dict[str, Any]] = {}
        self.replays: dict[str, dict[str, Any]] = {}
        self.active_version: str | None = None
        self._sequence = 0
        self._authority = {
            "active_task_id": "task-m8-07",
            "claim_id": "claim-m8-07",
            "path_owner_ref": "path://context-control-plane",
            "authorization_revision": 7,
            "validator_revision": 7,
            "evidence_gate_revision": 7,
            "effect_high_watermark": 0,
        }

    def _next_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}://{self.project_id}/{self._sequence}"

    def authority_snapshot(self) -> dict[str, Any]:
        """Expose the protected state boundary for regression assertions."""
        return copy.deepcopy(self._authority)

    def observe(
        self,
        *,
        run_ref: str,
        state_revision: int,
        verified: bool,
        metrics: Mapping[str, Any],
        correction_refs: list[str],
        failure_fixture_refs: list[str],
    ) -> dict[str, Any]:
        _id(run_ref, "run_ref")
        _positive(state_revision, "state_revision")
        if type(verified) is not bool or not verified:
            raise ProjectAdaptationError("only verified runs can be observed")
        normalized = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "observation_id": self._next_id("observation"),
            "project_id": self.project_id,
            "profile_id": self.profile_id,
            "run_ref": run_ref,
            "state_revision": state_revision,
            "verified": verified,
            "metrics": _metrics(metrics),
            "correction_refs": _strings(correction_refs, "correction_refs"),
            "failure_fixture_refs": _strings(failure_fixture_refs, "failure_fixture_refs"),
            "observed_at": _timestamp(self.clock(), "observed_at"),
            "state_write_authority": False,
            "completion_authority": False,
            "provider_native_authority": False,
        }
        normalized["observation_sha256"] = _digest(normalized)
        self.observations[normalized["observation_id"]] = copy.deepcopy(normalized)
        return copy.deepcopy(normalized)

    def propose(
        self,
        *,
        observation_refs: list[str],
        version: str,
        scope: str,
        applicability: list[dict[str, str]],
        changes: Mapping[str, Any],
        metrics_before: Mapping[str, Any],
        metrics_after: Mapping[str, Any],
        expiry: str | None = None,
    ) -> dict[str, Any]:
        refs = _strings(observation_refs, "observation_refs", allow_empty=False)
        if any(ref not in self.observations for ref in refs):
            raise ProjectAdaptationError("proposal references an unknown observation")
        _id(version, "version")
        if _SEMVER_RE.fullmatch(version) is None:
            raise ProjectAdaptationError("proposal version is invalid")
        if scope not in _SCOPES:
            raise ProjectAdaptationError("proposal scope is invalid")
        normalized_applicability = copy.deepcopy(applicability)
        proposal = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "adaptation_id": self._next_id("adaptation"),
            "profile_id": self.profile_id,
            "version": version,
            "proposal_revision": self.profile_revision,
            "content_sha256": "0" * 64,
            "scope": scope,
            "inputs": refs,
            "applicability": normalized_applicability,
            "changes": _changes(changes),
            "metrics_before": _metrics(metrics_before, "metrics_before"),
            "metrics_after": _metrics(metrics_after, "metrics_after"),
            "safety_veto_results": _vetoes(False),
            "replay_receipts": [],
            "expiry": expiry,
            "rollback_to": None,
            "status": "candidate",
            "approval_round": 0,
            "approval_ref": None,
            "approved_at": None,
            "activation_revision": None,
        }
        proposal["content_sha256"] = adaptation_content_sha256(proposal)
        validate_adaptation_proposal(proposal)
        self.proposals[proposal["adaptation_id"]] = copy.deepcopy(proposal)
        return copy.deepcopy(proposal)

    def shadow(
        self,
        adaptation_id: str,
        *,
        fixture_ref: str,
        provider_id: str,
        budget: Mapping[str, Any],
        attempts: int = 3,
        evaluator: Callable[..., Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        proposal = self._proposal(adaptation_id)
        if proposal["status"] not in {"candidate", "shadow", "quarantined"}:
            raise ProjectAdaptationError("only a candidate can enter shadow replay")
        _id(fixture_ref, "fixture_ref")
        _id(provider_id, "provider_id")
        normalized_budget = _budget(budget)
        if type(attempts) is not int or attempts < 3 or attempts > 32:
            raise ProjectAdaptationError("shadow replay requires 3 to 32 attempts")
        evaluator = evaluator or _default_evaluator
        projections: list[dict[str, Any]] = []
        baseline_digests: list[str] = []
        candidate_digests: list[str] = []
        candidate_metrics: dict[str, int] | None = None
        candidate_vetoes: dict[str, bool] | None = None
        for attempt in range(1, attempts + 1):
            row: dict[str, Any] = {"attempt": attempt}
            for variant, digest_list in (("baseline", baseline_digests), ("candidate", candidate_digests)):
                try:
                    result = evaluator(variant, copy.deepcopy(proposal), fixture_ref, provider_id, normalized_budget)
                except Exception as exc:
                    raise ProjectAdaptationError("shadow evaluator failed") from exc
                if not isinstance(result, Mapping) or "projection" not in result:
                    raise ProjectAdaptationError("shadow evaluator result is invalid")
                result_metrics = _metrics(result.get("metrics", proposal["metrics_before"]), f"{variant}.metrics")
                result_vetoes = _vetoes(result.get("vetoes", _vetoes(True)), f"{variant}.vetoes")
                projection_digest = _digest(
                    {"projection": result["projection"], "metrics": result_metrics, "vetoes": result_vetoes}
                )
                digest_list.append(projection_digest)
                row[variant] = {
                    "projection_sha256": projection_digest,
                    "metrics": result_metrics,
                    "safety_veto_results": result_vetoes,
                }
                if variant == "candidate":
                    candidate_metrics = result_metrics
                    candidate_vetoes = result_vetoes
            projections.append(row)
        assert candidate_metrics is not None and candidate_vetoes is not None
        deterministic = len(set(baseline_digests)) == 1 and len(set(candidate_digests)) == 1
        if not _metrics_not_worse(proposal["metrics_before"], candidate_metrics):
            candidate_vetoes = _vetoes(False)
        if not deterministic:
            candidate_vetoes = _vetoes(False)
        receipt = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "replay_id": self._next_id("replay"),
            "proposal_id": proposal["adaptation_id"],
            "proposal_sha256": proposal["content_sha256"],
            "fixture_ref": fixture_ref,
            "provider_id": provider_id,
            "budget": normalized_budget,
            "attempts": attempts,
            "projections": projections,
            "deterministic": deterministic,
            "safety_veto_results": candidate_vetoes,
            "provider_invocations": 0,
            "external_services": 0,
        }
        receipt["replay_sha256"] = _digest(receipt)
        proposal["replay_receipts"] = [_receipt_ref(receipt)]
        proposal["metrics_after"] = candidate_metrics
        proposal["safety_veto_results"] = candidate_vetoes
        proposal["status"] = "shadow" if deterministic and all(candidate_vetoes.values()) else "quarantined"
        validate_adaptation_proposal(proposal)
        self.replays[receipt["replay_id"]] = copy.deepcopy(receipt)
        return copy.deepcopy(receipt)

    def approve(self, adaptation_id: str, *, approval_ref: str, approval_round: int) -> dict[str, Any]:
        proposal = self._proposal(adaptation_id)
        if proposal["status"] != "shadow":
            raise ProjectAdaptationError("proposal requires a successful shadow replay before approval")
        _id(approval_ref, "approval_ref")
        _positive(approval_round, "approval_round")
        proposal["approval_ref"] = approval_ref
        proposal["approval_round"] = approval_round
        proposal["approved_at"] = _timestamp(self.clock(), "approved_at")
        proposal["status"] = "approved"
        validate_adaptation_proposal(proposal)
        return copy.deepcopy(proposal)

    def activate(self, adaptation_id: str) -> dict[str, Any]:
        proposal = self._proposal(adaptation_id)
        if proposal["status"] != "approved":
            raise ProjectAdaptationError("activation requires approval")
        replay = self._latest_replay(proposal)
        if replay["attempts"] < 3 or not replay["deterministic"] or not all(proposal["safety_veto_results"].values()):
            raise ProjectAdaptationError("activation safety gate failed")
        previous = self.active_version
        if previous is not None:
            self.proposals[self._proposal_id_for_version(previous)]["status"] = "superseded"
        self.profile_revision += 1
        proposal["activation_revision"] = self.profile_revision
        proposal["status"] = "active"
        self.active_version = proposal["version"]
        validate_adaptation_proposal(proposal)
        return self._transition(
            "activate",
            proposal,
            from_version=previous,
            to_version=proposal["version"],
        )

    def rollback(self, adaptation_id: str, *, target_version: str) -> dict[str, Any]:
        source = self._proposal(adaptation_id)
        if source["status"] != "active" or self.active_version != source["version"]:
            raise ProjectAdaptationError("rollback source is not active")
        target_id = self._proposal_id_for_version(target_version)
        target = self.proposals[target_id]
        if target["status"] not in {"superseded", "active"}:
            raise ProjectAdaptationError("rollback target lacks activated history")
        replay = self._latest_replay(target)
        if not replay["deterministic"] or not all(target["safety_veto_results"].values()):
            raise ProjectAdaptationError("rollback target safety gate failed")
        source["status"] = "superseded"
        source["rollback_to"] = target["version"]
        self.profile_revision += 1
        target["activation_revision"] = self.profile_revision
        target["status"] = "active"
        self.active_version = target["version"]
        validate_adaptation_proposal(source)
        validate_adaptation_proposal(target)
        return self._transition(
            "rollback",
            target,
            from_version=source["version"],
            to_version=target["version"],
        )

    def opt_out(self) -> dict[str, Any]:
        return self.reset()

    def reset(self) -> dict[str, Any]:
        previous = self.active_version
        if previous is not None:
            self.proposals[self._proposal_id_for_version(previous)]["status"] = "superseded"
            self.active_version = None
        self.profile_revision += 1
        return {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "transition_id": self._next_id("transition"),
            "transition_type": "reset",
            "proposal_sha256": None,
            "from_version": previous,
            "to_version": None,
            "activation_revision": self.profile_revision,
            "safety_veto_results": _vetoes(True),
            "state_write_authority": False,
            "completion_authority": False,
            "provider_native_authority": False,
            "provider_invocations": 0,
            "external_services": 0,
            "status": "reset",
        }

    def _proposal(self, adaptation_id: str) -> dict[str, Any]:
        _id(adaptation_id, "adaptation_id")
        try:
            return self.proposals[adaptation_id]
        except KeyError as exc:
            raise ProjectAdaptationError("proposal is unknown") from exc

    def _proposal_id_for_version(self, version: str) -> str:
        for adaptation_id, proposal in self.proposals.items():
            if proposal["version"] == version:
                return adaptation_id
        raise ProjectAdaptationError("adaptation version is unknown")

    def _latest_replay(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        refs = proposal["replay_receipts"]
        if not refs:
            raise ProjectAdaptationError("proposal has no replay receipt")
        digest = refs[-1].removeprefix("artifact://sha256/")
        for receipt in self.replays.values():
            if receipt["replay_sha256"] == digest:
                return receipt
        raise ProjectAdaptationError("replay receipt is unavailable")

    def _transition(
        self,
        transition_type: str,
        proposal: Mapping[str, Any],
        *,
        from_version: str | None,
        to_version: str,
    ) -> dict[str, Any]:
        transition = {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "transition_id": self._next_id("transition"),
            "transition_type": transition_type,
            "proposal_sha256": proposal["content_sha256"],
            "from_version": from_version,
            "to_version": to_version,
            "proposal_revision": proposal["proposal_revision"],
            "activation_revision": proposal["activation_revision"],
            "safety_veto_results": copy.deepcopy(proposal["safety_veto_results"]),
            "state_write_authority": False,
            "completion_authority": False,
            "provider_native_authority": False,
            "provider_invocations": 0,
            "external_services": 0,
            "status": "rolled_back" if transition_type == "rollback" else "activated",
        }
        transition["transition_sha256"] = _digest(transition)
        return transition


def _metrics_not_worse(before: Mapping[str, int], after: Mapping[str, int]) -> bool:
    return all(after[field] <= before[field] for field in _METRIC_FIELDS)
