"""Authority-bound M5-06 context return packets for Idea interruptions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable

from .artifact_store import ArtifactRef
from .idea_review import IdeaReviewError, packet_eligible_ideas
from .postcompact_canary import (
    PostCompactCanaryError,
    validate_postcompact_canary_receipt,
)
from .typed_state import (
    TypedStateError,
    canonical_state_bytes,
    validate_typed_state,
)

SCHEMA_VERSION = "context.idea-return-packet/v1alpha1"
COMPOSER_VERSION = "context.idea-return-packet-composer/v1alpha1"
CHECKPOINT_BINDING_VERSION = "context.idea-return-checkpoint-binding/v1alpha1"
MIGRATION_SCHEMA_VERSION = "context.return-context-migration/v1alpha1"
MAX_PACKET_BYTES = 8 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SAFE_TEXT_RE = re.compile(r"^[^\r\n\x00]+$")

_PACKET_FIELDS = {
    "schema_version",
    "composer_version",
    "project_id",
    "project_revision",
    "governance_ref",
    "canonical_plan_sha256",
    "registry_digest",
    "state_sha256",
    "observed_at",
    "active_leaf",
    "historical_checkpoint",
    "return_point",
    "progress",
    "idea_refs",
    "next_action",
    "governance_migration_ref",
    "execution_gate",
    "candidate_execution_authority",
    "candidate_state_write_authority",
    "state_write_authority",
    "external_effect_authority",
    "forbidden_without_state_mcp",
    "packet_sha256",
}
_ACTIVE_FIELDS = {"work_id", "title", "status", "revision"}
_HISTORICAL_FIELDS = {
    "checkpoint_ref",
    "project_revision",
    "return_work_id",
    "return_work_revision",
    "canary_sha256",
    "authority_role",
}
_RETURN_POINT_FIELDS = {
    "work_id",
    "historical_work_revision",
    "current_work_revision",
}
_PROGRESS_FIELDS = {
    "decision_ids",
    "constraint_ids",
    "blocker_ids",
    "evidence_ids",
    "scope_refs",
}
_SCOPE_FIELDS = {"scope_kind", "scope_ref"}
_IDEA_FIELDS = {"idea_id", "status", "return_work_id", "authority"}
_MIGRATION_REF_FIELDS = {"migration_id", "receipt_sha256", "authority_event_ref"}
_CHECKPOINT_BINDING_FIELDS = {
    "schema_version",
    "project_id",
    "project_revision",
    "return_work_id",
    "return_work_revision",
    "canonical_plan_sha256",
    "registry_digest",
    "checkpoint_ref",
}
_RETURN_FRAME_FIELDS = {
    "checkpoint_ref",
    "old_work_id",
    "old_work_revision",
    "proposal_sha256",
    "old_project_revision",
    "return_work_id",
    "return_work_revision",
    "current_work_id",
}
_MIGRATION_FIELDS = {
    "schema_version",
    "migration_id",
    "project_id",
    "return_work_id",
    "from_project_revision",
    "to_project_revision",
    "from_canonical_plan_sha256",
    "to_canonical_plan_sha256",
    "from_registry_digest",
    "to_registry_digest",
    "authority_event_ref",
    "authorization_ref",
    "status",
    "receipt_sha256",
}
_FORBIDDEN_WITHOUT_STATE_MCP = [
    "external-effect",
    "idea-activation",
    "state-write",
    "task-switch",
]


class IdeaReturnPacketError(ValueError):
    """Raised when a return packet cannot prove a safe original-task restore."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _body(document: dict[str, Any], digest_field: str) -> dict[str, Any]:
    value = copy.deepcopy(document)
    value.pop(digest_field, None)
    return value


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise IdeaReturnPacketError(f"{field} fields are invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise IdeaReturnPacketError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _SAFE_TEXT_RE.fullmatch(value) is None
    ):
        raise IdeaReturnPacketError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IdeaReturnPacketError(f"{field} must be lowercase SHA-256")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise IdeaReturnPacketError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise IdeaReturnPacketError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdeaReturnPacketError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IdeaReturnPacketError(f"{field} must include timezone")
    return parsed


