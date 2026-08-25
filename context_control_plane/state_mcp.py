"""Transport-neutral State MCP tool contract and authorization boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from .effect_scope_gate import (
    evaluate_claim_scope_gate,
    evaluate_effect_completion_gate,
    evaluate_effect_scope_gate,
    validate_scope,
)
from .experiment_lifecycle import (
    evaluate_attempt_gate,
    evaluate_experiment_activation_gate,
    experiment_contract_sha256,
    experiment_time_verdict,
)
from .idea_continuity import evaluate_idea_capture_gate
from .idea_review import (
    apply_idea_review,
    compute_idea_dedupe_key,
    evaluate_correction_write_gate,
    open_correction_protection,
    release_correction_protection,
    upsert_idea_observation,
)
from .state_events import (
    EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION_V4,
    IDEA_EVENT_SCHEMA_VERSION,
    IDEA_EVENT_SCHEMA_VERSION_V2,
    LEGACY_EVENT_SCHEMA_VERSION,
    StateEventError,
    build_state_event,
    replay_state_events,
)
from .state_store import (
    StateStoreBusy,
    StateStoreCapabilityError,
    StateStoreConflict,
    StateStoreIntegrityError,
    StateStoreNotFound,
    capability_manifest_to_document,
    invoke_state_store,
    validate_state_store_adapter,
)
from .typed_state import DURABLE_EFFECT_SCHEMA_VERSION, TypedStateError

REQUEST_SCHEMA_VERSION = "context.state-mcp-request/v1alpha1"
EFFECT_REQUEST_SCHEMA_VERSION_V2 = "context.state-mcp-request/v2alpha1"
RESPONSE_SCHEMA_VERSION = "context.state-mcp-response/v1alpha1"

READ_TOOL = "context.state.read"
COMMIT_TOOL = "context.state.commit"
CLAIM_TOOL = "context.state.claim"
EFFECT_TOOL = "context.state.effect"
EFFECT_GATE_TOOL = "context.state.effect.gate"
EXPERIMENT_EFFECT_TOOL = "context.experiment.effect"
ATTEMPT_TOOL = "context.experiment.attempt"
PROMOTION_PROPOSE_TOOL = "context.experiment.promotion.propose"
PROMOTION_APPROVE_TOOL = "context.experiment.promotion.approve"
IDEA_CAPTURE_TOOL = "context.idea.capture"
IDEA_REVIEW_TOOL = "context.idea.review"
IDEA_CORRECTION_PROTECT_TOOL = "context.idea.correction.protect"
IDEA_CORRECTION_RELEASE_TOOL = "context.idea.correction.release"
LOCAL_WORK_COMPLETION_TOOL = "context.state.work.complete"
LOCAL_WORK_ACTIVATION_TOOL = "context.state.work.activate"
LOCAL_WORK_TRANSITION_TOOL = "context.state.work.transition"
LOCAL_CLAIM_RECOVERY_TOOL = "context.state.claim.recovery"
DEFAULT_REQUEST_CACHE_ENTRIES = 1024
_AUTHORIZATION_GRANTED = "granted"
_AUTHORIZATION_HISTORICAL_REPLAY = "historical-replay"
_AUTHORIZATION_DENIED = "denied"
STATE_MCP_TOOLS = (
    READ_TOOL,
    COMMIT_TOOL,
    CLAIM_TOOL,
    EFFECT_TOOL,
    EFFECT_GATE_TOOL,
    EXPERIMENT_EFFECT_TOOL,
    ATTEMPT_TOOL,
    PROMOTION_PROPOSE_TOOL,
    PROMOTION_APPROVE_TOOL,
    IDEA_CAPTURE_TOOL,
    IDEA_REVIEW_TOOL,
    IDEA_CORRECTION_PROTECT_TOOL,
    IDEA_CORRECTION_RELEASE_TOOL,
    LOCAL_WORK_COMPLETION_TOOL,
    LOCAL_WORK_ACTIVATION_TOOL,
    LOCAL_WORK_TRANSITION_TOOL,
    LOCAL_CLAIM_RECOVERY_TOOL,
)

ATTEMPT_REQUEST_SCHEMA_VERSION = "context.experiment-attempt-request/v1alpha1"
EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION = "context.experiment-effect-request/v1alpha1"
PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION = (
    "context.experiment-promotion-proposal-request/v1alpha1"
)
PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION = (
    "context.experiment-promotion-approval-request/v1alpha1"
)
IDEA_CAPTURE_REQUEST_SCHEMA_VERSION = "context.idea-capture-request/v1alpha1"
IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2 = "context.idea-capture-request/v2alpha1"
IDEA_REVIEW_REQUEST_SCHEMA_VERSION = "context.idea-review-request/v1alpha1"
IDEA_CORRECTION_PROTECTION_REQUEST_SCHEMA_VERSION = (
    "context.idea-correction-protection-request/v1alpha1"
)
IDEA_CORRECTION_RELEASE_REQUEST_SCHEMA_VERSION = (
    "context.idea-correction-release-request/v1alpha1"
)
LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION = (
    "context.local-work-completion-request/v1alpha1"
)
LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION = (
    "context.local-work-activation-request/v1alpha1"
)
LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION = (
    "context.local-work-transition-request/v1alpha1"
)
LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION = (
    "context.state-claim-recovery-request/v1alpha1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_RANGE_REF_RE = re.compile(r"^rng_[a-z2-7]{26}$")
_COMMON_FIELDS = {"schema_version", "request_id", "project_id"}
_REQUEST_FIELDS = {
    READ_TOOL: _COMMON_FIELDS,
    COMMIT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "causation_ref",
        "correlation_ref",
        "supersedes_event_id",
        "changes",
    },
    CLAIM_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "claim_id",
        "scope_owners",
        "lease_expires_at",
        "causation_ref",
        "correlation_ref",
    },
    EFFECT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "action",
        "effect_id",
        "effect_key",
        "work_id",
        "claim_id",
        "operation",
        "scope_ref",
        "result_ref",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    EFFECT_GATE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "effect_id",
        "work_id",
        "claim_id",
        "operation",
        "scope_ref",
    },
    EXPERIMENT_EFFECT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "action",
        "effect_id",
        "effect_key",
        "work_id",
        "claim_id",
        "attempt_id",
        "operation",
        "scope_ref",
        "result_ref",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    ATTEMPT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "attempt_id",
        "work_id",
        "claim_id",
        "causation_ref",
        "correlation_ref",
    },
    PROMOTION_PROPOSE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "attempt_id",
        "proposal_id",
        "criterion_evidence",
        "causation_ref",
        "correlation_ref",
    },
    PROMOTION_APPROVE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "proposal_id",
        "approval_id",
        "causation_ref",
        "correlation_ref",
    },
    IDEA_CAPTURE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "idea_id",
        "parent_work_id",
        "return_work_id",
        "source_ref",
        "summary",
        "action",
        "switch_target_work_id",
        "expiry",
        "causation_ref",
        "correlation_ref",
    },
    IDEA_REVIEW_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "idea_id",
        "review_id",
        "decision",
        "urgency",
        "impact",
        "review_at",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    IDEA_CORRECTION_PROTECT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "idea_id",
        "protection_id",
        "affected_work_ids",
        "affected_scope_refs",
        "reason",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    IDEA_CORRECTION_RELEASE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "idea_id",
        "protection_id",
        "release_reason",
        "release_evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    LOCAL_WORK_COMPLETION_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "claim_id",
        "checkpoint_ref",
        "evidence",
        "causation_ref",
        "correlation_ref",
    },
    LOCAL_WORK_ACTIVATION_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "work_title",
        "owner_ref",
        "claim_id",
        "scope_owners",
        "source_evidence_id",
        "source_proposal_sha256",
        "checkpoint_ref",
        "lease_expires_at",
        "causation_ref",
        "correlation_ref",
    },
    LOCAL_WORK_TRANSITION_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "claim_id",
        "checkpoint_ref",
        "evidence",
        "return_point_work_id",
        "resolved_blocker_id",
        "successor_claim_id",
        "successor_scope_owners",
        "lease_expires_at",
        "source_proposal_sha256",
        "source_evidence_id",
        "workspace_verification",
        "remaining_blocker",
        "causation_ref",
        "correlation_ref",
    },
    LOCAL_CLAIM_RECOVERY_TOOL: _COMMON_FIELDS
    | {
        "action",
        "expected_revision",
        "claim_id",
        "new_claim_id",
        "actor_ref",
        "scope_owners",
        "lease_ttl_ms",
        "causation_ref",
        "correlation_ref",
    },
}
_IDEA_CAPTURE_REQUEST_FIELDS_V2 = _REQUEST_FIELDS[IDEA_CAPTURE_TOOL] | {
    "scope_ref",
    "urgency",
    "review_at",
    "occurrence_id",
}
_AUTHORIZATION_ACTIONS = {
    READ_TOOL: "state.read",
    COMMIT_TOOL: "state.commit",
    CLAIM_TOOL: "state.claim",
    EFFECT_GATE_TOOL: "state.effect.gate",
    ATTEMPT_TOOL: "state.experiment.attempt.begin",
    PROMOTION_PROPOSE_TOOL: "state.experiment.promotion.propose",
    PROMOTION_APPROVE_TOOL: "state.experiment.promotion.approve",
    IDEA_CAPTURE_TOOL: "state.idea.capture",
    IDEA_REVIEW_TOOL: "state.idea.review",
    IDEA_CORRECTION_PROTECT_TOOL: "state.idea.correction.protect",
    IDEA_CORRECTION_RELEASE_TOOL: "state.idea.correction.release",
    LOCAL_WORK_COMPLETION_TOOL: "state.work.complete",
    LOCAL_WORK_ACTIVATION_TOOL: "state.work.activate",
    LOCAL_WORK_TRANSITION_TOOL: "state.work.transition",
    LOCAL_CLAIM_RECOVERY_TOOL: "state.claim.recovery",
}
_COMMIT_COLLECTION_ID_FIELDS = {
    "works": "work_id",
    "ideas": "idea_id",
    "decisions": "decision_id",
    "constraints": "constraint_id",
    "evidence": "evidence_id",
    "blockers": "blocker_id",
}
_ALL_COLLECTION_ID_FIELDS = {
    **_COMMIT_COLLECTION_ID_FIELDS,
    "claims": "claim_id",
    "effects": "effect_id",
    "experiment_attempts": "attempt_id",
    "experiment_promotions": "promotion_id",
}


def _event_schema_version(snapshot: dict[str, Any]) -> str:
    schema_version = snapshot.get("schema_version")
    if schema_version in {
        "context.typed-state/v3alpha1",
        "context.typed-state/v4alpha1",
        DURABLE_EFFECT_SCHEMA_VERSION,
    }:
        return EVENT_SCHEMA_VERSION_V4
    if schema_version == "context.typed-state/v2alpha1":
        return EVENT_SCHEMA_VERSION
    return LEGACY_EVENT_SCHEMA_VERSION


@dataclass(frozen=True)
class RequestContext:
    """Trusted caller identity supplied by the transport adapter."""

    subject_ref: str
    authorization_ref: str

    def __post_init__(self) -> None:
        for field in ("subject_ref", "authorization_ref"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")


class StateMCPAuthorizer(Protocol):
    """Authorization provider invoked before any project lookup."""

    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool: ...


class _DenyAllAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return False


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": copy.deepcopy(properties),
    }


def _request_properties(tool: str) -> dict[str, Any]:
    common = {
        "schema_version": {"const": REQUEST_SCHEMA_VERSION},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
    }
    if tool == READ_TOOL:
        return common
    if tool == ATTEMPT_TOOL:
        return {
            **common,
            "schema_version": {"const": ATTEMPT_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == IDEA_CAPTURE_TOOL:
        return {
            **common,
            "schema_version": {"const": IDEA_CAPTURE_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "idea_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "parent_work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "return_work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "source_ref": {
                "type": "string",
                "pattern": r"^rng_[a-z2-7]{26}$",
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "action": {"enum": ["capture-and-continue", "park", "propose-switch"]},
            "switch_target_work_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200},
            "expiry": {"type": ["string", "null"], "format": "date-time"},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == IDEA_REVIEW_TOOL:
        return {
            **common,
            "schema_version": {"const": IDEA_REVIEW_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "idea_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "review_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "decision": {"enum": ["keep", "park", "reject", "supersede", "approve"]},
            "urgency": {"enum": ["immediate", "next", "later", "review-date"]},
            "impact": {"enum": ["none", "low", "medium", "high"]},
            "review_at": {"type": ["string", "null"], "format": "date-time"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == IDEA_CORRECTION_PROTECT_TOOL:
        return {
            **common,
            "schema_version": {"const": IDEA_CORRECTION_PROTECTION_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "idea_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "protection_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "affected_work_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "affected_scope_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == IDEA_CORRECTION_RELEASE_TOOL:
        return {
            **common,
            "schema_version": {"const": IDEA_CORRECTION_RELEASE_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "idea_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "protection_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "release_reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "release_evidence_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == EXPERIMENT_EFFECT_TOOL:
        return {
            **common,
            "schema_version": {"const": EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "action": {"enum": ["authorize", "complete"]},
            "effect_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "effect_key": {"type": "string", "minLength": 1, "maxLength": 300},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "operation": {"type": "string", "minLength": 1, "maxLength": 300},
            "scope_ref": {"type": "object"},
            "result_ref": {"type": ["string", "null"]},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == PROMOTION_PROPOSE_TOOL:
        return {
            **common,
            "schema_version": {"const": PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_work_revision": {"type": "integer", "minimum": 0},
            "expected_target_work_revision": {"type": "integer", "minimum": 0},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "proposal_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "criterion_evidence": {"type": "object", "minProperties": 1},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == PROMOTION_APPROVE_TOOL:
        return {
            **common,
            "schema_version": {"const": PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_work_revision": {"type": "integer", "minimum": 0},
            "expected_target_work_revision": {"type": "integer", "minimum": 0},
            "proposal_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "approval_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == COMMIT_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
            "supersedes_event_id": {"type": ["string", "null"]},
            "changes": {"type": "array", "minItems": 1},
        }
    if tool == LOCAL_WORK_COMPLETION_TOOL:
        return {
            **common,
            "schema_version": {
                "const": LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION
            },
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "checkpoint_ref": {"type": "object"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {"type": "object"},
            },
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == LOCAL_WORK_ACTIVATION_TOOL:
        return {
            **common,
            "schema_version": {
                "const": LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION
            },
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "work_title": {"type": "string", "minLength": 1, "maxLength": 512},
            "owner_ref": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "scope_owners": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": {"type": "object"},
            },
            "source_evidence_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "source_proposal_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "checkpoint_ref": {"type": "object"},
            "lease_expires_at": {"type": "string", "format": "date-time"},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == LOCAL_WORK_TRANSITION_TOOL:
        return {
            **common,
            "schema_version": {
                "const": LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION
            },
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "checkpoint_ref": {"type": "object"},
            "evidence": {
                "type": "array",
                "minItems": 2,
                "maxItems": 32,
                "items": {"type": "object"},
            },
            "return_point_work_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "resolved_blocker_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "successor_claim_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "successor_scope_owners": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": {"type": "object"},
            },
            "lease_expires_at": {"type": "string", "format": "date-time"},
            "source_proposal_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "source_evidence_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "workspace_verification": {"type": "object"},
            "remaining_blocker": {"type": ["object", "null"]},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == LOCAL_CLAIM_RECOVERY_TOOL:
        return {
            **common,
            "schema_version": {
                "const": LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION
            },
            "action": {"enum": ["heartbeat", "reclaim"]},
            "expected_revision": {"type": "integer", "minimum": 0},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "new_claim_id": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 256,
            },
            "actor_ref": {"type": "string", "minLength": 1, "maxLength": 256},
            "scope_owners": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scope_kind", "scope_ref"],
                    "properties": {
                        "scope_kind": {"type": "string", "minLength": 1},
                        "scope_ref": {"type": "string", "minLength": 1},
                    },
                },
            },
            "lease_ttl_ms": {"type": "integer", "minimum": 1},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == CLAIM_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "scope_owners": {"type": "array", "minItems": 1},
            "lease_expires_at": {"type": "string", "format": "date-time"},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == EFFECT_GATE_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "effect_id": {"type": "string", "minLength": 1},
            "work_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "operation": {"type": "string", "minLength": 1},
            "scope_ref": {"type": "object"},
        }
    return {
        **common,
        "expected_revision": {"type": "integer", "minimum": 0},
        "action": {"enum": ["authorize", "complete"]},
        "effect_id": {"type": "string", "minLength": 1},
        "effect_key": {"type": "string", "minLength": 1},
        "work_id": {"type": "string", "minLength": 1},
        "claim_id": {"type": "string", "minLength": 1},
        "operation": {"type": "string", "minLength": 1},
        "scope_ref": {"type": "object"},
        "result_ref": {"type": ["string", "null"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "causation_ref": {"type": ["string", "null"]},
        "correlation_ref": {"type": ["string", "null"]},
    }


def _effect_v2_request_properties() -> dict[str, Any]:
    properties = _request_properties(EFFECT_TOOL)
    properties["schema_version"] = {"const": EFFECT_REQUEST_SCHEMA_VERSION_V2}
    properties["request_sha256"] = {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
    }
    return properties


def _idea_capture_v2_request_properties() -> dict[str, Any]:
    properties = _request_properties(IDEA_CAPTURE_TOOL)
    properties["schema_version"] = {"const": IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2}
    properties.update(
        {
            "scope_ref": {"type": "string", "minLength": 1, "maxLength": 500},
            "urgency": {"enum": ["immediate", "next", "later", "review-date"]},
            "review_at": {"type": ["string", "null"], "format": "date-time"},
            "occurrence_id": {"type": "string", "minLength": 1, "maxLength": 200},
        }
    )
    return properties


def state_mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return provider-neutral MCP tool metadata with strict input schemas."""
    descriptions = {
        READ_TOOL: "Read one authorized typed-state snapshot and its event head.",
        COMMIT_TOOL: "Commit an authorized non-claim state intent with CAS.",
        CLAIM_TOOL: "Claim one ready Work atomically with CAS.",
        EFFECT_TOOL: "Authorize or complete one claimed external effect with CAS.",
        EFFECT_GATE_TOOL: "Evaluate one effect authorization gate without a state write.",
        EXPERIMENT_EFFECT_TOOL: "Authorize or complete one Experiment effect bound to a persisted attempt.",
        ATTEMPT_TOOL: "Persist one authorized Experiment attempt before external effects.",
        PROMOTION_PROPOSE_TOOL: "Propose verified Experiment findings for a canonical target.",
        PROMOTION_APPROVE_TOOL: "Approve a proposed Experiment promotion with an independent verifier.",
        IDEA_CAPTURE_TOOL: "Capture a candidate Idea without changing active execution authority.",
        IDEA_REVIEW_TOOL: "Record a bounded Idea review without granting execution authority.",
        IDEA_CORRECTION_PROTECT_TOOL: "Protect affected writes while an Idea correction is unresolved.",
        IDEA_CORRECTION_RELEASE_TOOL: "Release an Idea correction protection after verified evidence.",
        LOCAL_WORK_COMPLETION_TOOL: "Atomically complete local Work and release its active claim with verified evidence.",
        LOCAL_WORK_ACTIVATION_TOOL: "Atomically create or activate one source-bound Work, issue its claim, and publish its checkpoint.",
        LOCAL_WORK_TRANSITION_TOOL: "Atomically complete one dependency Work, resolve its declared blocker, and claim its predeclared return point.",
        LOCAL_CLAIM_RECOVERY_TOOL: "Heartbeat a live legacy local claim or reclaim an expired claim with a new identity.",
    }
    return [
        {
            "name": tool,
            "description": descriptions[tool],
            "inputSchema": (
                {
                    "oneOf": [
                        _object_schema(_request_properties(IDEA_CAPTURE_TOOL)),
                        _object_schema(_idea_capture_v2_request_properties()),
                    ]
                }
                if tool == IDEA_CAPTURE_TOOL
                else (
                    {
                        "oneOf": [
                            _object_schema(_request_properties(EFFECT_TOOL)),
                            _object_schema(_effect_v2_request_properties()),
                        ]
                    }
                    if tool == EFFECT_TOOL
                    else _object_schema(_request_properties(tool))
                )
            ),
        }
        for tool in STATE_MCP_TOOLS
    ]


