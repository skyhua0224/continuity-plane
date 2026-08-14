"""Versioned migration between M2 typed state and M3 graph authority."""

from __future__ import annotations

import copy
from typing import Any

from .typed_state import (
    EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TypedStateError,
    validate_typed_state,
)


_EXPERIMENT_FIELDS = {
    "return_point_work_id",
    "exit_criteria",
    "attempt_budget",
    "expires_at",
    "promotion_target_work_id",
    "mainline_authority",
}


def migrate_v1alpha1_to_v2alpha1(
    document: dict[str, Any],
    *,
    experiment_contracts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Add explicit M3 authority fields without guessing Experiment contracts."""
    if not isinstance(experiment_contracts, dict):
        raise TypedStateError("experiment contracts must be an object")
    if document.get("schema_version") == SCHEMA_VERSION:
        migrated = copy.deepcopy(document)
        validate_typed_state(migrated)
        return migrated
    if document.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise TypedStateError("unsupported migration source")
    validate_typed_state(document)
    migrated = copy.deepcopy(document)
    migrated["schema_version"] = SCHEMA_VERSION
    for work in migrated["works"]:
        contract = experiment_contracts.get(work["work_id"])
        if work["kind"] == "experiment":
            if not isinstance(contract, dict) or set(contract) != _EXPERIMENT_FIELDS:
                raise TypedStateError("experiment migration contract is incomplete")
            work.update(copy.deepcopy(contract))
        else:
            if contract is not None:
                raise TypedStateError("non-experiment migration contract is invalid")
            work.update(
                {
                    "return_point_work_id": None,
                    "exit_criteria": [],
                    "attempt_budget": None,
                    "expires_at": None,
                    "promotion_target_work_id": None,
                    "mainline_authority": True,
                }
            )
    validate_typed_state(migrated)
    return migrated


def rollback_v2alpha1_to_v1alpha1(document: dict[str, Any]) -> dict[str, Any]:
    """Project an M3 snapshot back to the legacy readable wire model."""
    if document.get("schema_version") == LEGACY_SCHEMA_VERSION:
        legacy = copy.deepcopy(document)
        validate_typed_state(legacy)
        return legacy
    if document.get("schema_version") != SCHEMA_VERSION:
        raise TypedStateError("unsupported rollback source")
    validate_typed_state(document)
    legacy = copy.deepcopy(document)
    legacy["schema_version"] = LEGACY_SCHEMA_VERSION
    for work in legacy["works"]:
        for field in _EXPERIMENT_FIELDS:
            del work[field]
    validate_typed_state(legacy)
    return legacy


def migrate_v2alpha1_to_v3alpha1(document: dict[str, Any]) -> dict[str, Any]:
    """Add empty append-only Experiment lifecycle ledgers without inference."""
    if document.get("schema_version") == EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
        migrated = copy.deepcopy(document)
        validate_typed_state(migrated)
        return migrated
    if document.get("schema_version") != SCHEMA_VERSION:
        raise TypedStateError("unsupported migration source")
    validate_typed_state(document)
    migrated = copy.deepcopy(document)
    migrated["schema_version"] = EXPERIMENT_LIFECYCLE_SCHEMA_VERSION
    migrated["experiment_attempts"] = []
    migrated["experiment_promotions"] = []
    for effect in migrated["effects"]:
        effect["attempt_id"] = None
    validate_typed_state(migrated)
    return migrated


def rollback_v3alpha1_to_v2alpha1(document: dict[str, Any]) -> dict[str, Any]:
    """Permit rollback only before any Experiment lifecycle record exists."""
    if document.get("schema_version") == SCHEMA_VERSION:
        legacy = copy.deepcopy(document)
        validate_typed_state(legacy)
        return legacy
    if document.get("schema_version") != EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
        raise TypedStateError("unsupported rollback source")
    validate_typed_state(document)
    if (
        document["experiment_attempts"]
        or document["experiment_promotions"]
        or any(effect["attempt_id"] is not None for effect in document["effects"])
    ):
        raise TypedStateError("v3 lifecycle history prevents v2 rollback")
    legacy = copy.deepcopy(document)
    legacy["schema_version"] = SCHEMA_VERSION
    del legacy["experiment_attempts"]
    del legacy["experiment_promotions"]
    for effect in legacy["effects"]:
        del effect["attempt_id"]
    validate_typed_state(legacy)
    return legacy