def _ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise IdeaReturnPacketError(f"{field} must be a list")
    result = [_id(item, field) for item in value]
    if result != sorted(result) or len(result) != len(set(result)):
        raise IdeaReturnPacketError(f"{field} must contain sorted unique IDs")
    return result


def _artifact_ref(value: Any, field: str) -> dict[str, Any]:
    try:
        return ArtifactRef.from_document(value).to_document()
    except (TypeError, ValueError) as exc:
        raise IdeaReturnPacketError(f"{field} is invalid") from exc


def _migration_digest(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_body(receipt, "receipt_sha256"))).hexdigest()


def build_return_context_migration_receipt(
    *,
    migration_id: str,
    project_id: str,
    return_work_id: str,
    from_project_revision: int,
    to_project_revision: int,
    from_canonical_plan_sha256: str,
    to_canonical_plan_sha256: str,
    from_registry_digest: str,
    to_registry_digest: str,
    authority_event_ref: str,
    authorization_ref: str,
) -> dict[str, Any]:
    """Build a digest-bound receipt for an already authorized governance migration."""
    receipt = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "project_id": project_id,
        "return_work_id": return_work_id,
        "from_project_revision": from_project_revision,
        "to_project_revision": to_project_revision,
        "from_canonical_plan_sha256": from_canonical_plan_sha256,
        "to_canonical_plan_sha256": to_canonical_plan_sha256,
        "from_registry_digest": from_registry_digest,
        "to_registry_digest": to_registry_digest,
        "authority_event_ref": authority_event_ref,
        "authorization_ref": authorization_ref,
        "status": "committed",
        "receipt_sha256": "pending",
    }
    receipt["receipt_sha256"] = _migration_digest(receipt)
    _validate_migration_receipt(receipt)
    return receipt


def _validate_migration_receipt(receipt: Any) -> dict[str, Any]:
    receipt = _object(receipt, _MIGRATION_FIELDS, "migration receipt")
    if receipt["schema_version"] != MIGRATION_SCHEMA_VERSION:
        raise IdeaReturnPacketError("migration receipt version is invalid")
    for field in (
        "migration_id",
        "project_id",
        "return_work_id",
        "authority_event_ref",
        "authorization_ref",
    ):
        _id(receipt[field], f"migration receipt {field}")
    _uint(receipt["from_project_revision"], "migration receipt from_project_revision")
    _uint(receipt["to_project_revision"], "migration receipt to_project_revision")
    if receipt["to_project_revision"] < receipt["from_project_revision"]:
        raise IdeaReturnPacketError("migration receipt revision order is invalid")
    for field in (
        "from_canonical_plan_sha256",
        "to_canonical_plan_sha256",
        "from_registry_digest",
        "to_registry_digest",
        "receipt_sha256",
    ):
        _sha(receipt[field], f"migration receipt {field}")
    if receipt["status"] != "committed":
        raise IdeaReturnPacketError("migration receipt is not committed")
    if receipt["receipt_sha256"] != _migration_digest(receipt):
        raise IdeaReturnPacketError("migration receipt digest mismatch")
    return copy.deepcopy(receipt)


