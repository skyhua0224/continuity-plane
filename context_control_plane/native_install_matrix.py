"""Provider-neutral M10-09 native installation matrix receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


SCHEMA_VERSION = "context.native-install-matrix/v1alpha1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SYSTEMS = {"Linux", "Darwin", "Windows"}
_STATUSES = {"passed", "blocked", "failed", "unavailable"}
_STEP_NAMES = ("install", "verify", "export", "import", "rollback", "uninstall")


class NativeInstallMatrixError(ValueError):
    """Raised when a native installation receipt is incomplete or forged."""


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
        raise NativeInstallMatrixError("receipt is not canonical JSON") from exc


def _digest(receipt: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _sha(value: Any, field: str) -> None:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise NativeInstallMatrixError(f"{field} must be a lowercase SHA-256")


def _validate_step(name: str, step: Any) -> None:
    if not isinstance(step, dict) or set(step) != {"status", "duration_ms", "output_sha256"}:
        raise NativeInstallMatrixError(f"step {name} fields are invalid")
    if step["status"] not in _STATUSES:
        raise NativeInstallMatrixError(f"step {name} status is invalid")
    if (
        type(step["duration_ms"]) not in {int, float}
        or isinstance(step["duration_ms"], bool)
        or not math.isfinite(float(step["duration_ms"]))
        or step["duration_ms"] < 0
    ):
        raise NativeInstallMatrixError(f"step {name} duration is invalid")
    _sha(step["output_sha256"], f"step {name}.output_sha256")


def _validate_common(receipt: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "project_id",
        "generated_at",
        "platform",
        "package",
        "profile",
        "steps",
        "verdict",
        "consistency",
        "receipt_sha256",
    }
    if set(receipt) != required:
        raise NativeInstallMatrixError("receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise NativeInstallMatrixError("receipt schema_version is unsupported")
    if not isinstance(receipt["project_id"], str) or _ID_RE.fullmatch(receipt["project_id"]) is None:
        raise NativeInstallMatrixError("project_id is invalid")
    if not isinstance(receipt["generated_at"], str) or not receipt["generated_at"]:
        raise NativeInstallMatrixError("generated_at is invalid")

    platform = receipt["platform"]
    if not isinstance(platform, dict) or set(platform) != {"system", "release", "machine", "python"}:
        raise NativeInstallMatrixError("platform fields are invalid")
    if platform["system"] not in _SYSTEMS:
        raise NativeInstallMatrixError("platform system is unsupported")
    for field in ("release", "machine", "python"):
        if not isinstance(platform[field], str) or not platform[field]:
            raise NativeInstallMatrixError(f"platform {field} is invalid")

    package = receipt["package"]
    if not isinstance(package, dict) or set(package) != {"name", "version", "artifact_sha256"}:
        raise NativeInstallMatrixError("package fields are invalid")
    if not isinstance(package["name"], str) or not package["name"]:
        raise NativeInstallMatrixError("package name is invalid")
    if not isinstance(package["version"], str) or not package["version"]:
        raise NativeInstallMatrixError("package version is invalid")
    _sha(package["artifact_sha256"], "package.artifact_sha256")

    profile = receipt["profile"]
    if not isinstance(profile, dict) or set(profile) != {
        "requested_profile",
        "state_adapter",
        "external_services_required",
        "admin_required",
        "container_required",
    }:
        raise NativeInstallMatrixError("profile fields are invalid")
    if profile["requested_profile"] != "local-embedded":
        raise NativeInstallMatrixError("only local-embedded is supported by this matrix")
    if profile["state_adapter"] != "sqlite":
        raise NativeInstallMatrixError("local-embedded must use sqlite")
    if profile["external_services_required"] != 0:
        raise NativeInstallMatrixError("local-embedded cannot require external services")
    for field in ("admin_required", "container_required"):
        if type(profile[field]) is not bool or profile[field]:
            raise NativeInstallMatrixError(f"local-embedded {field} must be false")

    steps = receipt["steps"]
    if not isinstance(steps, dict) or set(steps) != set(_STEP_NAMES):
        raise NativeInstallMatrixError("installation steps are invalid")
    for name in _STEP_NAMES:
        _validate_step(name, steps[name])

    consistency = receipt["consistency"]
    if not isinstance(consistency, dict) or set(consistency) != {
        "state_write_authority",
        "external_service_invocations",
        "rollback_hash_matches_export",
        "artifact_leaks",
    }:
        raise NativeInstallMatrixError("consistency fields are invalid")
    if consistency["state_write_authority"] is not False:
        raise NativeInstallMatrixError("native matrix has State write authority")
    if consistency["external_service_invocations"] != 0:
        raise NativeInstallMatrixError("native matrix invoked an external service")
    if consistency["artifact_leaks"] != 0:
        raise NativeInstallMatrixError("native matrix artifact leak is non-zero")
    if type(consistency["rollback_hash_matches_export"]) is not bool:
        raise NativeInstallMatrixError("rollback consistency is invalid")
    if not isinstance(receipt["verdict"], str) or receipt["verdict"] not in _STATUSES:
        raise NativeInstallMatrixError("receipt verdict is invalid")
    _sha(receipt["receipt_sha256"], "receipt_sha256")


def _expected_verdict(steps: dict[str, dict[str, Any]]) -> str:
    statuses = {step["status"] for step in steps.values()}
    if "failed" in statuses:
        return "failed"
    if statuses & {"blocked", "unavailable"}:
        return "blocked"
    return "passed"


def validate_native_install_matrix_receipt(receipt: dict[str, Any]) -> None:
    """Validate a native matrix receipt and all derived safety fields."""
    if not isinstance(receipt, dict):
        raise NativeInstallMatrixError("receipt must be an object")
    _validate_common(receipt)
    steps = receipt["steps"]
    rollback_matches = steps["rollback"]["output_sha256"] == steps["export"]["output_sha256"]
    if receipt["consistency"]["rollback_hash_matches_export"] != rollback_matches:
        raise NativeInstallMatrixError("rollback hash consistency is invalid")
    if not rollback_matches:
        raise NativeInstallMatrixError("rollback hash does not match export")
    if steps["install"]["status"] == "failed" and steps["uninstall"]["status"] == "passed":
        raise NativeInstallMatrixError("uninstall cannot pass after failed install")
    if receipt["receipt_sha256"] != _digest(receipt):
        raise NativeInstallMatrixError("receipt digest is invalid")
    if receipt["verdict"] != _expected_verdict(steps):
        raise NativeInstallMatrixError("receipt verdict is invalid")


def build_native_install_matrix_receipt(
    *,
    project_id: str,
    requested_profile: str,
    platform: dict[str, str],
    package: dict[str, str],
    steps: dict[str, dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """Build and validate one deterministic native install receipt."""
    if requested_profile != "local-embedded":
        raise NativeInstallMatrixError("only local-embedded is supported by this matrix")
    if not isinstance(steps, dict) or set(steps) != set(_STEP_NAMES):
        raise NativeInstallMatrixError("installation steps are invalid")
    if steps["install"].get("status") == "failed" and steps["uninstall"].get("status") == "passed":
        raise NativeInstallMatrixError("uninstall cannot pass after failed install")
    rollback_matches = steps["rollback"].get("output_sha256") == steps["export"].get("output_sha256")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": generated_at,
        "platform": platform,
        "package": package,
        "profile": {
            "requested_profile": requested_profile,
            "state_adapter": "sqlite",
            "external_services_required": 0,
            "admin_required": False,
            "container_required": False,
        },
        "steps": steps,
        "verdict": _expected_verdict(steps),
        "consistency": {
            "state_write_authority": False,
            "external_service_invocations": 0,
            "rollback_hash_matches_export": rollback_matches,
            "artifact_leaks": 0,
        },
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    validate_native_install_matrix_receipt(receipt)
    return receipt


__all__ = [
    "NativeInstallMatrixError",
    "SCHEMA_VERSION",
    "build_native_install_matrix_receipt",
    "validate_native_install_matrix_receipt",
]
