"""M10-00 self-dogfood pilot plan and evidence admission contract."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .context_trace import LocalContextTraceEmitter, validate_context_trace_chain
from .dogfood_emitter import (
    DogfoodObservationEmitter,
    assess_dogfood_coverage,
    require_dogfood_coverage_gate,
    validate_dogfood_coverage,
    validate_dogfood_observation,
)
from .harness_run import HarnessCoordinator, create_harness_run, replay_harness_events

SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION = "context.self-dogfood-pilot-plan/v1alpha1"
SELF_DOGFOOD_EVIDENCE_MATRIX_SCHEMA_VERSION = (
    "context.self-dogfood-evidence-matrix/v1alpha1"
)
SELF_DOGFOOD_FAULT_DRILL_SCHEMA_VERSION = "context.self-dogfood-fault-drill/v1alpha1"
SELF_DOGFOOD_RELEASE_VERIFICATION_SCHEMA_VERSION = (
    "context.self-dogfood-release-verification/v1alpha1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_EVIDENCE_CLASSES = {
    "historical-live-manual",
    "offline-admitted-replay",
    "local-runtime-contract",
    "local-runtime-fault",
    "synthetic-contract",
}
_EVIDENCE_CLASS_BY_EXPERIMENT = {
    "E0": "historical-live-manual",
    "E1": "offline-admitted-replay",
    "E2": "local-runtime-contract",
    "E3": "synthetic-contract",
    "E4": "local-runtime-contract",
    "E5": "local-runtime-contract",
    "E6": "local-runtime-contract",
    "E7": "synthetic-contract",
    "E8": "local-runtime-contract",
    "E9": "local-runtime-fault",
}
_VETO_EXPERIMENTS = {"E1", "E2", "E4", "E6", "E8", "E9"}
_REQUIRED_FAULTS = {"compaction", "idea", "interrupt", "worker-loss"}
_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}
_RESERVED_PILOT_OUTPUTS = {
    "experiments/evidence/m10-00-self-dogfood-pilot-plan.json",
    "experiments/evidence/m10-00-self-dogfood-pilot-results.json",
    "experiments/evidence/m10-00-release-verification-results.json",
    "experiments/evidence/m10-00-campaign-verdict-results.json",
}
_NON_EXECUTION_PATHS = {
    "MASTER.md",
    "STATUS.md",
    "profiles/document-control-config.yaml",
    "profiles/document-control-manifest.yaml",
    "experiments/state/m0-10-document-lifecycle-results.yaml",
}


class SelfDogfoodPilotError(ValueError):
    """Raised when a self-dogfood plan is not replayable or current."""


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
        raise SelfDogfoodPilotError("pilot plan is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise SelfDogfoodPilotError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SelfDogfoodPilotError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SelfDogfoodPilotError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelfDogfoodPilotError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SelfDogfoodPilotError(f"{field} requires a timezone")
    return value


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SelfDogfoodPilotError("pilot repository identity is unavailable") from exc
    return result.stdout.strip()


def _repository_baseline(root: Path) -> dict[str, Any]:
    base_commit = _git(root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
        raise SelfDogfoodPilotError("repository base commit is invalid")
    branch = _git(root, "branch", "--show-current") or "detached"
    _id(branch, "repository branch")
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SelfDogfoodPilotError("repository file inventory is unavailable") from exc
    paths = sorted(
        path.decode("utf-8")
        for path in listing.split(b"\0")
        if path
        and path.decode("utf-8") not in _RESERVED_PILOT_OUTPUTS
        and path.decode("utf-8") not in _NON_EXECUTION_PATHS
        and not path.decode("utf-8").startswith("docs/")
    )
    inventory = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise SelfDogfoodPilotError("repository inventory contains a non-file")
        payload = path.read_bytes()
        inventory.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            }
        )
    registry_path = root / "schemas/registry.yaml"
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "base_commit": base_commit,
        "branch": branch,
        "dirty": bool(status),
        "worktree_file_count": len(inventory),
        "worktree_sha256": _digest(inventory),
        "schema_registry_sha256": hashlib.sha256(
            registry_path.read_bytes()
        ).hexdigest(),
    }


def _validate_repository_baseline(value: Any) -> None:
    expected = {
        "base_commit",
        "branch",
        "dirty",
        "worktree_file_count",
        "worktree_sha256",
        "schema_registry_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise SelfDogfoodPilotError("repository baseline fields are invalid")
    if re.fullmatch(r"[0-9a-f]{40}", value["base_commit"]) is None:
        raise SelfDogfoodPilotError("repository base commit is invalid")
    _id(value["branch"], "repository branch")
    if type(value["dirty"]) is not bool:
        raise SelfDogfoodPilotError("repository dirty status is invalid")
    if (
        type(value["worktree_file_count"]) is not int
        or value["worktree_file_count"] <= 0
    ):
        raise SelfDogfoodPilotError("repository file count is invalid")
    _sha(value["worktree_sha256"], "repository worktree_sha256")
    _sha(value["schema_registry_sha256"], "repository schema_registry_sha256")


def _relative_path(value: Any, field: str) -> str:
    value = _id(value, field)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise SelfDogfoodPilotError(f"{field} must be a repository-relative path")
    if value.startswith(("provider-archive/", "transcripts/", "raw/")):
        raise SelfDogfoodPilotError(f"{field} cannot reference a raw transcript")
    return path.as_posix()


def _validate_evidence_refs(
    references: Any,
    *,
    root: Path,
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(references, list) or not references:
        raise SelfDogfoodPilotError(f"{field} must contain evidence")
    normalized: list[dict[str, str]] = []
    for index, reference in enumerate(references):
        if isinstance(reference, str):
            path = _relative_path(reference, f"{field}[{index}].path")
            resolved = (root / path).resolve()
            if root not in resolved.parents or not resolved.is_file():
                raise SelfDogfoodPilotError(f"{field}[{index}] path is unavailable")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            normalized.append({"path": path, "content_sha256": digest})
            continue
        if not isinstance(reference, dict) or set(reference) != {
            "path",
            "content_sha256",
        }:
            raise SelfDogfoodPilotError(f"{field}[{index}] fields are invalid")
        path = _relative_path(reference["path"], f"{field}[{index}].path")
        digest = _sha(reference["content_sha256"], f"{field}[{index}].content_sha256")
        resolved = (root / path).resolve()
        if root not in resolved.parents or not resolved.is_file():
            raise SelfDogfoodPilotError(f"{field}[{index}] path is unavailable")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
            raise SelfDogfoodPilotError(f"{field}[{index}] evidence is stale")
        normalized.append({"path": path, "content_sha256": digest})
    if len({item["path"] for item in normalized}) != len(normalized):
        raise SelfDogfoodPilotError(f"{field} paths must be unique")
    return sorted(normalized, key=lambda item: item["path"])


def _normalize_leaves(leaves: Any) -> list[dict[str, Any]]:
    if not isinstance(leaves, list) or len(leaves) < 3:
        raise SelfDogfoodPilotError("pilot requires at least three required leaves")
    normalized: list[dict[str, Any]] = []
    for index, leaf in enumerate(leaves):
        if not isinstance(leaf, dict) or set(leaf) != {
            "work_id",
            "dependency_ids",
            "worker_ref",
            "verifier_ref",
            "completion_gate_id",
        }:
            raise SelfDogfoodPilotError(f"required_leaves[{index}] fields are invalid")
        work_id = _id(leaf["work_id"], f"required_leaves[{index}].work_id")
        dependencies = leaf["dependency_ids"]
        if (
            not isinstance(dependencies, list)
            or len(set(dependencies)) != len(dependencies)
            or any(not isinstance(item, str) for item in dependencies)
        ):
            raise SelfDogfoodPilotError("required leaf dependencies are invalid")
        normalized.append(
            {
                "work_id": work_id,
                "dependency_ids": sorted(dependencies),
                "worker_ref": _id(leaf["worker_ref"], "worker_ref"),
                "verifier_ref": _id(leaf["verifier_ref"], "verifier_ref"),
                "completion_gate_id": _id(
                    leaf["completion_gate_id"], "completion_gate_id"
                ),
            }
        )
    if len({item["work_id"] for item in normalized}) != len(normalized):
        raise SelfDogfoodPilotError("required leaf IDs must be unique")
    by_id = {item["work_id"]: item for item in normalized}
    if any(
        dependency not in by_id
        for item in normalized
        for dependency in item["dependency_ids"]
    ):
        raise SelfDogfoodPilotError("required leaf dependency is unknown")
    roots = [item for item in normalized if not item["dependency_ids"]]
    if len(roots) != 1:
        raise SelfDogfoodPilotError("required leaves must have one root")
    ordered: list[dict[str, Any]] = []
    previous: str | None = None
    remaining = set(by_id)
    while remaining:
        candidates = [
            item
            for item in normalized
            if item["work_id"] in remaining
            and item["dependency_ids"] == ([] if previous is None else [previous])
        ]
        if len(candidates) != 1:
            raise SelfDogfoodPilotError("required leaves must form one linear chain")
        current = candidates[0]
        ordered.append(current)
        previous = current["work_id"]
        remaining.remove(previous)
    return ordered


def _normalize_workers(workers: Any, *, verifier_ref: str) -> list[dict[str, str]]:
    if not isinstance(workers, list) or len(workers) < 2:
        raise SelfDogfoodPilotError("pilot requires at least two workers")
    normalized = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict) or set(worker) != {
            "worker_ref",
            "role",
            "provider_contract_version",
        }:
            raise SelfDogfoodPilotError(f"workers[{index}] fields are invalid")
        normalized.append(
            {
                "worker_ref": _id(worker["worker_ref"], "worker_ref"),
                "role": _id(worker["role"], "worker role"),
                "provider_contract_version": _id(
                    worker["provider_contract_version"],
                    "provider_contract_version",
                ),
            }
        )
    if len({item["worker_ref"] for item in normalized}) != len(normalized):
        raise SelfDogfoodPilotError("worker IDs must be unique")
    if verifier_ref in {item["worker_ref"] for item in normalized}:
        raise SelfDogfoodPilotError("verifier must be independent of workers")
    return sorted(normalized, key=lambda item: item["worker_ref"])


def _normalize_faults(fault_kinds: Any) -> list[dict[str, Any]]:
    if not isinstance(fault_kinds, list) or any(
        not isinstance(item, str) for item in fault_kinds
    ):
        raise SelfDogfoodPilotError("fault kinds are invalid")
    values = sorted(set(fault_kinds))
    if not _REQUIRED_FAULTS.issubset(values):
        raise SelfDogfoodPilotError("pilot is missing a required fault injection")
    return [
        {
            "fault_id": f"fault/m10-00/{fault_kind}",
            "fault_kind": fault_kind,
            "required": fault_kind in _REQUIRED_FAULTS,
            "expected_veto": fault_kind in {"compaction", "interrupt", "worker-loss"},
        }
        for fault_kind in values
    ]


def _normalize_evidence_matrix(
    evidence_paths: Any,
    *,
    root: Path,
) -> list[dict[str, Any]]:
    if not isinstance(evidence_paths, dict) or set(evidence_paths) != {
        f"E{index}" for index in range(10)
    }:
        raise SelfDogfoodPilotError("evidence matrix must contain E0-E9")
    matrix = []
    for experiment_id in sorted(evidence_paths, key=lambda value: int(value[1:])):
        references = _validate_evidence_refs(
            evidence_paths[experiment_id],
            root=root,
            field=f"evidence_matrix.{experiment_id}",
        )
        matrix.append(
            {
                "experiment_id": experiment_id,
                "evidence_class": _EVIDENCE_CLASS_BY_EXPERIMENT[experiment_id],
                "veto": experiment_id in _VETO_EXPERIMENTS,
                "applicability": "conditional"
                if experiment_id == "E8"
                else "applicable",
                "admission_status": "candidate",
                "evidence_refs": references,
                "limitation": (
                    "local profile does not prove shared-strong coordination"
                    if experiment_id == "E8"
                    else "pilot execution must produce a campaign-bound receipt"
                ),
            }
        )
    return matrix


def _validate_plan_shape(plan: Any) -> None:
    expected = {
        "schema_version",
        "plan_id",
        "campaign_id",
        "project_id",
        "state_revision",
        "created_at",
        "capability_profile",
        "repository_baseline",
        "required_leaves",
        "workers",
        "verifier",
        "fault_injections",
        "evidence_matrix",
        "authority",
        "plan_sha256",
    }
    if not isinstance(plan, dict) or set(plan) != expected:
        raise SelfDogfoodPilotError("pilot plan fields are invalid")
    if plan["schema_version"] != SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION:
        raise SelfDogfoodPilotError("pilot plan schema is unsupported")
    for field in ("plan_id", "campaign_id", "project_id", "capability_profile"):
        _id(plan[field], field)
    if plan["capability_profile"] != "local-embedded":
        raise SelfDogfoodPilotError("M10-00 must declare local-embedded profile")
    if type(plan["state_revision"]) is not int or plan["state_revision"] < 0:
        raise SelfDogfoodPilotError("state_revision is invalid")
    _timestamp(plan["created_at"], "created_at")
    _validate_repository_baseline(plan["repository_baseline"])
    if plan["authority"] != _AUTHORITY:
        raise SelfDogfoodPilotError("pilot plan authority must remain zero")
    _sha(plan["plan_sha256"], "plan_sha256")


def build_self_dogfood_pilot_plan(
    *,
    root: Path,
    plan_id: str,
    campaign_id: str,
    project_id: str,
    state_revision: int,
    created_at: str,
    required_leaves: list[dict[str, Any]],
    workers: list[dict[str, str]],
    verifier_ref: str,
    fault_kinds: list[str],
    evidence_paths: dict[str, list[str | dict[str, str]]],
) -> dict[str, Any]:
    """Build a revision-bound plan without granting runtime State authority."""
    root = root.resolve()
    normalized_verifier = _id(verifier_ref, "verifier_ref")
    plan: dict[str, Any] = {
        "schema_version": SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION,
        "plan_id": _id(plan_id, "plan_id"),
        "campaign_id": _id(campaign_id, "campaign_id"),
        "project_id": _id(project_id, "project_id"),
        "state_revision": state_revision,
        "created_at": created_at,
        "capability_profile": "local-embedded",
        "repository_baseline": _repository_baseline(root),
        "required_leaves": _normalize_leaves(required_leaves),
        "workers": _normalize_workers(workers, verifier_ref=normalized_verifier),
        "verifier": {
            "verifier_ref": normalized_verifier,
            "role": "independent-verifier",
            "independent_of_worker_refs": sorted(
                worker["worker_ref"] for worker in workers
            ),
        },
        "fault_injections": _normalize_faults(fault_kinds),
        "evidence_matrix": _normalize_evidence_matrix(evidence_paths, root=root),
        "authority": copy.deepcopy(_AUTHORITY),
        "plan_sha256": "",
    }
    plan["plan_sha256"] = _digest(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    validate_self_dogfood_pilot_plan(plan, root=root)
    return plan


def validate_self_dogfood_pilot_plan(plan: Any, *, root: Path) -> None:
    """Validate plan shape, current evidence bytes and its canonical digest."""
    _validate_plan_shape(plan)
    normalized_leaves = _normalize_leaves(plan["required_leaves"])
    if normalized_leaves != plan["required_leaves"]:
        raise SelfDogfoodPilotError("required leaves are not canonical")
    normalized_workers = _normalize_workers(
        plan["workers"], verifier_ref=plan["verifier"]["verifier_ref"]
    )
    if normalized_workers != plan["workers"]:
        raise SelfDogfoodPilotError("workers are not canonical")
    verifier = plan["verifier"]
    if not isinstance(verifier, dict) or set(verifier) != {
        "verifier_ref",
        "role",
        "independent_of_worker_refs",
    }:
        raise SelfDogfoodPilotError("verifier fields are invalid")
    if verifier["role"] != "independent-verifier" or verifier[
        "independent_of_worker_refs"
    ] != sorted(worker["worker_ref"] for worker in plan["workers"]):
        raise SelfDogfoodPilotError("verifier independence binding is invalid")
    normalized_faults = _normalize_faults(
        [item.get("fault_kind") for item in plan["fault_injections"]]
        if isinstance(plan["fault_injections"], list)
        else None
    )
    if normalized_faults != plan["fault_injections"]:
        raise SelfDogfoodPilotError("fault injections are not canonical")
    root = root.resolve()
    evidence_paths = (
        {
            item["experiment_id"]: item["evidence_refs"]
            for item in plan["evidence_matrix"]
        }
        if isinstance(plan["evidence_matrix"], list)
        else None
    )
    normalized_matrix = _normalize_evidence_matrix(evidence_paths, root=root)
    if normalized_matrix != plan["evidence_matrix"]:
        raise SelfDogfoodPilotError("evidence matrix is not current or canonical")
    expected_digest = _digest(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    if plan["plan_sha256"] != expected_digest:
        raise SelfDogfoodPilotError("pilot plan digest mismatch")


def build_evidence_matrix_receipt(
    plan: dict[str, Any],
    *,
    root: Path,
    observed_at: str,
) -> dict[str, Any]:
    """Admit current candidate evidence without declaring runtime gates passed."""
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    _timestamp(observed_at, "observed_at")
    entries = [
        {
            "experiment_id": entry["experiment_id"],
            "evidence_class": entry["evidence_class"],
            "veto": entry["veto"],
            "applicability": entry["applicability"],
            "admission_status": entry["admission_status"],
            "runtime_status": "pending",
            "evidence_refs": _validate_evidence_refs(
                entry["evidence_refs"],
                root=root,
                field=f"evidence_matrix.{entry['experiment_id']}",
            ),
            "limitation": entry["limitation"],
        }
        for entry in plan["evidence_matrix"]
    ]
    receipt: dict[str, Any] = {
        "schema_version": SELF_DOGFOOD_EVIDENCE_MATRIX_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "campaign_id": plan["campaign_id"],
        "project_id": plan["project_id"],
        "state_revision": plan["state_revision"],
        "observed_at": observed_at,
        "entries": entries,
        "admitted_count": len(entries),
        "pending_count": len(entries),
        "completion_status": "ready-for-runtime",
        "authority": copy.deepcopy(_AUTHORITY),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    validate_evidence_matrix_receipt(receipt, plan=plan, root=root)
    return receipt


def validate_evidence_matrix_receipt(
    receipt: Any,
    *,
    plan: dict[str, Any],
    root: Path,
) -> None:
    """Validate the admission receipt against the still-current plan and files."""
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    expected_fields = {
        "schema_version",
        "plan_id",
        "plan_sha256",
        "campaign_id",
        "project_id",
        "state_revision",
        "observed_at",
        "entries",
        "admitted_count",
        "pending_count",
        "completion_status",
        "authority",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise SelfDogfoodPilotError("evidence matrix receipt fields are invalid")
    if (
        receipt["schema_version"] != SELF_DOGFOOD_EVIDENCE_MATRIX_SCHEMA_VERSION
        or receipt["plan_id"] != plan["plan_id"]
        or receipt["plan_sha256"] != plan["plan_sha256"]
        or receipt["campaign_id"] != plan["campaign_id"]
        or receipt["project_id"] != plan["project_id"]
        or receipt["state_revision"] != plan["state_revision"]
        or receipt["completion_status"] != "ready-for-runtime"
    ):
        raise SelfDogfoodPilotError("evidence matrix binding is invalid")
    _timestamp(receipt["observed_at"], "observed_at")
    if receipt["authority"] != _AUTHORITY:
        raise SelfDogfoodPilotError("evidence matrix authority must remain zero")
    if receipt["entries"] != [
        {
            **entry,
            "runtime_status": "pending",
        }
        for entry in plan["evidence_matrix"]
    ]:
        raise SelfDogfoodPilotError("evidence matrix entries are not current")
    if receipt["admitted_count"] != 10 or receipt["pending_count"] != 10:
        raise SelfDogfoodPilotError("evidence matrix counts are invalid")
    expected_digest = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt["receipt_sha256"] != expected_digest:
        raise SelfDogfoodPilotError("evidence matrix receipt digest mismatch")


def _fault_harness_run(
    *,
    run_id: str,
    claim_id: str,
    state_revision: int,
    observed_at: str,
    parent_run_id: str | None,
    status: str,
) -> dict[str, Any]:
    return create_harness_run(
        run_id=run_id,
        project_id="context-control-plane",
        task_id="M10-00-fault-drill",
        task_revision=state_revision,
        claim_id=claim_id,
        claim_lease_epoch=1,
        claim_fence=1,
        provider="codex",
        provider_contract_version="provider-neutral/v1",
        execution_packet_sha256="a" * 64,
        skill_set_digest="b" * 64,
        tool_grants=["repo.read", "state.read"],
        checkpoint_id="checkpoint-m10-00-fault-drill",
        effect_high_watermark=0,
        verification_profile_id="profile-m10-00-local",
        reference_validity_watermark=0,
        trace_id=f"trace-{run_id}",
        status=status,
        parent_run_id=parent_run_id,
        scope_refs=[
            {
                "scope_kind": "file",
                "scope_ref": "context_control_plane/self_dogfood_pilot.py",
            }
        ],
        created_at=observed_at,
        updated_at=observed_at,
    )


def run_self_dogfood_fault_drill(
    plan: dict[str, Any],
    matrix_receipt: dict[str, Any],
    *,
    root: Path,
    observed_at: str,
) -> dict[str, Any]:
    """Run the local four-fault drill through trace and Harness contracts."""
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    validate_evidence_matrix_receipt(matrix_receipt, plan=plan, root=root)
    if matrix_receipt["completion_status"] != "ready-for-runtime":
        raise SelfDogfoodPilotError("fault drill requires admitted evidence matrix")
    _timestamp(observed_at, "observed_at")

    coordinator = HarnessCoordinator(
        project_id=plan["project_id"],
        task_revision=plan["state_revision"],
        provider_contract_version="provider-neutral/v1",
        clock=lambda: observed_at,
    )
    parent_id = "m10-00-fault-parent"
    analysis_id = "m10-00-worker-analysis"
    execution_id = "m10-00-worker-execution"
    verifier_id = "m10-00-verifier"
    coordinator.register_run(
        _fault_harness_run(
            run_id=parent_id,
            claim_id="claim-m10-00-parent",
            state_revision=plan["state_revision"],
            observed_at=observed_at,
            parent_run_id=None,
            status="running",
        )
    )
    coordinator.dispatch_worker(
        parent_id,
        _fault_harness_run(
            run_id=analysis_id,
            claim_id="claim-m10-00-analysis",
            state_revision=plan["state_revision"],
            observed_at=observed_at,
            parent_run_id=parent_id,
            status="running",
        ),
    )
    coordinator.dispatch_worker(
        parent_id,
        _fault_harness_run(
            run_id=execution_id,
            claim_id="claim-m10-00-execution",
            state_revision=plan["state_revision"],
            observed_at=observed_at,
            parent_run_id=parent_id,
            status="running",
        ),
    )
    coordinator.register_run(
        _fault_harness_run(
            run_id=verifier_id,
            claim_id="claim-m10-00-verifier",
            state_revision=plan["state_revision"],
            observed_at=observed_at,
            parent_run_id=None,
            status="proposed",
        )
    )
    coordinator.complete_worker(analysis_id, evidence_sha256="c" * 64)
    authority_before_loss = coordinator.authority_snapshot()
    coordinator.mark_worker_lost(execution_id)
    authority_after_loss = coordinator.authority_snapshot()
    handoff = coordinator.create_handoff(
        execution_id,
        verifier_id,
        checkpoint_id="checkpoint-m10-00-fault-drill",
        next_action="verify fault drill",
        expected_task_revision=plan["state_revision"],
    )
    accepted_handoff = coordinator.acknowledge_handoff(
        handoff["handoff_id"], "verify fault drill"
    )
    coordinator.fan_in(parent_id, sorted([analysis_id, execution_id]))
    harness_events = list(coordinator.events)
    harness_replay = replay_harness_events(harness_events, provider="codex")

    emitter = LocalContextTraceEmitter(
        binding={
            "project_id": plan["project_id"],
            "state_revision": plan["state_revision"],
            "active_work_id": "M10-00-fault-drill",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "run_id": parent_id,
            "operation_id": "operation-m10-00-fault-drill",
            "correlation_id": "correlation-m10-00",
        },
        source={"kind": "component", "component": "self-dogfood-pilot"},
    )
    dogfood = DogfoodObservationEmitter(emitter)
    refs = ["artifact://m10-00/pilot-plan", "artifact://m10-00/evidence-matrix"]
    observations = [
        dogfood.emit(
            event_kind="input-routing",
            subject_id="fault/idea",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route="capture-and-continue",
            interrupted=False,
            candidate_only=True,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="input-routing",
            subject_id="fault/interrupt",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route="interrupt",
            interrupted=True,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="compaction",
            subject_id="fault/compaction",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=1,
            first_side_effect_sequence=2,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="skill-selection",
            subject_id="fault/skill-selection",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="plan-revision",
            subject_id="plan/m10-00",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="agent-dispatch",
            subject_id="worker-execution",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="agent-handoff",
            subject_id="worker-execution",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
        dogfood.emit(
            event_kind="delivery",
            subject_id="M10-00-release-verification",
            active_work_before="M10-00-fault-drill",
            active_work_after="M10-00-fault-drill",
            return_point_work_id="M10-00-fault-drill",
            route=None,
            interrupted=False,
            candidate_only=False,
            acknowledged_input_replayed=False,
            first_action_match=True,
            target_revision=plan["state_revision"],
            canary_sequence=None,
            first_side_effect_sequence=None,
            provider_metrics_status="unavailable",
            evidence_refs=refs,
            observed_at=observed_at,
        ),
    ]
    expectations = {
        "eligible_ingress_ids": ["fault/idea", "fault/interrupt"],
        "visible_compaction_ids": ["fault/compaction"],
        "skill_selection_ids": ["fault/skill-selection"],
        "target_revisions": [plan["state_revision"]],
        "agent_dispatch_ids": ["worker-execution"],
        "delivery_ids": ["M10-00-release-verification"],
    }
    coverage = assess_dogfood_coverage(observations, expectations=expectations)
    try:
        require_dogfood_coverage_gate(coverage)
    except Exception as exc:
        raise SelfDogfoodPilotError("fault drill dogfood coverage regressed") from exc
    validate_context_trace_chain(list(emitter.events))

    faults = [
        {
            "fault_id": "fault/m10-00/compaction",
            "fault_kind": "compaction",
            "status": "passed",
            "observation_id": "dogfood/compaction/fault/compaction",
            "canary_before_first_side_effect": True,
            "first_action_match": True,
            "acknowledged_input_replayed": False,
        },
        {
            "fault_id": "fault/m10-00/idea",
            "fault_kind": "idea",
            "status": "passed",
            "observation_id": "dogfood/input-routing/fault/idea",
            "active_work_preserved": True,
            "candidate_only": True,
        },
        {
            "fault_id": "fault/m10-00/interrupt",
            "fault_kind": "interrupt",
            "status": "passed",
            "observation_id": "dogfood/input-routing/fault/interrupt",
            "active_work_preserved": True,
            "return_point_preserved": True,
        },
        {
            "fault_id": "fault/m10-00/worker-loss",
            "fault_kind": "worker-loss",
            "status": "passed",
            "harness_event_type": "worker-loss",
            "authority_unchanged": authority_before_loss == authority_after_loss,
            "handoff_accepted": accepted_handoff["status"] == "accepted",
        },
    ]
    receipt: dict[str, Any] = {
        "schema_version": SELF_DOGFOOD_FAULT_DRILL_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "campaign_id": plan["campaign_id"],
        "project_id": plan["project_id"],
        "state_revision": plan["state_revision"],
        "observed_at": observed_at,
        "plan_sha256": plan["plan_sha256"],
        "matrix_receipt_sha256": matrix_receipt["receipt_sha256"],
        "worker_count": 2,
        "verifier_ref": plan["verifier"]["verifier_ref"],
        "faults": faults,
        "observations": observations,
        "trace_events": list(emitter.events),
        "coverage": coverage,
        "harness": {
            **harness_replay,
            "worker_loss_authority_unchanged": authority_before_loss
            == authority_after_loss,
            "handoff_status": accepted_handoff["status"],
            "worker_ids": [analysis_id, execution_id],
        },
        "authority": copy.deepcopy(_AUTHORITY),
        "status": "passed",
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return receipt


def validate_self_dogfood_fault_drill(
    receipt: Any,
    *,
    plan: dict[str, Any],
    matrix_receipt: dict[str, Any],
    root: Path,
) -> None:
    """Revalidate the local fault drill without trusting its claimed counters."""
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    validate_evidence_matrix_receipt(matrix_receipt, plan=plan, root=root)
    fields = {
        "schema_version",
        "plan_id",
        "campaign_id",
        "project_id",
        "state_revision",
        "observed_at",
        "plan_sha256",
        "matrix_receipt_sha256",
        "worker_count",
        "verifier_ref",
        "faults",
        "observations",
        "trace_events",
        "coverage",
        "harness",
        "authority",
        "status",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise SelfDogfoodPilotError("fault drill receipt fields are invalid")
    if (
        receipt["schema_version"] != SELF_DOGFOOD_FAULT_DRILL_SCHEMA_VERSION
        or receipt["plan_id"] != plan["plan_id"]
        or receipt["campaign_id"] != plan["campaign_id"]
        or receipt["project_id"] != plan["project_id"]
        or receipt["state_revision"] != plan["state_revision"]
        or receipt["plan_sha256"] != plan["plan_sha256"]
        or receipt["matrix_receipt_sha256"] != matrix_receipt["receipt_sha256"]
        or receipt["worker_count"] != 2
        or receipt["verifier_ref"] != plan["verifier"]["verifier_ref"]
        or receipt["status"] != "passed"
        or receipt["authority"] != _AUTHORITY
    ):
        raise SelfDogfoodPilotError("fault drill binding or authority is invalid")
    _timestamp(receipt["observed_at"], "observed_at")
    if [fault["fault_kind"] for fault in receipt["faults"]] != [
        "compaction",
        "idea",
        "interrupt",
        "worker-loss",
    ] or any(fault.get("status") != "passed" for fault in receipt["faults"]):
        raise SelfDogfoodPilotError("fault drill fault results are incomplete")
    for observation in receipt["observations"]:
        validate_dogfood_observation(observation)
    validate_dogfood_coverage(receipt["coverage"])
    if receipt["coverage"]["status"] != "pass":
        raise SelfDogfoodPilotError("fault drill coverage is not passing")
    validate_context_trace_chain(receipt["trace_events"])
    harness = receipt["harness"]
    if (
        harness.get("worker_loss_authority_unchanged") is not True
        or harness.get("handoff_status") != "accepted"
        or harness.get("worker_ids")
        != [
            "m10-00-worker-analysis",
            "m10-00-worker-execution",
        ]
        or harness.get("authority_source") != "state-mcp"
    ):
        raise SelfDogfoodPilotError("fault drill harness evidence is invalid")
    if receipt["receipt_sha256"] != _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise SelfDogfoodPilotError("fault drill receipt digest mismatch")


def build_repository_verification_receipt(
    plan: dict[str, Any],
    fault_receipt: dict[str, Any],
    *,
    root: Path,
    command: list[str],
    passed: int,
    skipped: int,
    wall_time_seconds: float,
    output: str,
    observed_at: str,
) -> dict[str, Any]:
    """Bind one successful full-suite run to the pilot end worktree."""
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    if not isinstance(fault_receipt, dict) or fault_receipt.get("status") != "passed":
        raise SelfDogfoodPilotError(
            "release verification requires passed fault receipt"
        )
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 64
        or any(
            not isinstance(item, str) or not item or len(item) > 1024
            for item in command
        )
    ):
        raise SelfDogfoodPilotError("verification command is invalid")
    if type(passed) is not int or passed < 3:
        raise SelfDogfoodPilotError("full suite did not run enough tests")
    if type(skipped) is not int or skipped < 0:
        raise SelfDogfoodPilotError("full suite skipped count is invalid")
    if (
        type(wall_time_seconds) not in {int, float}
        or not math.isfinite(wall_time_seconds)
        or wall_time_seconds <= 0
    ):
        raise SelfDogfoodPilotError("full suite wall time is invalid")
    if (
        not isinstance(output, str)
        or not output
        or len(output.encode("utf-8")) > 8 * 1024 * 1024
    ):
        raise SelfDogfoodPilotError("full suite output is invalid")
    _timestamp(observed_at, "observed_at")
    receipt: dict[str, Any] = {
        "schema_version": SELF_DOGFOOD_RELEASE_VERIFICATION_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "campaign_id": plan["campaign_id"],
        "project_id": plan["project_id"],
        "state_revision": plan["state_revision"],
        "observed_at": observed_at,
        "plan_sha256": plan["plan_sha256"],
        "fault_receipt_sha256": fault_receipt["receipt_sha256"],
        "required_leaf_ids": [leaf["work_id"] for leaf in plan["required_leaves"]],
        "full_suite": {
            "command": copy.deepcopy(command),
            "command_sha256": _digest(command),
            "tests_run": passed,
            "passed": passed,
            "failures": 0,
            "errors": 0,
            "skipped": skipped,
            "wall_time_seconds": round(float(wall_time_seconds), 6),
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        },
        "start_repository": copy.deepcopy(plan["repository_baseline"]),
        "end_repository": _repository_baseline(root),
        "authority": copy.deepcopy(_AUTHORITY),
        "status": "passed",
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    validate_repository_verification_receipt(
        receipt,
        plan=plan,
        fault_receipt=fault_receipt,
        root=root,
    )
    return receipt


def validate_repository_verification_receipt(
    receipt: Any,
    *,
    plan: dict[str, Any],
    fault_receipt: dict[str, Any],
    root: Path,
) -> None:
    root = root.resolve()
    validate_self_dogfood_pilot_plan(plan, root=root)
    fields = {
        "schema_version",
        "plan_id",
        "campaign_id",
        "project_id",
        "state_revision",
        "observed_at",
        "plan_sha256",
        "fault_receipt_sha256",
        "required_leaf_ids",
        "full_suite",
        "start_repository",
        "end_repository",
        "authority",
        "status",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise SelfDogfoodPilotError("release verification receipt fields are invalid")
    if (
        receipt["schema_version"] != SELF_DOGFOOD_RELEASE_VERIFICATION_SCHEMA_VERSION
        or receipt["plan_id"] != plan["plan_id"]
        or receipt["campaign_id"] != plan["campaign_id"]
        or receipt["project_id"] != plan["project_id"]
        or receipt["state_revision"] != plan["state_revision"]
        or receipt["plan_sha256"] != plan["plan_sha256"]
        or receipt["fault_receipt_sha256"] != fault_receipt.get("receipt_sha256")
        or receipt["required_leaf_ids"]
        != [leaf["work_id"] for leaf in plan["required_leaves"]]
        or receipt["start_repository"] != plan["repository_baseline"]
        or receipt["authority"] != _AUTHORITY
        or receipt["status"] != "passed"
    ):
        raise SelfDogfoodPilotError("release verification binding is invalid")
    _timestamp(receipt["observed_at"], "observed_at")
    suite = receipt["full_suite"]
    suite_fields = {
        "command",
        "command_sha256",
        "tests_run",
        "passed",
        "failures",
        "errors",
        "skipped",
        "wall_time_seconds",
        "output_sha256",
    }
    if not isinstance(suite, dict) or set(suite) != suite_fields:
        raise SelfDogfoodPilotError("full suite receipt fields are invalid")
    if (
        suite["command_sha256"] != _digest(suite["command"])
        or suite["tests_run"] != suite["passed"]
        or type(suite["passed"]) is not int
        or suite["passed"] < 3
        or suite["failures"] != 0
        or suite["errors"] != 0
        or type(suite["skipped"]) is not int
        or suite["skipped"] < 0
    ):
        raise SelfDogfoodPilotError("full suite did not pass")
    _sha(suite["output_sha256"], "full suite output_sha256")
    _validate_repository_baseline(receipt["end_repository"])
    if receipt["end_repository"] != _repository_baseline(root):
        raise SelfDogfoodPilotError("release verification end worktree is stale")
    if receipt["receipt_sha256"] != _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise SelfDogfoodPilotError("release verification receipt digest mismatch")


__all__ = [
    "SELF_DOGFOOD_EVIDENCE_MATRIX_SCHEMA_VERSION",
    "SELF_DOGFOOD_FAULT_DRILL_SCHEMA_VERSION",
    "SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION",
    "SELF_DOGFOOD_RELEASE_VERIFICATION_SCHEMA_VERSION",
    "SelfDogfoodPilotError",
    "build_evidence_matrix_receipt",
    "build_repository_verification_receipt",
    "build_self_dogfood_pilot_plan",
    "run_self_dogfood_fault_drill",
    "validate_evidence_matrix_receipt",
    "validate_repository_verification_receipt",
    "validate_self_dogfood_fault_drill",
    "validate_self_dogfood_pilot_plan",
]