def _validate_return_inputs(
    *,
    return_frame: Any,
    checkpoint_binding: Any,
    postcompact_canary: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    frame = copy.deepcopy(_object(return_frame, _RETURN_FRAME_FIELDS, "return frame"))
    binding = copy.deepcopy(
        _object(checkpoint_binding, _CHECKPOINT_BINDING_FIELDS, "checkpoint binding")
    )
    if binding["schema_version"] != CHECKPOINT_BINDING_VERSION:
        raise IdeaReturnPacketError("checkpoint binding version is invalid")
    for field in ("old_work_id", "return_work_id", "current_work_id"):
        _id(frame[field], f"return frame {field}")
    for field in ("old_work_revision", "return_work_revision", "old_project_revision"):
        _uint(frame[field], f"return frame {field}")
    _sha(frame["proposal_sha256"], "return frame proposal_sha256")
    frame_ref = _artifact_ref(frame["checkpoint_ref"], "return frame checkpoint_ref")
    for field in ("project_id", "return_work_id"):
        _id(binding[field], f"checkpoint binding {field}")
    for field in ("project_revision", "return_work_revision"):
        _uint(binding[field], f"checkpoint binding {field}")
    for field in ("canonical_plan_sha256", "registry_digest"):
        _sha(binding[field], f"checkpoint binding {field}")
    binding_ref = _artifact_ref(binding["checkpoint_ref"], "checkpoint binding checkpoint_ref")
    if (
        frame["old_work_id"] != frame["return_work_id"]
        or frame["old_work_revision"] != frame["return_work_revision"]
        or frame["checkpoint_ref"] != frame_ref
        or binding["checkpoint_ref"] != binding_ref
        or frame_ref != binding_ref
        or binding["project_revision"] != frame["old_project_revision"]
        or binding["return_work_id"] != frame["return_work_id"]
        or binding["return_work_revision"] != frame["return_work_revision"]
    ):
        raise IdeaReturnPacketError("return frame and checkpoint binding differ")
    try:
        validate_postcompact_canary_receipt(postcompact_canary)
    except (PostCompactCanaryError, TypeError) as exc:
        raise IdeaReturnPacketError("PostCompact canary is invalid") from exc
    canary = copy.deepcopy(postcompact_canary)
    if (
        canary["project_id"] != binding["project_id"]
        or canary["project_revision"] != binding["project_revision"]
        or canary["active_work_id"] != binding["return_work_id"]
        or canary["task_revision"] != binding["return_work_revision"]
        or canary["checkpoint_sha256"] != binding_ref["digest"]
    ):
        raise IdeaReturnPacketError("PostCompact canary does not prove the historical return point")
    return frame, binding, canary


def _validate_migration_binding(
    receipt: Any,
    *,
    binding: dict[str, Any],
    project_revision: int,
    canonical_plan_sha256: str,
    registry_digest: str,
) -> dict[str, Any]:
    migration = _validate_migration_receipt(receipt)
    expected = {
        "project_id": binding["project_id"],
        "return_work_id": binding["return_work_id"],
        "from_project_revision": binding["project_revision"],
        "to_project_revision": project_revision,
        "from_canonical_plan_sha256": binding["canonical_plan_sha256"],
        "to_canonical_plan_sha256": canonical_plan_sha256,
        "from_registry_digest": binding["registry_digest"],
        "to_registry_digest": registry_digest,
    }
    if any(migration[field] != value for field, value in expected.items()):
        raise IdeaReturnPacketError("migration receipt does not bind current authority")
    return migration


def _packet_digest(packet: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_body(packet, "packet_sha256"))).hexdigest()


def canonical_idea_return_packet_bytes(packet: dict[str, Any]) -> bytes:
    """Return canonical bytes after strict packet validation."""
    validate_idea_return_packet(packet)
    return _canonical(packet)