def _response(
    *,
    tool: str,
    request_id: str | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    ok = error_code is None
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "request_id": request_id,
        "tool": tool,
        "ok": ok,
        "result": copy.deepcopy(result) if ok else None,
        "error": (
            None
            if ok
            else {
                "code": error_code,
                "message": error_message or "request failed",
            }
        ),
    }


def _request_id(arguments: Any) -> str | None:
    if isinstance(arguments, dict) and isinstance(arguments.get("request_id"), str):
        return arguments["request_id"]
    return None


def _validate_request(tool: str, arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "request fields do not match the tool contract"
    if (
        tool == IDEA_CAPTURE_TOOL
        and arguments.get("schema_version") == IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2
    ):
        expected_fields = _IDEA_CAPTURE_REQUEST_FIELDS_V2
    elif (
        tool == EFFECT_TOOL
        and arguments.get("schema_version") == EFFECT_REQUEST_SCHEMA_VERSION_V2
    ):
        expected_fields = _REQUEST_FIELDS[tool] | {"request_sha256"}
    else:
        expected_fields = _REQUEST_FIELDS[tool]
    actual_fields = set(arguments)
    if actual_fields != expected_fields:
        return "request fields do not match the tool contract"
    expected_schema_version = {
        ATTEMPT_TOOL: ATTEMPT_REQUEST_SCHEMA_VERSION,
        EXPERIMENT_EFFECT_TOOL: EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION,
        PROMOTION_PROPOSE_TOOL: PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION,
        PROMOTION_APPROVE_TOOL: PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION,
        IDEA_CAPTURE_TOOL: arguments.get("schema_version"),
        EFFECT_TOOL: arguments.get("schema_version"),
        IDEA_REVIEW_TOOL: IDEA_REVIEW_REQUEST_SCHEMA_VERSION,
        IDEA_CORRECTION_PROTECT_TOOL: IDEA_CORRECTION_PROTECTION_REQUEST_SCHEMA_VERSION,
        IDEA_CORRECTION_RELEASE_TOOL: IDEA_CORRECTION_RELEASE_REQUEST_SCHEMA_VERSION,
        LOCAL_WORK_COMPLETION_TOOL: LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
        LOCAL_WORK_ACTIVATION_TOOL: LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION,
        LOCAL_WORK_TRANSITION_TOOL: LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION,
        LOCAL_CLAIM_RECOVERY_TOOL: LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
    }.get(tool, REQUEST_SCHEMA_VERSION)
    supported_request_versions = {
        IDEA_CAPTURE_TOOL: {
            IDEA_CAPTURE_REQUEST_SCHEMA_VERSION,
            IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2,
        },
        EFFECT_TOOL: {REQUEST_SCHEMA_VERSION, EFFECT_REQUEST_SCHEMA_VERSION_V2},
    }
    if arguments["schema_version"] != expected_schema_version or (
        tool in supported_request_versions
        and expected_schema_version not in supported_request_versions[tool]
    ):
        return "unsupported request schema_version"
    for field in ("request_id", "project_id"):
        value = arguments[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            return f"{field} must be a bounded non-empty string"
    if tool == READ_TOOL:
        return None
    revision = arguments["expected_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return "expected_revision must be a non-negative integer"
    if tool != EFFECT_GATE_TOOL:
        for field in ("causation_ref", "correlation_ref"):
            value = arguments[field]
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 500
            ):
                return f"{field} must be null or a bounded non-empty string"
    if tool == COMMIT_TOOL:
        supersedes = arguments["supersedes_event_id"]
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or not supersedes.strip()
            or len(supersedes) > 200
        ):
            return "supersedes_event_id must be null or a bounded string"
        changes = arguments["changes"]
        if not isinstance(changes, list) or not changes or len(changes) > 100:
            return "changes must be a bounded non-empty array"
        for change in changes:
            if not isinstance(change, dict) or set(change) != {
                "collection",
                "object_id",
                "value",
            }:
                return "change fields do not match the contract"
            collection = change["collection"]
            if collection not in _COMMIT_COLLECTION_ID_FIELDS:
                return "claims and effects require dedicated State MCP tools"
            object_id = change["object_id"]
            value = change["value"]
            if (
                not isinstance(object_id, str)
                or not object_id.strip()
                or not isinstance(value, dict)
                or value.get(_COMMIT_COLLECTION_ID_FIELDS[collection]) != object_id
            ):
                return "change identity is invalid"
    if tool == CLAIM_TOOL:
        for field in ("work_id", "claim_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        scopes = arguments["scope_owners"]
        if not isinstance(scopes, list) or not scopes or len(scopes) > 100:
            return "scope_owners must be a bounded non-empty array"
        for scope in scopes:
            if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
                return "scope_owners entries do not match the contract"
            if scope["scope_kind"] not in {
                "repo",
                "directory",
                "file",
                "symbol",
                "capability",
                "effect",
            } or not isinstance(scope["scope_ref"], str) or not scope["scope_ref"].strip():
                return "scope_owners entry is invalid"
        lease = arguments["lease_expires_at"]
        if not isinstance(lease, str) or not lease.strip():
            return "lease_expires_at must be a non-empty RFC3339 string"
        try:
            parsed_lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            return "lease_expires_at must be a valid RFC3339 timestamp"
        if parsed_lease.tzinfo is None:
            return "lease_expires_at must include a timezone"
    if tool == LOCAL_WORK_ACTIVATION_TOOL:
        for field in (
            "work_id",
            "owner_ref",
            "claim_id",
            "source_evidence_id",
        ):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        title = arguments["work_title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 512:
            return "work_title must be bounded and non-empty"
        scopes = arguments["scope_owners"]
        if not isinstance(scopes, list) or not scopes or len(scopes) > 128:
            return "scope_owners must be a bounded non-empty array"
        try:
            for scope in scopes:
                validate_scope(scope)
        except (TypeError, ValueError):
            return "scope_owners entry is invalid"
        source_sha256 = arguments["source_proposal_sha256"]
        if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
            return "source_proposal_sha256 must be lowercase SHA-256"
        source_evidence_id = arguments["source_evidence_id"]
        if (
            not isinstance(source_evidence_id, str)
            or not source_evidence_id.startswith("evidence-attach-")
            or len(source_evidence_id) > 200
        ):
            return "source_evidence_id must identify bounded attach evidence"
        lease = arguments["lease_expires_at"]
        if not isinstance(lease, str) or not lease.strip():
            return "lease_expires_at must be a non-empty RFC3339 string"
        try:
            parsed_lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            return "lease_expires_at must be a valid RFC3339 timestamp"
        if parsed_lease.tzinfo is None:
            return "lease_expires_at must include a timezone"
        checkpoint_ref = arguments["checkpoint_ref"]
        checkpoint_fields = {
            "schema_version",
            "digest_algorithm",
            "digest",
            "size_bytes",
            "artifact_uri",
        }
        if (
            not isinstance(checkpoint_ref, dict)
            or set(checkpoint_ref) != checkpoint_fields
            or checkpoint_ref["schema_version"] != "context.artifact-ref/v1alpha1"
            or checkpoint_ref["digest_algorithm"] != "sha-256"
            or not isinstance(checkpoint_ref["digest"], str)
            or _SHA256_RE.fullmatch(checkpoint_ref["digest"]) is None
            or type(checkpoint_ref["size_bytes"]) is not int
            or checkpoint_ref["size_bytes"] <= 0
            or checkpoint_ref["artifact_uri"]
            != f"artifact://sha256/{checkpoint_ref['digest']}"
        ):
            return "checkpoint_ref is invalid"
    if tool in {LOCAL_WORK_COMPLETION_TOOL, LOCAL_WORK_TRANSITION_TOOL}:
        for field in ("work_id", "claim_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        checkpoint_ref = arguments["checkpoint_ref"]
        checkpoint_fields = {
            "schema_version",
            "digest_algorithm",
            "digest",
            "size_bytes",
            "artifact_uri",
        }
        if (
            not isinstance(checkpoint_ref, dict)
            or set(checkpoint_ref) != checkpoint_fields
            or checkpoint_ref["schema_version"] != "context.artifact-ref/v1alpha1"
            or checkpoint_ref["digest_algorithm"] != "sha-256"
            or not isinstance(checkpoint_ref["digest"], str)
            or _SHA256_RE.fullmatch(checkpoint_ref["digest"]) is None
            or type(checkpoint_ref["size_bytes"]) is not int
            or checkpoint_ref["size_bytes"] <= 0
            or checkpoint_ref["artifact_uri"]
            != f"artifact://sha256/{checkpoint_ref['digest']}"
        ):
            return "checkpoint_ref is invalid"
        evidence = arguments["evidence"]
        evidence_fields = {
            "evidence_id",
            "kind",
            "artifact_ref",
            "content_sha256",
            "validity",
            "observed_at",
            "verified_at",
        }
        if not isinstance(evidence, list) or not evidence or len(evidence) > 32:
            return "evidence must be a bounded non-empty array"
        evidence_ids = []
        for item in evidence:
            if not isinstance(item, dict) or set(item) != evidence_fields:
                return "completion evidence fields are invalid"
            evidence_ids.append(item["evidence_id"])
        if (
            any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            return "completion evidence IDs must be unique non-empty strings"
        checkpoint_evidence = [
            item
            for item in evidence
            if item["kind"] == "artifact"
            and item["artifact_ref"] == checkpoint_ref["artifact_uri"]
            and item["content_sha256"] == checkpoint_ref["digest"]
            and item["validity"] == "verified"
        ]
        if len(checkpoint_evidence) != 1:
            return "completion requires one verified checkpoint evidence"
    if tool == LOCAL_WORK_TRANSITION_TOOL:
        for field in (
            "return_point_work_id",
            "resolved_blocker_id",
            "successor_claim_id",
        ):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        if arguments["work_id"] == arguments["return_point_work_id"]:
            return "return point must differ from completed Work"
        scopes = arguments["successor_scope_owners"]
        if not isinstance(scopes, list) or not scopes or len(scopes) > 128:
            return "successor_scope_owners must be a bounded non-empty array"
        for scope in scopes:
            if not isinstance(scope, dict) or set(scope) != {
                "scope_kind",
                "scope_ref",
            }:
                return "successor scope fields are invalid"
            try:
                validate_scope(scope)
            except (TypeError, ValueError):
                return "successor scope is invalid"
        lease = arguments["lease_expires_at"]
        if not isinstance(lease, str) or not lease.strip():
            return "lease_expires_at must be a non-empty RFC3339 string"
        try:
            parsed_lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            return "lease_expires_at must be a valid RFC3339 timestamp"
        if parsed_lease.tzinfo is None:
            return "lease_expires_at must include a timezone"
        source_sha256 = arguments["source_proposal_sha256"]
        if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
            return "source_proposal_sha256 must be lowercase SHA-256"
        source_evidence_id = arguments["source_evidence_id"]
        if (
            not isinstance(source_evidence_id, str)
            or not source_evidence_id.startswith("evidence-attach-")
            or len(source_evidence_id) > 200
        ):
            return "source_evidence_id must identify bounded attach evidence"
        workspace = arguments["workspace_verification"]
        if not isinstance(workspace, dict) or set(workspace) != {
            "head_commit",
            "clean",
            "expected_ref",
            "expected_ref_commit",
        }:
            return "workspace_verification fields are invalid"
        if (
            not isinstance(workspace["head_commit"], str)
            or re.fullmatch(r"[0-9a-f]{40}", workspace["head_commit"]) is None
            or workspace["clean"] is not True
        ):
            return "workspace_verification is not clean or content-bound"
        expected_ref = workspace["expected_ref"]
        expected_ref_commit = workspace["expected_ref_commit"]
        if (expected_ref is None) != (expected_ref_commit is None):
            return "workspace expected ref and commit must be supplied together"
        if expected_ref is not None and (
            not isinstance(expected_ref, str)
            or not expected_ref.strip()
            or len(expected_ref) > 500
            or not isinstance(expected_ref_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", expected_ref_commit) is None
            or expected_ref_commit != workspace["head_commit"]
        ):
            return "workspace expected ref does not match HEAD"
        remaining = arguments["remaining_blocker"]
        if remaining is not None:
            if not isinstance(remaining, dict) or set(remaining) != {
                "blocker_id",
                "reason",
                "evidence_ids",
            }:
                return "remaining_blocker fields are invalid"
            if (
                not isinstance(remaining["blocker_id"], str)
                or not remaining["blocker_id"].strip()
                or len(remaining["blocker_id"]) > 200
                or not isinstance(remaining["reason"], str)
                or not remaining["reason"].strip()
                or len(remaining["reason"]) > 2_000
            ):
                return "remaining_blocker identity or reason is invalid"
            evidence_ids = remaining["evidence_ids"]
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or len(evidence_ids) > 32
                or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
                or len(evidence_ids) != len(set(evidence_ids))
            ):
                return "remaining blocker evidence IDs are invalid"
    if tool == LOCAL_CLAIM_RECOVERY_TOOL:
        if arguments["action"] not in {"heartbeat", "reclaim"}:
            return "claim recovery action is unsupported"
        for field in ("claim_id", "actor_ref"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                return f"{field} must be a bounded non-empty string"
        new_claim_id = arguments["new_claim_id"]
        if arguments["action"] == "reclaim":
            if (
                not isinstance(new_claim_id, str)
                or not new_claim_id.strip()
                or len(new_claim_id) > 256
            ):
                return "new_claim_id is required for reclaim"
        elif new_claim_id is not None:
            return "new_claim_id must be null for heartbeat"
        scopes = arguments["scope_owners"]
        if not isinstance(scopes, list) or not scopes:
            return "scope_owners must be a bounded non-empty array"
        for scope in scopes:
            if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
                return "scope_owners entry is invalid"
            if (
                not isinstance(scope["scope_kind"], str)
                or not scope["scope_kind"]
                or not isinstance(scope["scope_ref"], str)
                or not scope["scope_ref"]
            ):
                return "scope_owners entry is invalid"
        ttl = arguments["lease_ttl_ms"]
        if type(ttl) is not int or ttl <= 0 or ttl > 7 * 24 * 60 * 60 * 1000:
            return "lease_ttl_ms is outside the configured bound"
    if tool == ATTEMPT_TOOL:
        for field in ("attempt_id", "work_id", "claim_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
    if tool == IDEA_CAPTURE_TOOL:
        for field in ("idea_id", "parent_work_id", "return_work_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        source_ref = arguments["source_ref"]
        if not isinstance(source_ref, str) or not _OPAQUE_RANGE_REF_RE.fullmatch(
            source_ref
        ):
            return "source_ref must be an opaque source range reference"
        summary = arguments["summary"]
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            return "summary must be a bounded non-empty string"
        action = arguments["action"]
        target = arguments["switch_target_work_id"]
        if action not in {"capture-and-continue", "park", "propose-switch"}:
            return "action is unsupported"
        if (action == "propose-switch") != (isinstance(target, str) and bool(target.strip())):
            return "switch_target_work_id does not match action"
        if isinstance(target, str) and len(target) > 200:
            return "switch_target_work_id must be bounded"
        expiry = arguments["expiry"]
        if expiry is not None:
            if not isinstance(expiry, str) or not expiry.strip():
                return "expiry must be null or RFC3339"
            try:
                parsed_expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            except ValueError:
                return "expiry must be null or RFC3339"
            if parsed_expiry.tzinfo is None:
                return "expiry must include a timezone"
        if arguments["schema_version"] == IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2:
            scope_ref = arguments["scope_ref"]
            if not isinstance(scope_ref, str) or not scope_ref.strip() or len(scope_ref) > 500:
                return "scope_ref must be a bounded non-empty string"
            if arguments["urgency"] not in {"immediate", "next", "later", "review-date"}:
                return "urgency is unsupported"
            review_at = arguments["review_at"]
            if arguments["urgency"] == "review-date" and review_at is None:
                return "review-date urgency requires review_at"
            if review_at is not None:
                if not isinstance(review_at, str) or not review_at.strip():
                    return "review_at must be null or RFC3339"
                try:
                    parsed_review_at = datetime.fromisoformat(review_at.replace("Z", "+00:00"))
                except ValueError:
                    return "review_at must be null or RFC3339"
                if parsed_review_at.tzinfo is None:
                    return "review_at must include a timezone"
            occurrence_id = arguments["occurrence_id"]
            if (
                not isinstance(occurrence_id, str)
                or not occurrence_id.strip()
                or len(occurrence_id) > 200
            ):
                return "occurrence_id must be a bounded non-empty string"
    if tool == IDEA_REVIEW_TOOL:
        for field in ("idea_id", "review_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        if arguments["decision"] not in {"keep", "park", "reject", "supersede", "approve"}:
            return "review decision is unsupported"
        if arguments["urgency"] not in {"immediate", "next", "later", "review-date"}:
            return "urgency is unsupported"
        if arguments["impact"] not in {"none", "low", "medium", "high"}:
            return "impact is unsupported"
        if arguments["urgency"] == "review-date" and arguments["review_at"] is None:
            return "review-date urgency requires review_at"
        if arguments["review_at"] is not None:
            try:
                parsed = datetime.fromisoformat(arguments["review_at"].replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                return "review_at must be null or RFC3339"
            if parsed.tzinfo is None:
                return "review_at must include a timezone"
        evidence_ids = arguments["evidence_ids"]
        if (
            not isinstance(evidence_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            return "evidence_ids must contain unique non-empty strings"
    if tool == IDEA_CORRECTION_PROTECT_TOOL:
        for field in ("idea_id", "protection_id", "reason"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                return f"{field} must be a bounded non-empty string"
        for field in ("affected_work_ids", "affected_scope_refs", "evidence_ids"):
            values = arguments[field]
            if (
                not isinstance(values, list)
                or (field != "evidence_ids" and not values)
                or any(not isinstance(item, str) or not item.strip() for item in values)
                or len(values) != len(set(values))
            ):
                return f"{field} must contain unique non-empty strings"
    if tool == IDEA_CORRECTION_RELEASE_TOOL:
        for field in ("idea_id", "protection_id", "release_reason"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                return f"{field} must be a bounded non-empty string"
        values = arguments["release_evidence_ids"]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            return "release_evidence_ids must contain unique non-empty strings"
    if tool in {PROMOTION_PROPOSE_TOOL, PROMOTION_APPROVE_TOOL}:
        for field in ("work_id", "proposal_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        for field in ("expected_work_revision", "expected_target_work_revision"):
            value = arguments[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return f"{field} must be a non-negative integer"
        if tool == PROMOTION_PROPOSE_TOOL:
            attempt_id = arguments["attempt_id"]
            if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 200:
                return "attempt_id must be a bounded non-empty string"
            criteria = arguments["criterion_evidence"]
            if not isinstance(criteria, dict) or not criteria:
                return "criterion_evidence must be a non-empty object"
            for criterion, evidence_ids in criteria.items():
                if not isinstance(criterion, str) or not criterion.strip():
                    return "criterion_evidence keys must be non-empty strings"
                if (
                    not isinstance(evidence_ids, list)
                    or not evidence_ids
                    or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
                    or len(evidence_ids) != len(set(evidence_ids))
                ):
                    return "criterion evidence must contain unique non-empty strings"
        else:
            approval_id = arguments["approval_id"]
            if not isinstance(approval_id, str) or not approval_id.strip() or len(approval_id) > 200:
                return "approval_id must be a bounded non-empty string"
    if tool in {EFFECT_TOOL, EFFECT_GATE_TOOL, EXPERIMENT_EFFECT_TOOL}:
        for field in ("effect_id", "work_id", "claim_id", "operation"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                return f"{field} must be a bounded non-empty string"
        scope = arguments["scope_ref"]
        if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
            return "scope_ref is invalid"
        if scope["scope_kind"] not in {
            "repo",
            "directory",
            "file",
            "symbol",
            "capability",
            "effect",
        } or not isinstance(scope["scope_ref"], str) or not scope["scope_ref"].strip():
            return "scope_ref is invalid"
    if tool in {EFFECT_TOOL, EXPERIMENT_EFFECT_TOOL}:
        if arguments["action"] not in {"authorize", "complete"}:
            return "action is unsupported"
        for field in ("effect_key",):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                return f"{field} must be a bounded non-empty string"
        request_sha256 = arguments.get("request_sha256")
        if tool == EFFECT_TOOL and arguments["schema_version"] == EFFECT_REQUEST_SCHEMA_VERSION_V2 and (
            not isinstance(request_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
        ):
            return "request_sha256 must be a lowercase SHA-256"
        evidence_ids = arguments["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in evidence_ids
        ) or len(evidence_ids) != len(set(evidence_ids)):
            return "evidence_ids must contain unique non-empty strings"
        if arguments["action"] == "complete":
            result_ref = arguments["result_ref"]
            if not isinstance(result_ref, str) or not result_ref.strip():
                return "complete requires result_ref"
        elif arguments["result_ref"] is not None:
            return "authorize requires a null result_ref"
        if tool == EXPERIMENT_EFFECT_TOOL:
            attempt_id = arguments["attempt_id"]
            if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 200:
                return "attempt_id must be a bounded non-empty string"
    return None


def _replace_or_append(
    snapshot: dict[str, Any],
    change: dict[str, Any],
) -> None:
    collection = change["collection"]
    object_id = change["object_id"]
    id_field = _ALL_COLLECTION_ID_FIELDS[collection]
    values = snapshot[collection]
    index = next(
        (index for index, item in enumerate(values) if item[id_field] == object_id),
        None,
    )
    if index is None:
        values.append(copy.deepcopy(change["value"]))
    else:
        values[index] = copy.deepcopy(change["value"])


def _upsert_change(
    changes: list[dict[str, Any]],
    *,
    collection: str,
    value: dict[str, Any],
) -> None:
    id_field = _ALL_COLLECTION_ID_FIELDS[collection]
    object_id = value[id_field]
    replacement = {
        "collection": collection,
        "object_id": object_id,
        "value": copy.deepcopy(value),
    }
    for index, change in enumerate(changes):
        if change["collection"] == collection and change["object_id"] == object_id:
            changes[index] = replacement
            return
    changes.append(replacement)


def _derive_project_projection(
    snapshot: dict[str, Any],
    *,
    revision: int,
    updated_at: str,
) -> None:
    project = snapshot["project"]
    active_work_ids = sorted(
        item["work_id"] for item in snapshot["works"] if item["status"] == "active"
    )
    current_primary = project["primary_work_id"]
    project.update(
        {
            "revision": revision,
            "active_work_ids": active_work_ids,
            "primary_work_id": (
                current_primary
                if current_primary in active_work_ids
                else (active_work_ids[0] if active_work_ids else None)
            ),
            "current_decision_ids": sorted(
                item["decision_id"]
                for item in snapshot["decisions"]
                if item["status"] == "accepted"
            ),
            "active_constraint_ids": sorted(
                item["constraint_id"]
                for item in snapshot["constraints"]
                if item["status"] == "active"
            ),
            "open_blocker_ids": sorted(
                item["blocker_id"]
                for item in snapshot["blockers"]
                if item["status"] == "open"
            ),
            "effect_high_watermark": max(
                (
                    item["sequence_no"]
                    for item in snapshot["effects"]
                    if item["status"] in {"succeeded", "failed", "compensated"}
                ),
                default=0,
            ),
            "updated_at": updated_at,
        }
    )


def _backend_error_response(
    *,
    tool: str,
    request_id: str | None,
    error: Exception,
) -> dict[str, Any]:
    if isinstance(error, StateStoreNotFound):
        code, message = "not_found", "project was not found"
    elif isinstance(error, StateStoreConflict):
        code, message = "conflict", "state changed concurrently"
    elif isinstance(error, StateStoreBusy):
        code, message = "busy", "state store is busy"
    elif isinstance(error, StateStoreCapabilityError):
        code, message = "capability", "state-store capability contract rejected the operation"
    elif isinstance(
        error,
        (StateStoreIntegrityError, StateEventError, TypedStateError, ValueError),
    ):
        code, message = "integrity", "state integrity validation failed"
    else:
        raise error
    return _response(
        tool=tool,
        request_id=request_id,
        error_code=code,
        error_message=message,
    )


class StateMCPService:
    """Apply State MCP authorization before dispatching to a StateStore."""

    def __init__(
        self,
        store: Any,
        *,
        authorizer: StateMCPAuthorizer | None = None,
        registry_digest: str,
        clock: Callable[[], str],
        event_id_factory: Callable[[str], str],
        transition_checkpoint_publisher: Callable[[dict[str, Any]], dict[str, Any]]
        | None = None,
    ) -> None:
        self._manifest = validate_state_store_adapter(store)
        if not isinstance(registry_digest, str) or not _SHA256_RE.fullmatch(
            registry_digest
        ):
            raise ValueError("registry_digest must be lowercase SHA-256")
        if not callable(clock) or not callable(event_id_factory):
            raise TypeError("clock and event_id_factory must be callable")
        if transition_checkpoint_publisher is not None and not callable(
            transition_checkpoint_publisher
        ):
            raise TypeError("transition_checkpoint_publisher must be callable")
        self._store = store
        self._authorizer = authorizer or _DenyAllAuthorizer()
        self._registry_hash_value = registry_digest
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._transition_checkpoint_publisher = transition_checkpoint_publisher
        self._request_receipts: dict[
            tuple[str, str, str], tuple[str, dict[str, Any]]
        ] = {}
        self._authorization_receipts: dict[
            tuple[str, str, str, str], dict[str, Any]
        ] = {}
        self._request_cache_limit = DEFAULT_REQUEST_CACHE_ENTRIES
        self._mutation_lock = threading.Lock()

    def _cache_put(self, cache: dict[Any, Any], key: Any, value: Any) -> None:
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > self._request_cache_limit:
            del cache[next(iter(cache))]

    @staticmethod
    def _request_fingerprint(
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> str:
        payload = {
            "tool": tool,
            "arguments": arguments,
            "subject_ref": context.subject_ref,
            "authorization_ref": context.authorization_ref,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _request_replay(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> dict[str, Any] | None:
        previous = self._request_receipts.get(
            (arguments["project_id"], tool, arguments["request_id"])
        )
        if previous is None:
            return None
        if previous[0] != self._request_fingerprint(tool, arguments, context):
            return _response(
                tool=tool,
                request_id=arguments["request_id"],
                error_code="conflict",
                error_message="request_id was already used for a different intent",
            )
        return copy.deepcopy(previous[1])

    def _remember_request(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
        response: dict[str, Any],
    ) -> None:
        self._cache_put(
            self._request_receipts,
            (arguments["project_id"], tool, arguments["request_id"]),
            (
                self._request_fingerprint(tool, arguments, context),
                copy.deepcopy(response),
            ),
        )

    def _authorize(
        self,
        *,
        context: RequestContext,
        action: str,
        project_id: str,
        request_id: str,
        request_sha256: str,
    ) -> str:
        try:
            authorize_with_receipt = getattr(
                self._authorizer, "authorize_with_receipt", None
            )
            if callable(authorize_with_receipt):
                outcome = authorize_with_receipt(
                    context,
                    action,
                    project_id,
                    request_id=request_id,
                    request_sha256=request_sha256,
                )
                if not isinstance(outcome, dict):
                    return _AUTHORIZATION_DENIED
                if set(outcome) == {
                    "audit_event",
                    "currently_authorized",
                    "historical_replay_only",
                }:
                    receipt = outcome["audit_event"]
                    currently_authorized = outcome["currently_authorized"] is True
                    historical_replay_only = (
                        outcome["historical_replay_only"] is True
                    )
                else:
                    receipt = outcome
                    currently_authorized = receipt.get("decision") == "allow"
                    historical_replay_only = False
                if not isinstance(receipt, dict) or receipt.get("decision") != "allow":
                    return _AUTHORIZATION_DENIED
                self._cache_put(
                    self._authorization_receipts,
                    (project_id, action, request_id, request_sha256),
                    copy.deepcopy(receipt),
                )
                if currently_authorized:
                    return _AUTHORIZATION_GRANTED
                if historical_replay_only:
                    return _AUTHORIZATION_HISTORICAL_REPLAY
                return _AUTHORIZATION_DENIED
            if self._authorizer.authorize(context, action, project_id) is True:
                return _AUTHORIZATION_GRANTED
            return _AUTHORIZATION_DENIED
        except Exception:  # noqa: BLE001
            # Authorization provider failures deny writes.
            return _AUTHORIZATION_DENIED

    def authorization_receipt(
        self,
        tool: str,
        arguments: Any,
        *,
        context: RequestContext,
    ) -> dict[str, Any] | None:
        """Return exact pre-write authorization evidence for one accepted request."""
        if (
            tool not in STATE_MCP_TOOLS
            or not isinstance(context, RequestContext)
            or _validate_request(tool, arguments) is not None
        ):
            return None
        if tool == EXPERIMENT_EFFECT_TOOL:
            action = f"state.experiment.effect.{arguments['action']}"
        elif tool == EFFECT_TOOL:
            action = f"state.effect.{arguments['action']}"
        else:
            action = _AUTHORIZATION_ACTIONS[tool]
        request_sha256 = self._request_fingerprint(tool, arguments, context)
        receipt = self._authorization_receipts.get(
            (
                arguments["project_id"],
                action,
                arguments["request_id"],
                request_sha256,
            )
        )
        return copy.deepcopy(receipt) if receipt is not None else None

    def call_tool(
        self,
        tool: str,
        arguments: Any,
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Validate, authorize and execute one State MCP tool call."""
        request_id = _request_id(arguments)
        if tool not in STATE_MCP_TOOLS:
            return _response(
                tool=str(tool),
                request_id=request_id,
                error_code="unsupported",
                error_message="unsupported State MCP tool",
            )
        request_error = _validate_request(tool, arguments)
        if request_error is not None:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="invalid_request",
                error_message=request_error,
            )

        if not isinstance(context, RequestContext):
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_message="trusted request context is required",
            )

        if tool == EXPERIMENT_EFFECT_TOOL:
            action = f"state.experiment.effect.{arguments['action']}"
        elif tool == EFFECT_TOOL:
            action = f"state.effect.{arguments['action']}"
        else:
            action = _AUTHORIZATION_ACTIONS[tool]
        request_sha256 = self._request_fingerprint(tool, arguments, context)
        authorization = self._authorize(
            context=context,
            action=action,
            project_id=arguments["project_id"],
            request_id=arguments["request_id"],
            request_sha256=request_sha256,
        )
        if authorization == _AUTHORIZATION_DENIED:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_message="request is not authorized",
            )

        historical_replay_only = (
            authorization == _AUTHORIZATION_HISTORICAL_REPLAY
        )
        if historical_replay_only and tool not in {
            PROMOTION_PROPOSE_TOOL,
            PROMOTION_APPROVE_TOOL,
            IDEA_CORRECTION_PROTECT_TOOL,
            IDEA_CORRECTION_RELEASE_TOOL,
        }:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_message="historical authorization requires a committed State event",
            )

        if tool == EFFECT_GATE_TOOL:
            return self._effect_gate(arguments, context=context)
        if tool in {
            COMMIT_TOOL,
            CLAIM_TOOL,
            EFFECT_TOOL,
            EXPERIMENT_EFFECT_TOOL,
            ATTEMPT_TOOL,
            PROMOTION_PROPOSE_TOOL,
            PROMOTION_APPROVE_TOOL,
            IDEA_CAPTURE_TOOL,
            IDEA_REVIEW_TOOL,
            IDEA_CORRECTION_PROTECT_TOOL,
            IDEA_CORRECTION_RELEASE_TOOL,
            LOCAL_WORK_COMPLETION_TOOL,
            LOCAL_WORK_ACTIVATION_TOOL,
            LOCAL_WORK_TRANSITION_TOOL,
            LOCAL_CLAIM_RECOVERY_TOOL,
        }:
            with self._mutation_lock:
                replay = self._request_replay(tool, arguments, context)
                if replay is not None:
                    return replay
                if tool == COMMIT_TOOL:
                    return self._commit(arguments, context=context)
                if tool == CLAIM_TOOL:
                    return self._claim(arguments, context=context)
                if tool == LOCAL_WORK_COMPLETION_TOOL:
                    return self._complete_local_work(arguments, context=context)
                if tool == LOCAL_WORK_ACTIVATION_TOOL:
                    return self._activate_local_work(arguments, context=context)
                if tool == LOCAL_WORK_TRANSITION_TOOL:
                    return self._transition_local_work(arguments, context=context)
                if tool == LOCAL_CLAIM_RECOVERY_TOOL:
                    return self._recover_local_claim(arguments, context=context)
                if tool == ATTEMPT_TOOL:
                    return self._attempt(arguments, context=context)
                if tool == IDEA_CAPTURE_TOOL:
                    return self._idea_capture(arguments, context=context)
                if tool in {
                    IDEA_REVIEW_TOOL,
                    IDEA_CORRECTION_PROTECT_TOOL,
                    IDEA_CORRECTION_RELEASE_TOOL,
                }:
                    return self._idea_review_write(
                        tool,
                        arguments,
                        context=context,
                        replay_only=historical_replay_only,
                    )
                if tool in {PROMOTION_PROPOSE_TOOL, PROMOTION_APPROVE_TOOL}:
                    return self._promotion(
                        tool,
                        arguments,
                        context=context,
                        replay_only=historical_replay_only,
                    )
                if tool == EXPERIMENT_EFFECT_TOOL:
                    return self._effect(
                        arguments,
                        context=context,
                        tool=EXPERIMENT_EFFECT_TOOL,
                    )
                return self._effect(arguments, context=context)
        if tool != READ_TOOL:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="unsupported",
                error_message="State MCP write action is not implemented",
            )

        try:
            snapshot = invoke_state_store(
                self._store,
                "read_project",
                arguments["project_id"],
            )
            events = invoke_state_store(
                self._store,
                "read_events",
                arguments["project_id"],
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )

        event_head = None
        if events:
            if (
                events[-1]["project_id"] != snapshot["project"]["project_id"]
                or events[-1]["revision_after"] != snapshot["project"]["revision"]
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="snapshot and event head were read at different revisions",
                )
            event_head = {
                "sequence_no": events[-1]["sequence_no"],
                "event_sha256": events[-1]["event_sha256"],
            }
        return _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": snapshot,
                "revision": snapshot["project"]["revision"],
                "event_head": event_head,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )

    def _idea_capture(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Persist one candidate-only Idea without changing execution authority."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        if arguments["schema_version"] == IDEA_CAPTURE_REQUEST_SCHEMA_VERSION_V2:
            return self._idea_capture_v2(arguments, context=context)
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(
                IDEA_CAPTURE_TOOL, arguments, context
            )
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("idea_transition")
                if (
                    not isinstance(transition, dict)
                    or transition.get("request_sha256") != request_fingerprint
                    or transition.get("operation") != arguments["action"]
                    or transition.get("idea_id") != arguments["idea_id"]
                ):
                    return _response(
                        tool=IDEA_CAPTURE_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="Idea capture request identity conflicts with durable state",
                    )
                response = _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    result={
                        "revision": existing_event["revision_after"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                    },
                )
                self._remember_request(IDEA_CAPTURE_TOOL, arguments, context, response)
                return response
            if snapshot.get("schema_version") != "context.typed-state/v3alpha1":
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Idea capture requires typed-state v3alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            if any(item["idea_id"] == arguments["idea_id"] for item in snapshot["ideas"]):
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="Idea identity already exists",
                )
            observed_at = self._clock()
            gate = evaluate_idea_capture_gate(
                snapshot,
                actor_ref=context.subject_ref,
                expected_revision=arguments["expected_revision"],
                parent_work_id=arguments["parent_work_id"],
                return_work_id=arguments["return_work_id"],
                action=arguments["action"],
                switch_target_work_id=arguments["switch_target_work_id"],
                expiry=arguments["expiry"],
                observed_at=observed_at,
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code=(
                        "conflict" if gate["reason"] == "stale_revision" else "integrity"
                    ),
                    error_message="Idea capture gate denied read-only: " + gate["reason"],
                )
            idea = {
                "idea_id": arguments["idea_id"],
                "parent_work_id": arguments["parent_work_id"],
                "source_ref": arguments["source_ref"],
                "summary": arguments["summary"],
                "status": {
                    "capture-and-continue": "candidate",
                    "park": "parked",
                    "propose-switch": "proposed",
                }[arguments["action"]],
                "return_work_id": arguments["return_work_id"],
                "expiry": arguments["expiry"],
                "attempt_budget": None,
                "promotion_target": arguments["switch_target_work_id"],
                "evidence_ids": [],
            }
            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {
                    "collection": "ideas",
                    "object_id": idea["idea_id"],
                    "value": idea,
                },
            )
            revision_after = arguments["expected_revision"] + 1
            changes = [
                {
                    "collection": "ideas",
                    "object_id": idea["idea_id"],
                    "value": idea,
                }
            ]
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                idea_transition={
                    "operation": arguments["action"],
                    "request_sha256": request_fingerprint,
                    "idea_id": idea["idea_id"],
                    "parent_work_id": idea["parent_work_id"],
                    "return_work_id": idea["return_work_id"],
                    "switch_target_work_id": idea["promotion_target"],
                },
                schema_version=IDEA_EVENT_SCHEMA_VERSION,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=IDEA_CAPTURE_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=IDEA_CAPTURE_TOOL,
            request_id=request_id,
            result={
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
            },
        )
        self._remember_request(IDEA_CAPTURE_TOOL, arguments, context, response)
        return response

    def _idea_capture_v2(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
        _retry_on_conflict: bool = True,
    ) -> dict[str, Any]:
        """Capture one v4 Idea observation with deterministic canonical dedupe."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(
                IDEA_CAPTURE_TOOL, arguments, context
            )
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("idea_transition")
                if (
                    not isinstance(transition, dict)
                    or transition.get("request_sha256") != request_fingerprint
                    or transition.get("submitted_idea_id") != arguments["idea_id"]
                    or transition.get("occurrence_id") != arguments["occurrence_id"]
                ):
                    return _response(
                        tool=IDEA_CAPTURE_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="Idea v2 capture request identity conflicts with durable state",
                    )
                response = _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    result={
                        "revision": existing_event["revision_after"],
                        "canonical_idea_id": transition["canonical_idea_id"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                    },
                )
                self._remember_request(IDEA_CAPTURE_TOOL, arguments, context, response)
                return response
            if snapshot.get("schema_version") not in {
                "context.typed-state/v4alpha1",
                DURABLE_EFFECT_SCHEMA_VERSION,
            }:
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Idea v2 capture requires typed-state v4alpha1",
                )
            dedupe_key = compute_idea_dedupe_key(
                parent_work_id=arguments["parent_work_id"],
                scope_ref=arguments["scope_ref"],
                summary=arguments["summary"],
            )
            existing = next(
                (item for item in snapshot["ideas"] if item["dedupe_key"] == dedupe_key),
                None,
            )
            commit_revision = snapshot["project"]["revision"]
            if (
                commit_revision != arguments["expected_revision"]
                and existing is None
            ):
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            gate = evaluate_idea_capture_gate(
                snapshot,
                actor_ref=context.subject_ref,
                expected_revision=commit_revision,
                parent_work_id=arguments["parent_work_id"],
                return_work_id=arguments["return_work_id"],
                action=arguments["action"],
                switch_target_work_id=arguments["switch_target_work_id"],
                expiry=arguments["expiry"],
                observed_at=observed_at,
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=IDEA_CAPTURE_TOOL,
                    request_id=request_id,
                    error_code="conflict" if gate["reason"] == "stale_revision" else "integrity",
                    error_message="Idea v2 capture gate denied read-only: " + gate["reason"],
                )
            candidate = upsert_idea_observation(
                snapshot,
                idea_id=arguments["idea_id"],
                parent_work_id=arguments["parent_work_id"],
                return_work_id=arguments["return_work_id"],
                source_ref=arguments["source_ref"],
                summary=arguments["summary"],
                scope_ref=arguments["scope_ref"],
                urgency=arguments["urgency"],
                review_at=arguments["review_at"],
                occurrence_id=arguments["occurrence_id"],
                observed_at=observed_at,
                actor_ref=context.subject_ref,
                request_sha256=request_fingerprint,
            )
            canonical_idea_id = existing["idea_id"] if existing is not None else arguments["idea_id"]
            if existing is None:
                canonical = next(
                    item for item in candidate["ideas"] if item["idea_id"] == canonical_idea_id
                )
                canonical["status"] = {
                    "capture-and-continue": "candidate",
                    "park": "parked",
                    "propose-switch": "proposed",
                }[arguments["action"]]
                canonical["expiry"] = arguments["expiry"]
                canonical["promotion_target"] = arguments["switch_target_work_id"]
            revision_after = commit_revision + 1
            occurrence = next(
                item
                for item in candidate["idea_occurrences"]
                if item["occurrence_id"] == arguments["occurrence_id"]
            )
            changes: list[dict[str, Any]] = []
            if existing is None:
                canonical = next(
                    item for item in candidate["ideas"] if item["idea_id"] == canonical_idea_id
                )
                changes.append(
                    {"collection": "ideas", "object_id": canonical_idea_id, "value": canonical}
                )
            changes.append(
                {
                    "collection": "idea_occurrences",
                    "object_id": occurrence["occurrence_id"],
                    "value": occurrence,
                }
            )
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=commit_revision,
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                idea_transition={
                    "operation": "capture-merged" if existing is not None else "capture-created",
                    "request_sha256": request_fingerprint,
                    "canonical_idea_id": canonical_idea_id,
                    "submitted_idea_id": arguments["idea_id"],
                    "occurrence_id": occurrence["occurrence_id"],
                    "review_id": None,
                    "protection_id": None,
                },
                schema_version=IDEA_EVENT_SCHEMA_VERSION_V2,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=commit_revision,
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except StateStoreConflict as exc:
            if _retry_on_conflict:
                return self._idea_capture_v2(
                    arguments,
                    context=context,
                    _retry_on_conflict=False,
                )
            return _backend_error_response(
                tool=IDEA_CAPTURE_TOOL,
                request_id=request_id,
                error=exc,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(
                tool=IDEA_CAPTURE_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=IDEA_CAPTURE_TOOL,
            request_id=request_id,
            result={
                "revision": expected_snapshot["project"]["revision"],
                "canonical_idea_id": canonical_idea_id,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
            },
        )
        self._remember_request(IDEA_CAPTURE_TOOL, arguments, context, response)
        return response

    def _idea_review_write(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
        replay_only: bool = False,
    ) -> dict[str, Any]:
        """Persist one review or correction-protection Idea v2 transition."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(tool, arguments, context)
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("idea_transition")
                expected_operation = {
                    IDEA_REVIEW_TOOL: "review-updated",
                    IDEA_CORRECTION_PROTECT_TOOL: "correction-guarded",
                    IDEA_CORRECTION_RELEASE_TOOL: "correction-released",
                }[tool]
                if (
                    not isinstance(transition, dict)
                    or transition.get("request_sha256") != request_fingerprint
                    or transition.get("operation") != expected_operation
                    or transition.get("canonical_idea_id") != arguments["idea_id"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="Idea review request identity conflicts with durable state",
                    )
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    result={
                        "revision": existing_event["revision_after"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                    },
                )
                self._remember_request(tool, arguments, context, response)
                return response
            if replay_only:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="permission_denied",
                    error_message=(
                        "historical authorization requires a committed State event"
                    ),
                )
            if snapshot.get("schema_version") not in {
                "context.typed-state/v4alpha1",
                DURABLE_EFFECT_SCHEMA_VERSION,
            }:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Idea review writes require typed-state v4alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            occurred_at = self._clock()
            if tool == IDEA_REVIEW_TOOL:
                candidate = apply_idea_review(
                    snapshot,
                    idea_id=arguments["idea_id"],
                    review_id=arguments["review_id"],
                    reviewer_ref=context.subject_ref,
                    decision=arguments["decision"],
                    urgency=arguments["urgency"],
                    impact=arguments["impact"],
                    review_at=arguments["review_at"],
                    evidence_ids=arguments["evidence_ids"],
                    reviewed_at=occurred_at,
                )
                idea = next(
                    item for item in candidate["ideas"] if item["idea_id"] == arguments["idea_id"]
                )
                review = next(
                    item
                    for item in candidate["idea_reviews"]
                    if item["review_id"] == arguments["review_id"]
                )
                changes = [
                    {"collection": "ideas", "object_id": idea["idea_id"], "value": idea},
                    {
                        "collection": "idea_reviews",
                        "object_id": review["review_id"],
                        "value": review,
                    },
                ]
                operation = "review-updated"
                review_id = review["review_id"]
                protection_id = None
            elif tool == IDEA_CORRECTION_PROTECT_TOOL:
                candidate = open_correction_protection(
                    snapshot,
                    protection_id=arguments["protection_id"],
                    idea_id=arguments["idea_id"],
                    affected_work_ids=arguments["affected_work_ids"],
                    affected_scope_refs=arguments["affected_scope_refs"],
                    reason=arguments["reason"],
                    evidence_ids=arguments["evidence_ids"],
                    opened_at=occurred_at,
                    opened_by_ref=context.subject_ref,
                )
                protection = next(
                    item
                    for item in candidate["correction_protections"]
                    if item["protection_id"] == arguments["protection_id"]
                )
                changes = [
                    {
                        "collection": "correction_protections",
                        "object_id": protection["protection_id"],
                        "value": protection,
                    }
                ]
                operation = "correction-guarded"
                review_id = None
                protection_id = protection["protection_id"]
            else:
                candidate = release_correction_protection(
                    snapshot,
                    protection_id=arguments["protection_id"],
                    idea_id=arguments["idea_id"],
                    released_by_ref=context.subject_ref,
                    release_reason=arguments["release_reason"],
                    release_evidence_ids=arguments["release_evidence_ids"],
                    released_at=occurred_at,
                )
                protection = next(
                    item
                    for item in candidate["correction_protections"]
                    if item["protection_id"] == arguments["protection_id"]
                )
                changes = [
                    {
                        "collection": "correction_protections",
                        "object_id": protection["protection_id"],
                        "value": protection,
                    }
                ]
                operation = "correction-released"
                review_id = None
                protection_id = protection["protection_id"]
            revision_after = arguments["expected_revision"] + 1
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=occurred_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=occurred_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                idea_transition={
                    "operation": operation,
                    "request_sha256": request_fingerprint,
                    "canonical_idea_id": arguments["idea_id"],
                    "submitted_idea_id": None,
                    "occurrence_id": None,
                    "review_id": review_id,
                    "protection_id": protection_id,
                },
                schema_version=IDEA_EVENT_SCHEMA_VERSION_V2,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(tool=tool, request_id=request_id, error=exc)
        response = _response(
            tool=tool,
            request_id=request_id,
            result={
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
            },
        )
        self._remember_request(tool, arguments, context, response)
        return response

    def _attempt(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Atomically record one authorized Experiment attempt in the v3 ledger."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(
                ATTEMPT_TOOL, arguments, context
            )
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("experiment_transition")
                if (
                    not isinstance(transition, dict)
                    or transition.get("operation") != "attempt-started"
                    or transition.get("request_sha256") != request_fingerprint
                    or transition.get("attempt_id") != arguments["attempt_id"]
                    or events[-1]["event_id"] != event_id
                    or snapshot["project"]["revision"]
                    != existing_event["revision_after"]
                ):
                    return _response(
                        tool=ATTEMPT_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="attempt request identity conflicts with durable state",
                    )
                response = _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": snapshot["project"]["revision"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
                self._remember_request(ATTEMPT_TOOL, arguments, context, response)
                return response
            if snapshot.get("schema_version") != "context.typed-state/v3alpha1":
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Experiment attempts require typed-state v3alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            gate = evaluate_attempt_gate(
                snapshot,
                actor_ref=context.subject_ref,
                work_id=arguments["work_id"],
                claim_id=arguments["claim_id"],
                expected_revision=arguments["expected_revision"],
                observed_at=observed_at,
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"] in {"stale_revision", "claim_mismatch"}
                        else "integrity"
                    ),
                    error_message=(
                        "experiment attempt gate denied read-only: "
                        + gate["reason"]
                    ),
                )
            if any(
                item["attempt_id"] == arguments["attempt_id"]
                for item in snapshot["experiment_attempts"]
            ):
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="attempt identity already exists",
                )
            work = next(
                item
                for item in snapshot["works"]
                if item["work_id"] == arguments["work_id"]
            )
            attempt = {
                "attempt_id": arguments["attempt_id"],
                "work_id": work["work_id"],
                "claim_id": arguments["claim_id"],
                "actor_ref": context.subject_ref,
                "attempt_no": gate["attempt_no"],
                "experiment_contract_sha256": experiment_contract_sha256(work),
                "started_at": observed_at,
            }
            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
            )
            revision_after = arguments["expected_revision"] + 1
            changes = [
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                }
            ]
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                experiment_transition={
                    "operation": "attempt-started",
                    "request_sha256": request_fingerprint,
                    "attempt_id": attempt["attempt_id"],
                    "promotion_id": None,
                    "proposal_id": None,
                },
                schema_version=EVENT_SCHEMA_VERSION_V4,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=ATTEMPT_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=ATTEMPT_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(ATTEMPT_TOOL, arguments, context, response)
        return response

    def _promotion(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
        replay_only: bool = False,
    ) -> dict[str, Any]:
        """Propose or independently approve immutable Experiment promotion records."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(tool, arguments, context)
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("experiment_transition")
                expected_operation = (
                    "promotion-proposed"
                    if tool == PROMOTION_PROPOSE_TOOL
                    else "promotion-approved"
                )
                if (
                    not isinstance(transition, dict)
                    or transition.get("operation") != expected_operation
                    or transition.get("request_sha256") != request_fingerprint
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion request identity conflicts with durable state",
                    )
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": existing_event["revision_after"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
                self._remember_request(tool, arguments, context, response)
                return response
            if replay_only:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="permission_denied",
                    error_message=(
                        "historical authorization requires a committed State event"
                    ),
                )
            if snapshot.get("schema_version") != "context.typed-state/v3alpha1":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Experiment promotion requires typed-state v3alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work is None or work["kind"] != "experiment":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="promotion requires an Experiment Work",
                )
            target = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == work["promotion_target_work_id"]
                ),
                None,
            )
            if (
                work["revision"] != arguments["expected_work_revision"]
                or target is None
                or target["revision"] != arguments["expected_target_work_revision"]
                or not target["mainline_authority"]
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="Experiment promotion source or target revision is stale",
                )
            lifecycle_gate = evaluate_experiment_activation_gate(
                snapshot,
                work_id=work["work_id"],
                observed_at=observed_at,
            )
            if lifecycle_gate["reason"] in {
                "trusted_time_invalid",
                "trusted_time_regressed",
                "experiment_expired",
            }:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message=(
                        "experiment promotion gate denied read-only: "
                        + lifecycle_gate["reason"]
                    ),
                )
            contract_digest = experiment_contract_sha256(work)
            if tool == PROMOTION_PROPOSE_TOOL:
                attempt = next(
                    (
                        item
                        for item in snapshot["experiment_attempts"]
                        if item["attempt_id"] == arguments["attempt_id"]
                    ),
                    None,
                )
                if (
                    attempt is None
                    or attempt["work_id"] != work["work_id"]
                    or attempt["actor_ref"] != context.subject_ref
                    or attempt["experiment_contract_sha256"] != contract_digest
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion proposal requires a bound Experiment attempt",
                    )
                if any(
                    item["proposal_id"] == arguments["proposal_id"]
                    for item in snapshot["experiment_promotions"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal identity already exists",
                    )
                criteria = arguments["criterion_evidence"]
                if set(criteria) != set(work["exit_criteria"]):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion criteria do not match the Experiment contract",
                    )
                evidence_by_id = {
                    item["evidence_id"]: item for item in snapshot["evidence"]
                }
                if any(
                    evidence_id not in evidence_by_id
                    for evidence_ids in criteria.values()
                    for evidence_id in evidence_ids
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion criterion evidence is unknown",
                    )
                promotion = {
                    "promotion_id": arguments["proposal_id"],
                    "kind": "proposed",
                    "proposal_id": arguments["proposal_id"],
                    "work_id": work["work_id"],
                    "target_work_id": target["work_id"],
                    "actor_ref": context.subject_ref,
                    "source_work_revision": work["revision"],
                    "target_work_revision": target["revision"],
                    "attempt_id": attempt["attempt_id"],
                    "experiment_contract_sha256": contract_digest,
                    "criterion_evidence": copy.deepcopy(criteria),
                    "created_at": observed_at,
                }
                operation = "promotion-proposed"
                promotion_id = promotion["promotion_id"]
                proposal_id = promotion["proposal_id"]
            else:
                proposal = next(
                    (
                        item
                        for item in snapshot["experiment_promotions"]
                        if item["kind"] == "proposed"
                        and item["proposal_id"] == arguments["proposal_id"]
                    ),
                    None,
                )
                if proposal is None or proposal["work_id"] != work["work_id"]:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires its prior proposal",
                    )
                if (
                    proposal["source_work_revision"]
                    != arguments["expected_work_revision"]
                    or proposal["target_work_revision"]
                    != arguments["expected_target_work_revision"]
                    or proposal["source_work_revision"] != work["revision"]
                    or proposal["target_work_revision"] != target["revision"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal revisions are stale",
                    )
                if proposal["actor_ref"] == context.subject_ref:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="independent_verifier_required",
                    )
                if any(
                    item["kind"] == "approved"
                    and item["proposal_id"] == proposal["proposal_id"]
                    for item in snapshot["experiment_promotions"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal is already approved",
                    )
                evidence_by_id = {
                    item["evidence_id"]: item for item in snapshot["evidence"]
                }
                approval_time = datetime.fromisoformat(
                    observed_at.replace("Z", "+00:00")
                )
                if any(
                    evidence_by_id[evidence_id]["validity"] != "verified"
                    or evidence_by_id[evidence_id]["verified_at"] is None
                    or datetime.fromisoformat(
                        evidence_by_id[evidence_id]["observed_at"].replace(
                            "Z", "+00:00"
                        )
                    )
                    > approval_time
                    or datetime.fromisoformat(
                        evidence_by_id[evidence_id]["verified_at"].replace(
                            "Z", "+00:00"
                        )
                    )
                    > approval_time
                    for evidence_ids in proposal["criterion_evidence"].values()
                    for evidence_id in evidence_ids
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires verified criterion evidence",
                    )
                if any(
                    effect["work_id"] == work["work_id"]
                    and effect["status"] in {"authorized", "started"}
                    for effect in snapshot["effects"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires no pending Experiment effect",
                    )
                promotion = copy.deepcopy(proposal)
                promotion.update(
                    {
                        "promotion_id": arguments["approval_id"],
                        "kind": "approved",
                        "actor_ref": context.subject_ref,
                        "created_at": observed_at,
                    }
                )
                operation = "promotion-approved"
                promotion_id = promotion["promotion_id"]
                proposal_id = proposal["proposal_id"]

            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {
                    "collection": "experiment_promotions",
                    "object_id": promotion_id,
                    "value": promotion,
                },
            )
            revision_after = arguments["expected_revision"] + 1
            changes = [
                {
                    "collection": "experiment_promotions",
                    "object_id": promotion_id,
                    "value": promotion,
                }
            ]
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                experiment_transition={
                    "operation": operation,
                    "request_sha256": request_fingerprint,
                    "attempt_id": None,
                    "promotion_id": promotion_id,
                    "proposal_id": proposal_id,
                },
                schema_version=EVENT_SCHEMA_VERSION_V4,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(tool, arguments, context, response)
        return response

    def _effect_gate(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Return a typed read-only effect verdict at the current revision."""
        request_id = arguments["request_id"]
        try:
            snapshot = invoke_state_store(
                self._store, "read_project", arguments["project_id"]
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
        ) as exc:
            return _backend_error_response(
                tool=EFFECT_GATE_TOOL,
                request_id=request_id,
                error=exc,
            )
        if snapshot.get("schema_version") in {
            "context.typed-state/v4alpha1",
            DURABLE_EFFECT_SCHEMA_VERSION,
        }:
            correction_gate = evaluate_correction_write_gate(
                snapshot,
                [
                    {
                        "collection": "effects",
                        "object_id": arguments["effect_id"],
                        "value": {
                            "work_id": arguments["work_id"],
                            "scope_ref": arguments["scope_ref"],
                        },
                    }
                ],
            )
            if correction_gate["decision"] != "allow":
                verdict = {
                    "schema_version": "context.effect-scope-verdict/v1alpha1",
                    "decision": "deny",
                    "read_only": True,
                    "reason": "correction_protection",
                }
                return _response(
                    tool=EFFECT_GATE_TOOL,
                    request_id=request_id,
                    result={
                        "verdict": verdict,
                        "revision": snapshot["project"]["revision"],
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
        work = next(
            (
                item
                for item in snapshot["works"]
                if item["work_id"] == arguments["work_id"]
            ),
            None,
        )
        if work is not None and work["kind"] == "experiment":
            verdict = {
                "schema_version": "context.effect-scope-verdict/v1alpha1",
                "decision": "deny",
                "read_only": True,
                "reason": "experiment_attempt_provenance_required",
            }
            return _response(
                tool=EFFECT_GATE_TOOL,
                request_id=request_id,
                result={
                    "verdict": verdict,
                    "revision": snapshot["project"]["revision"],
                    "registry_digest": self._registry_hash_value,
                    "capabilities": capability_manifest_to_document(self._manifest),
                },
            )
        verdict = evaluate_effect_scope_gate(
            snapshot,
            actor_ref=context.subject_ref,
            work_id=arguments["work_id"],
            claim_id=arguments["claim_id"],
            expected_revision=arguments["expected_revision"],
            operation=arguments["operation"],
            requested_scope=arguments["scope_ref"],
            effect_id=arguments["effect_id"],
            observed_at=self._clock(),
        )
        return _response(
            tool=EFFECT_GATE_TOOL,
            request_id=request_id,
            result={
                "verdict": verdict,
                "revision": snapshot["project"]["revision"],
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )

    def _effect(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
        tool: str = EFFECT_TOOL,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if (
                tool == EFFECT_TOOL
                and arguments["schema_version"] == EFFECT_REQUEST_SCHEMA_VERSION_V2
                and snapshot.get("schema_version")
                != DURABLE_EFFECT_SCHEMA_VERSION
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="unsupported",
                    error_message="effect request v2 requires typed state v5 migration",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            if (
                arguments["action"] == "authorize"
                and snapshot.get("schema_version")
                in {"context.typed-state/v4alpha1", DURABLE_EFFECT_SCHEMA_VERSION}
            ):
                correction_gate = evaluate_correction_write_gate(
                    snapshot,
                    [
                        {
                            "collection": "effects",
                            "object_id": arguments["effect_id"],
                            "value": {
                                "work_id": arguments["work_id"],
                                "scope_ref": arguments["scope_ref"],
                            },
                        }
                    ],
                )
                if correction_gate["decision"] != "allow":
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message=(
                            "effect authorization denied by active correction protection"
                        ),
                    )
            if snapshot["schema_version"] in {
                "context.typed-state/v2alpha1",
                "context.typed-state/v3alpha1",
            }:
                try:
                    validate_scope(arguments["scope_ref"])
                except ValueError:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="invalid_request",
                        error_message="scope_ref is not canonical",
                    )
            observed_at = self._clock()
            work_for_effect = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work_for_effect is not None and work_for_effect["kind"] == "experiment":
                if tool != EXPERIMENT_EFFECT_TOOL:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="Experiment effect requires context.experiment.effect",
                    )
                if arguments["action"] == "authorize":
                    # An existing, immutable attempt retains authority for its
                    # bounded effects. Budget exhaustion blocks a new attempt,
                    # not work that already consumed the final permitted slot.
                    lifecycle_gate = experiment_time_verdict(
                        snapshot,
                        work_id=work_for_effect["work_id"],
                        observed_at=observed_at,
                    )
                    if lifecycle_gate["decision"] != "allow":
                        return _response(
                            tool=tool,
                            request_id=request_id,
                            error_code="integrity",
                            error_message=(
                                "Experiment effect gate denied read-only: "
                                + lifecycle_gate["reason"]
                            ),
                        )
                attempt = next(
                    (
                        item
                        for item in snapshot["experiment_attempts"]
                        if item["attempt_id"] == arguments["attempt_id"]
                    ),
                    None,
                )
                if (
                    attempt is None
                    or attempt["work_id"] != work_for_effect["work_id"]
                    or attempt["claim_id"] != arguments["claim_id"]
                    or attempt["actor_ref"] != context.subject_ref
                    or attempt["experiment_contract_sha256"]
                    != experiment_contract_sha256(work_for_effect)
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="Experiment effect attempt provenance is invalid",
                    )
            elif tool == EXPERIMENT_EFFECT_TOOL:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="context.experiment.effect requires an Experiment Work",
                )
            if arguments["action"] == "authorize":
                gate = evaluate_effect_scope_gate(
                    snapshot,
                    actor_ref=context.subject_ref,
                    work_id=arguments["work_id"],
                    claim_id=arguments["claim_id"],
                    expected_revision=arguments["expected_revision"],
                    operation=arguments["operation"],
                    requested_scope=arguments["scope_ref"],
                    effect_id=arguments["effect_id"],
                    observed_at=observed_at,
                )
            else:
                gate = evaluate_effect_completion_gate(
                    snapshot,
                    actor_ref=context.subject_ref,
                    work_id=arguments["work_id"],
                    claim_id=arguments["claim_id"],
                    expected_revision=arguments["expected_revision"],
                    effect_id=arguments["effect_id"],
                    effect_key=arguments["effect_key"],
                    operation=arguments["operation"],
                    requested_scope=arguments["scope_ref"],
                )
            if gate["decision"] != "allow":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"]
                        in {
                            "stale_revision",
                            "claim_revision_mismatch",
                            "claim_expired",
                            "effect_scope_conflict",
                        }
                        else "integrity"
                    ),
                    error_message=f"effect scope gate denied read-only: {gate['reason']}",
                )
            claim = next(
                (item for item in snapshot["claims"] if item["claim_id"] == arguments["claim_id"]),
                None,
            )
            work = next(
                (item for item in snapshot["works"] if item["work_id"] == arguments["work_id"]),
                None,
            )
            assert claim is not None and work is not None

            existing = next(
                (item for item in snapshot["effects"] if item["effect_id"] == arguments["effect_id"]),
                None,
            )
            if arguments["action"] == "authorize":
                if existing is not None:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="effect identity already exists",
                    )
                if any(
                    item["effect_key"] == arguments["effect_key"]
                    for item in snapshot["effects"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="effect key already exists",
                    )
                effect = {
                    "effect_id": arguments["effect_id"],
                    "effect_key": arguments["effect_key"],
                    "work_id": work["work_id"],
                    "claim_id": claim["claim_id"],
                    "status": "authorized",
                    "operation": arguments["operation"],
                    "scope_ref": copy.deepcopy(arguments["scope_ref"]),
                    "expected_project_revision": arguments["expected_revision"] + 1,
                    "sequence_no": max(
                        (item["sequence_no"] for item in snapshot["effects"]),
                        default=0,
                    )
                    + 1,
                    "evidence_ids": copy.deepcopy(arguments["evidence_ids"]),
                    "result_ref": None,
                    "requested_at": observed_at,
                    "completed_at": None,
                }
                if snapshot["schema_version"] == DURABLE_EFFECT_SCHEMA_VERSION:
                    effect["request_sha256"] = arguments.get("request_sha256")
                if snapshot["schema_version"] in {
                    "context.typed-state/v3alpha1",
                    "context.typed-state/v4alpha1",
                    DURABLE_EFFECT_SCHEMA_VERSION,
                }:
                    effect["attempt_id"] = (
                        arguments["attempt_id"]
                        if tool == EXPERIMENT_EFFECT_TOOL
                        else None
                    )
            else:
                if existing is None:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="not_found",
                        error_message="effect was not found",
                    )
                if (
                    existing["effect_key"] != arguments["effect_key"]
                    or existing["work_id"] != work["work_id"]
                    or existing["claim_id"] != claim["claim_id"]
                    or existing["operation"] != arguments["operation"]
                    or existing["scope_ref"] != arguments["scope_ref"]
                    or existing.get("request_sha256")
                    != arguments.get("request_sha256")
                    or existing["status"] not in {"authorized", "started"}
                    or (
                        snapshot["schema_version"]
                        in {
                            "context.typed-state/v3alpha1",
                            "context.typed-state/v4alpha1",
                            DURABLE_EFFECT_SCHEMA_VERSION,
                        }
                        and existing.get("attempt_id")
                        != (
                            arguments["attempt_id"]
                            if tool == EXPERIMENT_EFFECT_TOOL
                            else None
                        )
                    )
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="effect provenance does not match the active claim",
                    )
                effect = copy.deepcopy(existing)
                effect.update(
                    {
                        "status": "succeeded",
                        "evidence_ids": copy.deepcopy(arguments["evidence_ids"]),
                        "result_ref": arguments["result_ref"],
                        "completed_at": observed_at,
                    }
                )

            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {"collection": "effects", "object_id": effect["effect_id"], "value": effect},
            )
            changes = [
                {"collection": "effects", "object_id": effect["effect_id"], "value": effect}
            ]
            if arguments["action"] == "complete":
                completed_claim = next(
                    item
                    for item in candidate["claims"]
                    if item["claim_id"] == claim["claim_id"]
                )
                lease_expires_at = datetime.fromisoformat(
                    completed_claim["lease_expires_at"].replace("Z", "+00:00")
                )
                completed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if completed_at >= lease_expires_at:
                    completed_claim.update(
                        {"status": "expired", "released_at": observed_at}
                    )
                    _upsert_change(
                        changes, collection="claims", value=completed_claim
                    )
                    recovery_work = next(
                        item
                        for item in candidate["works"]
                        if item["work_id"] == completed_claim["work_id"]
                    )
                    recovery_work.update(
                        {
                            "status": "verifying",
                            "revision": recovery_work["revision"] + 1,
                        }
                    )
                    _upsert_change(changes, collection="works", value=recovery_work)
            revision_after = arguments["expected_revision"] + 1
            for existing_claim in candidate["claims"]:
                if existing_claim["status"] == "active":
                    existing_claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=existing_claim)
            for existing_effect in candidate["effects"]:
                if existing_effect["status"] in {"authorized", "started"}:
                    existing_effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=existing_effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=candidate["project"]["updated_at"],
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(tool, arguments, context, response)
        return response

    def _claim(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work is not None and work["kind"] == "experiment":
                lifecycle_gate = evaluate_experiment_activation_gate(
                    snapshot,
                    work_id=work["work_id"],
                    observed_at=observed_at,
                )
                if lifecycle_gate["decision"] != "allow":
                    return _response(
                        tool=CLAIM_TOOL,
                        request_id=request_id,
                        error_code="integrity",
                        error_message=(
                            "experiment activation gate denied read-only: "
                            + lifecycle_gate["reason"]
                        ),
                    )
            gate = evaluate_claim_scope_gate(
                snapshot,
                actor_ref=context.subject_ref,
                work_id=arguments["work_id"],
                expected_revision=arguments["expected_revision"],
                requested_scopes=arguments["scope_owners"],
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"] in {"stale_revision", "scope_conflict"}
                        else "integrity"
                    ),
                    error_message=f"claim scope gate denied read-only: {gate['reason']}",
                )
            assert work is not None
            if any(
                claim["claim_id"] == arguments["claim_id"]
                for claim in snapshot["claims"]
            ):
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="claim identity already exists",
                )
            if any(
                claim["work_id"] == work["work_id"] and claim["status"] == "active"
                for claim in snapshot["claims"]
            ):
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="Work already has an active claim",
                )

            claimed_at = observed_at
            work = copy.deepcopy(work)
            work["status"] = "active"
            work["revision"] += 1
            claim = {
                "claim_id": arguments["claim_id"],
                "work_id": work["work_id"],
                "actor_ref": context.subject_ref,
                "status": "active",
                "expected_project_revision": arguments["expected_revision"] + 1,
                "claimed_at": claimed_at,
                "lease_expires_at": arguments["lease_expires_at"],
                "released_at": None,
                "scope_owners": copy.deepcopy(arguments["scope_owners"]),
            }
            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {"collection": "works", "object_id": work["work_id"], "value": work},
            )
            _replace_or_append(
                candidate,
                {"collection": "claims", "object_id": claim["claim_id"], "value": claim},
            )
            changes = [
                {"collection": "works", "object_id": work["work_id"], "value": work},
                {"collection": "claims", "object_id": claim["claim_id"], "value": claim},
            ]
            for existing_claim in candidate["claims"]:
                if existing_claim["status"] == "active":
                    existing_claim["expected_project_revision"] = arguments["expected_revision"] + 1
                    _upsert_change(changes, collection="claims", value=existing_claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = arguments["expected_revision"] + 1
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=arguments["expected_revision"] + 1,
                updated_at=claimed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=claimed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=CLAIM_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=CLAIM_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(CLAIM_TOOL, arguments, context, response)
        return response

    def _commit(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if (
                snapshot.get("schema_version")
                in {
                    "context.typed-state/v3alpha1",
                    "context.typed-state/v4alpha1",
                    DURABLE_EFFECT_SCHEMA_VERSION,
                }
                and any(change["collection"] == "ideas" for change in arguments["changes"])
            ):
                return _response(
                    tool=COMMIT_TOOL,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="typed-state Ideas require a dedicated Idea State MCP tool",
                )
            if snapshot.get("schema_version") in {
                "context.typed-state/v4alpha1",
                DURABLE_EFFECT_SCHEMA_VERSION,
            }:
                correction_gate = evaluate_correction_write_gate(
                    snapshot, arguments["changes"]
                )
                if correction_gate["decision"] != "allow":
                    return _response(
                        tool=COMMIT_TOOL,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="state commit denied by active correction protection",
                    )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=COMMIT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )

            changes = copy.deepcopy(arguments["changes"])
            candidate = copy.deepcopy(snapshot)
            for change in changes:
                _replace_or_append(candidate, change)

            revision_after = arguments["expected_revision"] + 1
            occurred_at = self._clock()
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=occurred_at,
            )

            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type=(
                    "correction"
                    if arguments["supersedes_event_id"] is not None
                    else "state-transition"
                ),
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=occurred_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=arguments["supersedes_event_id"],
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=COMMIT_TOOL,
                request_id=request_id,
                error=exc,
            )
        except (KeyError, TypeError):
            return _response(
                tool=COMMIT_TOOL,
                request_id=request_id,
                error_code="integrity",
                error_message="state intent contains an invalid nested value",
            )

        response = _response(
            tool=COMMIT_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(COMMIT_TOOL, arguments, context, response)
        return response

    def _complete_local_work(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            claim = next(
                (
                    item
                    for item in snapshot["claims"]
                    if item["claim_id"] == arguments["claim_id"]
                ),
                None,
            )
            requested_evidence = copy.deepcopy(arguments["evidence"])
            requested_ids = [item["evidence_id"] for item in requested_evidence]
            evidence_by_id = {
                item["evidence_id"]: item for item in snapshot["evidence"]
            }
            if (
                work is not None
                and claim is not None
                and work["status"] == "completed"
                and claim["status"] == "released"
                and claim["work_id"] == work["work_id"]
                and claim["actor_ref"] == context.subject_ref
                and set(requested_ids).issubset(work["evidence_ids"])
                and all(
                    item["evidence_id"] in evidence_by_id
                    and evidence_by_id[item["evidence_id"]]["content_sha256"]
                    == item["content_sha256"]
                    for item in requested_evidence
                )
            ):
                event_head = (
                    None
                    if not events
                    else {
                        "sequence_no": events[-1]["sequence_no"],
                        "event_sha256": events[-1]["event_sha256"],
                    }
                )
                response = _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": snapshot["project"]["revision"],
                        "event_head": event_head,
                        "event": None,
                        "already_completed": True,
                        "evidence_ids": requested_ids,
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(
                            self._manifest
                        ),
                    },
                )
                self._remember_request(
                    LOCAL_WORK_COMPLETION_TOOL, arguments, context, response
                )
                return response
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            if work is None or claim is None:
                return _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    error_code="not_found",
                    error_message="active Work or claim was not found",
                )
            if snapshot["schema_version"] == "context.typed-state/v6alpha1":
                return _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    error_code="capability",
                    error_message="shared Work requires the fenced completion adapter",
                )
            observed_at = self._clock()
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            lease_expires = datetime.fromisoformat(
                claim["lease_expires_at"].replace("Z", "+00:00")
            )
            if (
                work["status"] != "active"
                or claim["status"] != "active"
                or claim["work_id"] != work["work_id"]
                or claim["actor_ref"] != context.subject_ref
                or claim["expected_project_revision"] != arguments["expected_revision"]
                or observed >= lease_expires
            ):
                return _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="completion requires the current active Work and claim",
                )
            if any(item["evidence_id"] in evidence_by_id for item in requested_evidence):
                return _response(
                    tool=LOCAL_WORK_COMPLETION_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="completion evidence identity is already in use",
                )

            candidate = copy.deepcopy(snapshot)
            changes: list[dict[str, Any]] = []
            for item in requested_evidence:
                change = {
                    "collection": "evidence",
                    "object_id": item["evidence_id"],
                    "value": item,
                }
                _replace_or_append(candidate, change)
                changes.append(change)
            completed_work = next(
                item
                for item in candidate["works"]
                if item["work_id"] == work["work_id"]
            )
            completed_work["status"] = "completed"
            completed_work["revision"] += 1
            completed_work["evidence_ids"] = list(
                dict.fromkeys([*completed_work["evidence_ids"], *requested_ids])
            )
            completed_claim = next(
                item
                for item in candidate["claims"]
                if item["claim_id"] == claim["claim_id"]
            )
            revision_after = arguments["expected_revision"] + 1
            completed_claim.update(
                {
                    "status": "released",
                    "expected_project_revision": revision_after,
                    "released_at": observed_at,
                }
            )
            _upsert_change(changes, collection="works", value=completed_work)
            _upsert_change(changes, collection="claims", value=completed_claim)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(
                tool=LOCAL_WORK_COMPLETION_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=LOCAL_WORK_COMPLETION_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "already_completed": False,
                "evidence_ids": requested_ids,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(
            LOCAL_WORK_COMPLETION_TOOL, arguments, context, response
        )
        return response

    def _activate_local_work(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Create or activate one source-bound Work and claim it in one Event."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]

        def denied(gate: str, *, code: str = "integrity") -> dict[str, Any]:
            return _response(
                tool=LOCAL_WORK_ACTIVATION_TOOL,
                request_id=request_id,
                error_code=code,
                error_message=f"activation_gate:{gate}",
            )

        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            existing_claim = next(
                (
                    item
                    for item in snapshot["claims"]
                    if item["claim_id"] == arguments["claim_id"]
                ),
                None,
            )
            identity_matches = work is not None and (
                work["title"] == arguments["work_title"]
                and work["owner_refs"] == [arguments["owner_ref"]]
                and work["scope_refs"] == arguments["scope_owners"]
            )
            if (
                identity_matches
                and work["status"] == "active"
                and existing_claim is not None
                and existing_claim["status"] == "active"
                and existing_claim["work_id"] == work["work_id"]
                and existing_claim["actor_ref"] == context.subject_ref
            ):
                event_head = (
                    None
                    if not events
                    else {
                        "sequence_no": events[-1]["sequence_no"],
                        "event_sha256": events[-1]["event_sha256"],
                    }
                )
                response = _response(
                    tool=LOCAL_WORK_ACTIVATION_TOOL,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": snapshot["project"]["revision"],
                        "event_head": event_head,
                        "event": None,
                        "checkpoint_ref": copy.deepcopy(arguments["checkpoint_ref"]),
                        "already_activated": True,
                        "claim": copy.deepcopy(existing_claim),
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
                self._remember_request(
                    LOCAL_WORK_ACTIVATION_TOOL, arguments, context, response
                )
                return response
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return denied("expected_revision", code="conflict")
            if snapshot["schema_version"] == "context.typed-state/v6alpha1":
                return denied("shared_state_adapter", code="capability")
            if (
                snapshot["project"]["active_work_ids"]
                or snapshot["project"]["primary_work_id"] is not None
                or any(item["status"] == "active" for item in snapshot["claims"])
            ):
                return denied("idle_state", code="conflict")
            if context.subject_ref != arguments["owner_ref"]:
                return denied("owner", code="permission_denied")
            if existing_claim is not None:
                return denied("claim_identity", code="conflict")
            if work is not None and (not identity_matches or work["status"] != "ready"):
                return denied("work_identity", code="conflict")
            if any(
                item["status"] in {"authorized", "started"}
                for item in snapshot["effects"]
            ):
                return denied("pending_effects", code="conflict")
            source_evidence_id = arguments["source_evidence_id"]
            source_evidence = next(
                (
                    item
                    for item in snapshot["evidence"]
                    if item["evidence_id"] == source_evidence_id
                ),
                None,
            )
            if source_evidence is None or source_evidence["validity"] != "verified":
                return denied("source_fresh")
            observed_at = self._clock()
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            lease_expires = datetime.fromisoformat(
                arguments["lease_expires_at"].replace("Z", "+00:00")
            )
            if lease_expires <= observed:
                return denied("lease")

            candidate = copy.deepcopy(snapshot)
            changes: list[dict[str, Any]] = []
            if work is None:
                activated_work = {
                    "work_id": arguments["work_id"],
                    "kind": "work",
                    "title": arguments["work_title"],
                    "status": "active",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": [arguments["owner_ref"]],
                    "scope_refs": copy.deepcopy(arguments["scope_owners"]),
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [source_evidence_id],
                    "blocker_ids": [],
                    "revision": 1,
                }
                candidate["works"].append(activated_work)
            else:
                activated_work = next(
                    item
                    for item in candidate["works"]
                    if item["work_id"] == work["work_id"]
                )
                activated_work["status"] = "active"
                activated_work["revision"] += 1
                activated_work["evidence_ids"] = list(
                    dict.fromkeys(
                        [*activated_work["evidence_ids"], source_evidence_id]
                    )
                )
            revision_after = arguments["expected_revision"] + 1
            claim = {
                "claim_id": arguments["claim_id"],
                "work_id": activated_work["work_id"],
                "actor_ref": context.subject_ref,
                "status": "active",
                "expected_project_revision": revision_after,
                "claimed_at": observed_at,
                "lease_expires_at": arguments["lease_expires_at"],
                "released_at": None,
                "scope_owners": copy.deepcopy(arguments["scope_owners"]),
            }
            candidate["claims"].append(claim)
            _upsert_change(changes, collection="works", value=activated_work)
            _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            if self._transition_checkpoint_publisher is None:
                return denied("checkpoint_publisher", code="capability")
            checkpoint_input = {
                "snapshot": expected_snapshot,
                "revision": revision_after,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            }
            try:
                checkpoint_ref = self._transition_checkpoint_publisher(
                    checkpoint_input
                )
            except Exception:  # noqa: BLE001
                return denied("checkpoint_publication")
            checkpoint_fields = {
                "schema_version",
                "digest_algorithm",
                "digest",
                "size_bytes",
                "artifact_uri",
            }
            if (
                not isinstance(checkpoint_ref, dict)
                or set(checkpoint_ref) != checkpoint_fields
                or checkpoint_ref["schema_version"] != "context.artifact-ref/v1alpha1"
                or checkpoint_ref["digest_algorithm"] != "sha-256"
                or not isinstance(checkpoint_ref["digest"], str)
                or _SHA256_RE.fullmatch(checkpoint_ref["digest"]) is None
                or type(checkpoint_ref["size_bytes"]) is not int
                or checkpoint_ref["size_bytes"] <= 0
                or checkpoint_ref["artifact_uri"]
                != f"artifact://sha256/{checkpoint_ref['digest']}"
            ):
                return denied("checkpoint_publication")
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(
                tool=LOCAL_WORK_ACTIVATION_TOOL,
                request_id=request_id,
                error=exc,
            )

        response = _response(
            tool=LOCAL_WORK_ACTIVATION_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": revision_after,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "checkpoint_ref": checkpoint_ref,
                "already_activated": False,
                "claim": claim,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(
            LOCAL_WORK_ACTIVATION_TOOL, arguments, context, response
        )
        return response

    def _transition_local_work(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Complete a dependency and claim its declared return point in one Event."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]

        def denied(
            gate: str,
            *,
            code: str = "integrity",
        ) -> dict[str, Any]:
            return _response(
                tool=LOCAL_WORK_TRANSITION_TOOL,
                request_id=request_id,
                error_code=code,
                error_message=f"transition_gate:{gate}",
            )

        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return denied("expected_revision", code="conflict")
            if snapshot["schema_version"] == "context.typed-state/v6alpha1":
                return denied("shared_state_adapter", code="capability")

            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            claim = next(
                (
                    item
                    for item in snapshot["claims"]
                    if item["claim_id"] == arguments["claim_id"]
                ),
                None,
            )
            return_work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["return_point_work_id"]
                ),
                None,
            )
            blocker = next(
                (
                    item
                    for item in snapshot["blockers"]
                    if item["blocker_id"] == arguments["resolved_blocker_id"]
                ),
                None,
            )
            if work is None or claim is None or return_work is None or blocker is None:
                return denied("declared_transition_objects", code="not_found")
            if (
                work["status"] != "active"
                or snapshot["project"]["primary_work_id"] != work["work_id"]
                or snapshot["project"]["active_work_ids"] != [work["work_id"]]
            ):
                return denied("active_work")

            observed_at = self._clock()
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            lease_expires = datetime.fromisoformat(
                claim["lease_expires_at"].replace("Z", "+00:00")
            )
            successor_lease = datetime.fromisoformat(
                arguments["lease_expires_at"].replace("Z", "+00:00")
            )
            if (
                claim["status"] != "active"
                or claim["work_id"] != work["work_id"]
                or claim["actor_ref"] != context.subject_ref
                or claim["expected_project_revision"] != arguments["expected_revision"]
            ):
                return denied("active_claim_owner", code="conflict")
            if observed >= lease_expires:
                return denied("lease_valid", code="conflict")
            if successor_lease <= observed:
                return denied("successor_lease")
            if work["parent_work_id"] != return_work["work_id"]:
                return denied("predeclared_return_point")
            if (
                return_work["status"] != "ready"
                or context.subject_ref not in return_work["owner_refs"]
            ):
                return denied("return_point_ready_owner")
            if return_work["scope_refs"] != arguments["successor_scope_owners"]:
                return denied("scope_no_expansion")
            if any(
                item["claim_id"] == arguments["successor_claim_id"]
                for item in snapshot["claims"]
            ):
                return denied("successor_claim_identity", code="conflict")
            if any(
                item["status"] in {"authorized", "started"}
                and item["work_id"] == work["work_id"]
                for item in snapshot["effects"]
            ):
                return denied("pending_effects", code="conflict")
            if (
                blocker["status"] != "open"
                or return_work["work_id"] not in blocker["blocked_work_ids"]
                or blocker["blocker_id"] not in return_work["blocker_ids"]
                or not set(blocker["evidence_ids"]).issubset(work["evidence_ids"])
            ):
                return denied("declared_dependency_blocker")

            source_evidence_id = arguments["source_evidence_id"]
            source_evidence = next(
                (
                    item
                    for item in snapshot["evidence"]
                    if item["evidence_id"] == source_evidence_id
                ),
                None,
            )
            if (
                source_evidence is None
                or source_evidence["validity"] != "verified"
            ):
                return denied("source_fresh")
            source_evidence_rebound = source_evidence_id not in return_work["evidence_ids"]
            workspace = arguments["workspace_verification"]
            if workspace["clean"] is not True:
                return denied("workspace_clean")
            if (
                workspace["expected_ref"] is not None
                and workspace["expected_ref_commit"] != workspace["head_commit"]
            ):
                return denied("workspace_ref")

            requested_evidence = copy.deepcopy(arguments["evidence"])
            requested_ids = [item["evidence_id"] for item in requested_evidence]
            evidence_by_id = {
                item["evidence_id"]: item for item in snapshot["evidence"]
            }
            if any(item["evidence_id"] in evidence_by_id for item in requested_evidence):
                return denied("evidence_identity", code="conflict")
            remaining = copy.deepcopy(arguments["remaining_blocker"])
            if remaining is not None:
                if remaining["blocker_id"] == blocker["blocker_id"] or any(
                    item["blocker_id"] == remaining["blocker_id"]
                    for item in snapshot["blockers"]
                ):
                    return denied("remaining_blocker_identity", code="conflict")
                if not set(remaining["evidence_ids"]).issubset(requested_ids):
                    return denied("remaining_blocker_evidence")

            candidate = copy.deepcopy(snapshot)
            changes: list[dict[str, Any]] = []
            for item in requested_evidence:
                change = {
                    "collection": "evidence",
                    "object_id": item["evidence_id"],
                    "value": item,
                }
                _replace_or_append(candidate, change)
                changes.append(change)

            completed_work = next(
                item for item in candidate["works"] if item["work_id"] == work["work_id"]
            )
            completed_work["status"] = "completed"
            completed_work["revision"] += 1
            completed_work["evidence_ids"] = list(
                dict.fromkeys([*completed_work["evidence_ids"], *requested_ids])
            )
            completed_claim = next(
                item
                for item in candidate["claims"]
                if item["claim_id"] == claim["claim_id"]
            )
            revision_after = arguments["expected_revision"] + 1
            completed_claim.update(
                {
                    "status": "released",
                    "expected_project_revision": revision_after,
                    "released_at": observed_at,
                }
            )
            resolved_blocker = next(
                item
                for item in candidate["blockers"]
                if item["blocker_id"] == blocker["blocker_id"]
            )
            resolved_blocker["status"] = "resolved"
            resolved_blocker["resolved_at"] = observed_at
            returned_work = next(
                item
                for item in candidate["works"]
                if item["work_id"] == return_work["work_id"]
            )
            returned_work["status"] = "active"
            returned_work["revision"] += 1
            returned_work["evidence_ids"] = list(
                dict.fromkeys([*returned_work["evidence_ids"], source_evidence_id])
            )
            returned_work["blocker_ids"] = [
                item
                for item in returned_work["blocker_ids"]
                if item != blocker["blocker_id"]
            ]
            if remaining is not None:
                remaining_blocker = {
                    "blocker_id": remaining["blocker_id"],
                    "status": "open",
                    "reason": remaining["reason"],
                    "blocked_work_ids": [returned_work["work_id"]],
                    "evidence_ids": sorted(remaining["evidence_ids"]),
                    "opened_at": observed_at,
                    "resolved_at": None,
                    "supersedes_blocker_id": None,
                }
                candidate["blockers"].append(remaining_blocker)
                returned_work["blocker_ids"] = sorted(
                    {*returned_work["blocker_ids"], remaining_blocker["blocker_id"]}
                )
                _upsert_change(
                    changes,
                    collection="blockers",
                    value=remaining_blocker,
                )
            successor_claim = {
                "claim_id": arguments["successor_claim_id"],
                "work_id": returned_work["work_id"],
                "actor_ref": context.subject_ref,
                "status": "active",
                "expected_project_revision": revision_after,
                "claimed_at": observed_at,
                "lease_expires_at": arguments["lease_expires_at"],
                "released_at": None,
                "scope_owners": copy.deepcopy(arguments["successor_scope_owners"]),
            }
            candidate["claims"].append(successor_claim)
            _upsert_change(changes, collection="works", value=completed_work)
            _upsert_change(changes, collection="claims", value=completed_claim)
            _upsert_change(changes, collection="blockers", value=resolved_blocker)
            _upsert_change(changes, collection="works", value=returned_work)
            _upsert_change(changes, collection="claims", value=successor_claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            if self._transition_checkpoint_publisher is None:
                return denied("checkpoint_publisher", code="capability")
            checkpoint_input = {
                "snapshot": expected_snapshot,
                "revision": revision_after,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            }
            try:
                checkpoint_ref = self._transition_checkpoint_publisher(
                    checkpoint_input
                )
            except Exception:  # noqa: BLE001
                return denied("checkpoint_publication")
            checkpoint_fields = {
                "schema_version",
                "digest_algorithm",
                "digest",
                "size_bytes",
                "artifact_uri",
            }
            if (
                not isinstance(checkpoint_ref, dict)
                or set(checkpoint_ref) != checkpoint_fields
                or checkpoint_ref["schema_version"] != "context.artifact-ref/v1alpha1"
                or checkpoint_ref["digest_algorithm"] != "sha-256"
                or not isinstance(checkpoint_ref["digest"], str)
                or _SHA256_RE.fullmatch(checkpoint_ref["digest"]) is None
                or type(checkpoint_ref["size_bytes"]) is not int
                or checkpoint_ref["size_bytes"] <= 0
                or checkpoint_ref["artifact_uri"]
                != f"artifact://sha256/{checkpoint_ref['digest']}"
            ):
                return denied("checkpoint_publication")

            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(
                tool=LOCAL_WORK_TRANSITION_TOOL,
                request_id=request_id,
                error=exc,
            )

        response = _response(
            tool=LOCAL_WORK_TRANSITION_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "checkpoint_ref": checkpoint_ref,
                "already_transitioned": False,
                "evidence_ids": requested_ids,
                "successor_claim": successor_claim,
                "completion_policy": {
                    "status": "granted",
                    "authority": "checkpoint-bound-local-transition",
                    "actor_ref": context.subject_ref,
                    "scope_expanded": False,
                },
                "source_evidence_rebound": source_evidence_rebound,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(
            LOCAL_WORK_TRANSITION_TOOL,
            arguments,
            context,
            response,
        )
        return response

    def _recover_local_claim(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Recover a legacy local claim without changing its identity in place."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot.get("schema_version") == "context.typed-state/v6alpha1":
                return _response(
                    tool=LOCAL_CLAIM_RECOVERY_TOOL,
                    request_id=request_id,
                    error_code="capability",
                    error_message="v6 claims require the fenced shared lifecycle adapter",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=LOCAL_CLAIM_RECOVERY_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            claim = next(
                (
                    item
                    for item in snapshot["claims"]
                    if item["claim_id"] == arguments["claim_id"]
                ),
                None,
            )
            if claim is None:
                return _response(
                    tool=LOCAL_CLAIM_RECOVERY_TOOL,
                    request_id=request_id,
                    error_code="not_found",
                    error_message="claim was not found",
                )
            if claim["actor_ref"] != context.subject_ref:
                return _response(
                    tool=LOCAL_CLAIM_RECOVERY_TOOL,
                    request_id=request_id,
                    error_code="permission_denied",
                    error_message="claim actor does not match trusted context",
                )
            now_text = self._clock()
            now = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
            lease_expires = datetime.fromisoformat(
                claim["lease_expires_at"].replace("Z", "+00:00")
            )
            if arguments["action"] == "heartbeat":
                if claim["status"] != "active" or now >= lease_expires:
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="expired claim cannot be heartbeated",
                    )
                updated_claim = copy.deepcopy(claim)
                updated_claim["lease_expires_at"] = (
                    now + timedelta(milliseconds=arguments["lease_ttl_ms"])
                ).isoformat()
                revision_after = arguments["expected_revision"] + 1
                updated_claim["expected_project_revision"] = revision_after
                changes = [
                    {
                        "collection": "claims",
                        "object_id": updated_claim["claim_id"],
                        "value": updated_claim,
                    }
                ]
                operation = "heartbeat"
                result_claim = updated_claim
            else:
                if claim["status"] != "active" or now < lease_expires:
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="claim is not expired",
                    )
                new_claim_id = arguments["new_claim_id"]
                if any(item["claim_id"] == new_claim_id for item in snapshot["claims"]):
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="new claim identity is already in use",
                    )
                if arguments["scope_owners"] != claim["scope_owners"]:
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="reclaim cannot widen claim scope",
                    )
                revision_after = arguments["expected_revision"] + 1
                expired_claim = copy.deepcopy(claim)
                expired_claim.update(
                    {
                        "status": "expired",
                        "released_at": now_text,
                        "expected_project_revision": revision_after,
                    }
                )
                result_claim = {
                    "claim_id": new_claim_id,
                    "work_id": claim["work_id"],
                    "actor_ref": context.subject_ref,
                    "status": "active",
                    "expected_project_revision": revision_after,
                    "claimed_at": now_text,
                    "lease_expires_at": (
                        now + timedelta(milliseconds=arguments["lease_ttl_ms"])
                    ).isoformat(),
                    "released_at": None,
                    "scope_owners": copy.deepcopy(claim["scope_owners"]),
                }
                changes = [
                    {
                        "collection": "claims",
                        "object_id": expired_claim["claim_id"],
                        "value": expired_claim,
                    },
                    {
                        "collection": "claims",
                        "object_id": result_claim["claim_id"],
                        "value": result_claim,
                    },
                ]
                operation = "reclaim"

            candidate = copy.deepcopy(snapshot)
            for change in changes:
                _replace_or_append(candidate, change)
            if operation == "reclaim":
                # Other active claims/effects retain the new project revision.
                for item in candidate["claims"]:
                    if item["status"] == "active" and item["claim_id"] != result_claim["claim_id"]:
                        item["expected_project_revision"] = revision_after
                        _upsert_change(changes, collection="claims", value=item)
                _derive_project_projection(candidate, revision=revision_after, updated_at=now_text)
            else:
                _derive_project_projection(candidate, revision=revision_after, updated_at=now_text)
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=now_text,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            checkpoint_ref = None
            if self._transition_checkpoint_publisher is not None:
                checkpoint_input = {
                    "snapshot": expected_snapshot,
                    "revision": revision_after,
                    "event_head": {
                        "sequence_no": event["sequence_no"],
                        "event_sha256": event["event_sha256"],
                    },
                    "registry_digest": self._registry_hash_value,
                    "capabilities": capability_manifest_to_document(self._manifest),
                }
                try:
                    checkpoint_ref = self._transition_checkpoint_publisher(
                        checkpoint_input
                    )
                except Exception:  # noqa: BLE001
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="checkpoint",
                        error_message="claim recovery checkpoint publication failed",
                    )
                checkpoint_fields = {
                    "schema_version",
                    "digest_algorithm",
                    "digest",
                    "size_bytes",
                    "artifact_uri",
                }
                if (
                    not isinstance(checkpoint_ref, dict)
                    or set(checkpoint_ref) != checkpoint_fields
                    or checkpoint_ref["schema_version"]
                    != "context.artifact-ref/v1alpha1"
                    or checkpoint_ref["digest_algorithm"] != "sha-256"
                    or not isinstance(checkpoint_ref["digest"], str)
                    or _SHA256_RE.fullmatch(checkpoint_ref["digest"]) is None
                    or type(checkpoint_ref["size_bytes"]) is not int
                    or checkpoint_ref["size_bytes"] <= 0
                    or checkpoint_ref["artifact_uri"]
                    != f"artifact://sha256/{checkpoint_ref['digest']}"
                ):
                    return _response(
                        tool=LOCAL_CLAIM_RECOVERY_TOOL,
                        request_id=request_id,
                        error_code="checkpoint",
                        error_message="claim recovery checkpoint publication failed",
                    )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
            ValueError,
        ) as exc:
            return _backend_error_response(
                tool=LOCAL_CLAIM_RECOVERY_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=LOCAL_CLAIM_RECOVERY_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "operation": operation,
                "claim": result_claim,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
                **(
                    {"checkpoint_ref": checkpoint_ref}
                    if checkpoint_ref is not None
                    else {}
                ),
            },
        )
        self._remember_request(LOCAL_CLAIM_RECOVERY_TOOL, arguments, context, response)
        return response