def validate_idea_return_packet(packet: Any, *, verify_digest: bool = True) -> None:
    packet = _object(packet, _PACKET_FIELDS, "Idea return packet")
    if packet["schema_version"] != SCHEMA_VERSION or packet["composer_version"] != COMPOSER_VERSION:
        raise IdeaReturnPacketError("Idea return packet identity is invalid")
    _id(packet["project_id"], "project_id")
    _uint(packet["project_revision"], "project_revision")
    _text(packet["governance_ref"], "governance_ref")
    for field in ("canonical_plan_sha256", "registry_digest", "state_sha256", "packet_sha256"):
        _sha(packet[field], field)
    _timestamp(packet["observed_at"], "observed_at")

    active = _object(packet["active_leaf"], _ACTIVE_FIELDS, "active_leaf")
    _id(active["work_id"], "active_leaf.work_id")
    _text(active["title"], "active_leaf.title", maximum=4096)
    if active["status"] != "active":
        raise IdeaReturnPacketError("active_leaf is not active")
    _uint(active["revision"], "active_leaf.revision")

    historical = _object(
        packet["historical_checkpoint"], _HISTORICAL_FIELDS, "historical_checkpoint"
    )
    _artifact_ref(historical["checkpoint_ref"], "historical_checkpoint.checkpoint_ref")
    _uint(historical["project_revision"], "historical_checkpoint.project_revision")
    _id(historical["return_work_id"], "historical_checkpoint.return_work_id")
    _uint(historical["return_work_revision"], "historical_checkpoint.return_work_revision")
    _sha(historical["canary_sha256"], "historical_checkpoint.canary_sha256")
    if historical["authority_role"] != "recovery-evidence-only":
        raise IdeaReturnPacketError("historical checkpoint cannot claim current authority")

    point = _object(packet["return_point"], _RETURN_POINT_FIELDS, "return_point")
    _id(point["work_id"], "return_point.work_id")
    _uint(point["historical_work_revision"], "return_point.historical_work_revision")
    _uint(point["current_work_revision"], "return_point.current_work_revision")
    if (
        active["work_id"] != point["work_id"]
        or historical["return_work_id"] != point["work_id"]
        or historical["return_work_revision"] != point["historical_work_revision"]
        or active["revision"] != point["current_work_revision"]
        or point["current_work_revision"] < point["historical_work_revision"]
        or packet["project_revision"] < historical["project_revision"]
    ):
        raise IdeaReturnPacketError("return point revision binding is invalid")

    progress = _object(packet["progress"], _PROGRESS_FIELDS, "progress")
    for field in ("decision_ids", "constraint_ids", "blocker_ids", "evidence_ids"):
        _ids(progress[field], f"progress.{field}")
    if not isinstance(progress["scope_refs"], list):
        raise IdeaReturnPacketError("progress.scope_refs must be a list")
    scope_keys = []
    for value in progress["scope_refs"]:
        scope = _object(value, _SCOPE_FIELDS, "progress.scope_ref")
        scope_keys.append(
            (
                _text(scope["scope_kind"], "progress.scope_kind", maximum=64),
                _text(scope["scope_ref"], "progress.scope_ref", maximum=2048),
            )
        )
    if scope_keys != sorted(scope_keys) or len(scope_keys) != len(set(scope_keys)):
        raise IdeaReturnPacketError("progress.scope_refs must be sorted and unique")

    if not isinstance(packet["idea_refs"], list):
        raise IdeaReturnPacketError("idea_refs must be a list")
    idea_ids = []
    for value in packet["idea_refs"]:
        idea = _object(value, _IDEA_FIELDS, "idea_ref")
        idea_ids.append(_id(idea["idea_id"], "idea_ref.idea_id"))
        if idea["status"] not in {"candidate", "proposed", "approved"}:
            raise IdeaReturnPacketError("idea_ref status is not packet eligible")
        if idea["return_work_id"] != active["work_id"]:
            raise IdeaReturnPacketError("idea_ref return point is unrelated")
        if idea["authority"] != "candidate-only":
            raise IdeaReturnPacketError("idea_ref authority is invalid")
    if idea_ids != sorted(idea_ids) or len(idea_ids) != len(set(idea_ids)):
        raise IdeaReturnPacketError("idea_refs must be sorted and unique")

    _text(packet["next_action"], "next_action", maximum=1024)
    migration_ref = packet["governance_migration_ref"]
    if migration_ref is not None:
        migration_ref = _object(migration_ref, _MIGRATION_REF_FIELDS, "governance_migration_ref")
        _id(migration_ref["migration_id"], "governance_migration_ref.migration_id")
        _sha(migration_ref["receipt_sha256"], "governance_migration_ref.receipt_sha256")
        _id(migration_ref["authority_event_ref"], "governance_migration_ref.authority_event_ref")
    if packet["execution_gate"] != "allow-original-task-only":
        raise IdeaReturnPacketError("execution gate is invalid")
    for field in (
        "candidate_execution_authority",
        "candidate_state_write_authority",
        "state_write_authority",
        "external_effect_authority",
    ):
        if packet[field] is not False:
            raise IdeaReturnPacketError(f"{field} authority must remain false")
    if packet["forbidden_without_state_mcp"] != _FORBIDDEN_WITHOUT_STATE_MCP:
        raise IdeaReturnPacketError("forbidden action gate is invalid")
    if verify_digest and packet["packet_sha256"] != _packet_digest(packet):
        raise IdeaReturnPacketError("packet_sha256 does not match packet content")
    if len(_canonical(packet)) > MAX_PACKET_BYTES:
        raise IdeaReturnPacketError("Idea return packet exceeds 8 KiB")


def compose_idea_return_packet(
    *,
    snapshot: dict[str, Any],
    return_frame: dict[str, Any],
    checkpoint_binding: dict[str, Any],
    postcompact_canary: dict[str, Any],
    canonical_plan_sha256: str,
    registry_digest: str,
    next_action: str,
    observed_at: str,
    trusted_migration_receipt: dict[str, Any] | None = None,
    migration_receipt_verifier: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Compose a bounded return packet from current authority and historical proof."""
    source = copy.deepcopy(snapshot)
    try:
        validate_typed_state(source)
    except TypedStateError as exc:
        raise IdeaReturnPacketError("current typed state is invalid") from exc
    if source.get("schema_version") != "context.typed-state/v4alpha1":
        raise IdeaReturnPacketError("Idea return packet requires typed state v4")
    _sha(canonical_plan_sha256, "canonical_plan_sha256")
    _sha(registry_digest, "registry_digest")
    _text(next_action, "next_action", maximum=1024)
    observed = _timestamp(observed_at, "observed_at")
    updated_at = _timestamp(source["project"]["updated_at"], "project.updated_at")
    if observed < updated_at:
        raise IdeaReturnPacketError("observed_at is older than current authority")

    frame, binding, canary = _validate_return_inputs(
        return_frame=return_frame,
        checkpoint_binding=checkpoint_binding,
        postcompact_canary=postcompact_canary,
    )
    project = source["project"]
    if project["project_id"] != binding["project_id"]:
        raise IdeaReturnPacketError("current authority project differs from checkpoint")
    if project["revision"] < binding["project_revision"]:
        raise IdeaReturnPacketError("current authority revision predates checkpoint")
    active_id = project["primary_work_id"]
    works = {item["work_id"]: item for item in source["works"]}
    active = works.get(active_id)
    if (
        active_id != frame["return_work_id"]
        or active_id not in project["active_work_ids"]
        or active is None
        or active["status"] != "active"
        or active["revision"] < frame["return_work_revision"]
    ):
        raise IdeaReturnPacketError("current authority has not returned to the original Work")
    if project["effect_high_watermark"] < canary["effect_high_watermark"]:
        raise IdeaReturnPacketError("current authority effect watermark predates checkpoint")

    drifted = (
        canonical_plan_sha256 != binding["canonical_plan_sha256"]
        or registry_digest != binding["registry_digest"]
    )
    migration = None
    if drifted:
        if trusted_migration_receipt is None:
            raise IdeaReturnPacketError("governance drift requires an explicit trusted migration")
        migration = _validate_migration_binding(
            trusted_migration_receipt,
            binding=binding,
            project_revision=project["revision"],
            canonical_plan_sha256=canonical_plan_sha256,
            registry_digest=registry_digest,
        )
        if not callable(migration_receipt_verifier):
            raise IdeaReturnPacketError(
                "migration receipt requires independent authority verification"
            )
        try:
            migration_verified = migration_receipt_verifier(copy.deepcopy(migration))
        except Exception as exc:
            raise IdeaReturnPacketError(
                "migration receipt authority verification failed"
            ) from exc
        if migration_verified is not True:
            raise IdeaReturnPacketError(
                "migration receipt authority verification failed"
            )
    elif trusted_migration_receipt is not None:
        raise IdeaReturnPacketError("migration receipt supplied without governance drift")
    elif migration_receipt_verifier is not None:
        raise IdeaReturnPacketError("migration verifier supplied without governance drift")

    try:
        eligible_ids = packet_eligible_ideas(source, now=observed_at)
    except IdeaReviewError as exc:
        raise IdeaReturnPacketError("Idea eligibility evaluation failed") from exc
    ideas_by_id = {item["idea_id"]: item for item in source["ideas"]}
    idea_refs = []
    for idea_id in eligible_ids:
        idea = ideas_by_id[idea_id]
        related = (
            idea["return_work_id"] == active_id
            and (
                idea["parent_work_id"] == active_id
                or idea.get("scope_ref") == f"work://{active_id}"
            )
        )
        if related:
            idea_refs.append(
                {
                    "idea_id": idea["idea_id"],
                    "status": idea["status"],
                    "return_work_id": idea["return_work_id"],
                    "authority": "candidate-only",
                }
            )

    decision_ids = sorted(
        item["decision_id"]
        for item in source["decisions"]
        if item["decision_id"] in project["current_decision_ids"]
        and item["status"] == "accepted"
        and item["work_id"] == active_id
    )
    constraint_ids = sorted(
        item["constraint_id"]
        for item in source["constraints"]
        if item["constraint_id"] in project["active_constraint_ids"]
        and item["status"] == "active"
        and (not item["scope_work_ids"] or active_id in item["scope_work_ids"])
    )
    blocker_ids = sorted(
        item["blocker_id"]
        for item in source["blockers"]
        if item["blocker_id"] in project["open_blocker_ids"]
        and item["status"] == "open"
        and active_id in item["blocked_work_ids"]
    )
    evidence_ids = set(active["evidence_ids"])
    selected_ids = set(decision_ids) | set(constraint_ids) | set(blocker_ids)
    for collection, id_field in (
        (source["decisions"], "decision_id"),
        (source["constraints"], "constraint_id"),
        (source["blockers"], "blocker_id"),
    ):
        for item in collection:
            if item[id_field] in selected_ids:
                evidence_ids.update(item["evidence_ids"])

    packet = {
        "schema_version": SCHEMA_VERSION,
        "composer_version": COMPOSER_VERSION,
        "project_id": project["project_id"],
        "project_revision": project["revision"],
        "governance_ref": project["governance_ref"],
        "canonical_plan_sha256": canonical_plan_sha256,
        "registry_digest": registry_digest,
        "state_sha256": hashlib.sha256(canonical_state_bytes(source)).hexdigest(),
        "observed_at": observed_at,
        "active_leaf": {
            "work_id": active["work_id"],
            "title": active["title"],
            "status": active["status"],
            "revision": active["revision"],
        },
        "historical_checkpoint": {
            "checkpoint_ref": copy.deepcopy(binding["checkpoint_ref"]),
            "project_revision": binding["project_revision"],
            "return_work_id": binding["return_work_id"],
            "return_work_revision": binding["return_work_revision"],
            "canary_sha256": canary["canary_sha256"],
            "authority_role": "recovery-evidence-only",
        },
        "return_point": {
            "work_id": active["work_id"],
            "historical_work_revision": binding["return_work_revision"],
            "current_work_revision": active["revision"],
        },
        "progress": {
            "decision_ids": decision_ids,
            "constraint_ids": constraint_ids,
            "blocker_ids": blocker_ids,
            "evidence_ids": sorted(evidence_ids),
            "scope_refs": sorted(
                copy.deepcopy(active["scope_refs"]),
                key=lambda item: (item["scope_kind"], item["scope_ref"]),
            ),
        },
        "idea_refs": idea_refs,
        "next_action": next_action,
        "governance_migration_ref": (
            None
            if migration is None
            else {
                "migration_id": migration["migration_id"],
                "receipt_sha256": migration["receipt_sha256"],
                "authority_event_ref": migration["authority_event_ref"],
            }
        ),
        "execution_gate": "allow-original-task-only",
        "candidate_execution_authority": False,
        "candidate_state_write_authority": False,
        "state_write_authority": False,
        "external_effect_authority": False,
        "forbidden_without_state_mcp": list(_FORBIDDEN_WITHOUT_STATE_MCP),
        "packet_sha256": "pending",
    }
    packet["packet_sha256"] = _packet_digest(packet)
    validate_idea_return_packet(packet)
    return packet
