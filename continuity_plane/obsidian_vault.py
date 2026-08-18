"""Provider-neutral, read-only Obsidian projection for M9-06."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .external_state_provider import HMACExternalStateProjectionSigner

VAULT_SCHEMA_VERSION = "context.obsidian-vault/v1alpha1"
TEMPLATE_VERSION = VAULT_SCHEMA_VERSION
MANIFEST_NAME = ".continuity.json"
MAX_FILE_BYTES = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_GRAPH_SCHEMA = "context.project-graph-projection/v1alpha1"
_DECISION_SCHEMA = "context.decision-evidence-projection/v1alpha1"
_HEALTH_SCHEMA = "context.context-health-projection/v1alpha1"
_FILES = (
    ("00 Continuity Plane.md", "overview"),
    ("10 Project Graph.md", "project_graph"),
    ("20 Decisions and Evidence.md", "decision_evidence"),
    ("30 Context Health.md", "context_health"),
)
_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "approval_authority": False,
    "provider_native_authority": False,
    "external_effect_authority": False,
}
_SOURCE_KEYS = {
    "project_graph_projection_sha256",
    "decision_evidence_projection_sha256",
    "context_health_projection_sha256",
}
_SOURCE_AUTHORITIES = {
    _PROJECT_GRAPH_SCHEMA: {
        "state_write_authority": False,
        "controlled_action_authority": False,
        "provider_authority": 0,
        "external_effect_authority": 0,
    },
    _DECISION_SCHEMA: {
        "state_write_authority": False,
        "completion_authority": False,
        "approval_authority": False,
        "provider_authority": 0,
        "external_effect_authority": 0,
    },
    _HEALTH_SCHEMA: {
        "state_write_authority": False,
        "completion_authority": False,
        "approval_authority": False,
        "provider_authority": 0,
        "external_effect_authority": 0,
    },
}


class ObsidianVaultError(ValueError):
    """The generated vault or its source bindings are invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ObsidianVaultError(f"{field} is invalid")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ObsidianVaultError("generated_at is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObsidianVaultError("generated_at is invalid") from exc
    return value


def _projection(
    value: Any,
    *,
    name: str,
    schema_version: str,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ObsidianVaultError(f"{name} projection is invalid")
    if value.get("schema_version") != schema_version:
        raise ObsidianVaultError(f"{name} schema version is invalid")
    project_id = value.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ObsidianVaultError(f"{name} project_id is invalid")
    revision = value.get("state_revision")
    if type(revision) is not int or revision < 0:
        raise ObsidianVaultError(f"{name} state revision is invalid")
    _sha(value.get("state_sha256"), f"{name}.state_sha256")
    projection_sha256 = _sha(
        value.get("projection_sha256"), f"{name}.projection_sha256"
    )
    if value.get("authority") != _SOURCE_AUTHORITIES[schema_version]:
        raise ObsidianVaultError(f"{name} authority is invalid")
    unsigned = {
        key: item
        for key, item in value.items()
        if key not in {"projection_sha256", "signature"}
    }
    if projection_sha256 != _digest(unsigned):
        raise ObsidianVaultError(f"{name} projection digest is invalid")
    signed = {key: item for key, item in value.items() if key != "signature"}
    if not signer.verify(signed, value.get("signature")):
        raise ObsidianVaultError(f"{name} projection signature is invalid")
    return value


def _frontmatter(
    *,
    title: str,
    project_id: str,
    state_revision: int,
    source_projection_sha256: str,
) -> str:
    return "\n".join(
        (
            "---",
            f"title: {title}",
            f"project_id: {project_id}",
            f"state_revision: {state_revision}",
            f"source_projection_sha256: {source_projection_sha256}",
            "read_only: true",
            f"template_version: {TEMPLATE_VERSION}",
            "---",
            "",
        )
    )


def _markdown(
    *,
    title: str,
    project_id: str,
    state_revision: int,
    source_projection_sha256: str,
    body: Any,
) -> bytes:
    return (
        _frontmatter(
            title=title,
            project_id=project_id,
            state_revision=state_revision,
            source_projection_sha256=source_projection_sha256,
        )
        + f"# {title}\n\n"
        + "```json\n"
        + json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```\n"
    ).encode("utf-8")


def _validate_sources(
    project_graph: Any,
    decision_evidence: Any,
    context_health: Any,
    signer: HMACExternalStateProjectionSigner,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    graph = _projection(
        project_graph,
        name="project graph",
        schema_version=_PROJECT_GRAPH_SCHEMA,
        signer=signer,
    )
    decisions = _projection(
        decision_evidence,
        name="decision evidence",
        schema_version=_DECISION_SCHEMA,
        signer=signer,
    )
    health = _projection(
        context_health,
        name="context health",
        schema_version=_HEALTH_SCHEMA,
        signer=signer,
    )
    identities = {
        (item["project_id"], item["state_revision"], item["state_sha256"])
        for item in (graph, decisions, health)
    }
    if len(identities) != 1:
        raise ObsidianVaultError("source project, revision or state digest differs")
    if health.get("source_projection_sha256") != graph["source_projection_sha256"]:
        raise ObsidianVaultError("context health source binding differs")
    if (
        health.get("decision_evidence_projection_sha256")
        != decisions["projection_sha256"]
    ):
        raise ObsidianVaultError("context health decision binding differs")
    return graph, decisions, health


def _source_map(
    graph: dict[str, Any], decisions: dict[str, Any], health: dict[str, Any]
) -> dict[str, str]:
    return {
        "project_graph_projection_sha256": graph["projection_sha256"],
        "decision_evidence_projection_sha256": decisions["projection_sha256"],
        "context_health_projection_sha256": health["projection_sha256"],
    }


def _build_files(
    graph: dict[str, Any], decisions: dict[str, Any], health: dict[str, Any]
) -> list[dict[str, Any]]:
    project_id = graph["project_id"]
    revision = graph["state_revision"]
    bodies = {
        "overview": {
            "project_id": project_id,
            "state_revision": revision,
            "sources": _source_map(graph, decisions, health),
            "authority": _AUTHORITY,
        },
        "project_graph": {
            "graph": graph.get("graph", {}),
            "active_work_set": graph.get("active_work_set", []),
            "work_ledger": graph.get("work_ledger", {}),
            "health": graph.get("health", {}),
        },
        "decision_evidence": {
            "decision_timeline": decisions.get("decision_timeline", []),
            "constraint_matrix": decisions.get("constraint_matrix", []),
            "evidence_matrix": decisions.get("evidence_matrix", []),
            "health": decisions.get("health", {}),
        },
        "context_health": {
            "context_health": health.get("context_health", {}),
            "reference_health": health.get("reference_health", {}),
            "harness_health": health.get("harness_health", {}),
            "replay_health": health.get("replay_health", {}),
            "drilldowns": health.get("drilldowns", []),
            "overall_status": health.get("overall_status"),
        },
    }
    titles = {
        "overview": "Continuity Plane",
        "project_graph": "Project Graph",
        "decision_evidence": "Decisions and Evidence",
        "context_health": "Context Health",
    }
    source_by_kind = {
        "overview": graph["projection_sha256"],
        "project_graph": graph["projection_sha256"],
        "decision_evidence": decisions["projection_sha256"],
        "context_health": health["projection_sha256"],
    }
    files = []
    for path, kind in _FILES:
        content = _markdown(
            title=titles[kind],
            project_id=project_id,
            state_revision=revision,
            source_projection_sha256=source_by_kind[kind],
            body=bodies[kind],
        )
        if len(content) > MAX_FILE_BYTES:
            raise ObsidianVaultError("vault file size exceeds maximum bytes")
        files.append(
            {
                "path": path,
                "content": content.decode("utf-8"),
                "content_sha256": _bytes_digest(content),
                "source_projection_sha256": source_by_kind[kind],
                "utf8_bytes": len(content),
            }
        )
    return files


def build_obsidian_vault(
    *,
    project_graph: dict[str, Any],
    decision_evidence: dict[str, Any],
    context_health: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
    generated_at: str,
) -> dict[str, Any]:
    """Build a deterministic, zero-authority vault manifest."""
    graph, decisions, health = _validate_sources(
        project_graph, decision_evidence, context_health, signer
    )
    generated_at = _timestamp(generated_at)
    files = _build_files(graph, decisions, health)
    vault = {
        "schema_version": VAULT_SCHEMA_VERSION,
        "template_version": TEMPLATE_VERSION,
        "project_id": graph["project_id"],
        "state_revision": graph["state_revision"],
        "state_sha256": graph["state_sha256"],
        "generated_at": generated_at,
        "sources": _source_map(graph, decisions, health),
        "files": files,
        "authority": copy_authority(),
    }
    vault["vault_sha256"] = _digest(vault)
    vault["signature"] = signer.sign(vault)
    return vault


def copy_authority() -> dict[str, bool]:
    return dict(_AUTHORITY)


def _validate_vault_document(
    vault: Any,
    *,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    if not isinstance(vault, dict):
        raise ObsidianVaultError("vault manifest is invalid")
    required = {
        "schema_version",
        "template_version",
        "project_id",
        "state_revision",
        "state_sha256",
        "generated_at",
        "sources",
        "files",
        "authority",
        "vault_sha256",
        "signature",
    }
    if set(vault) != required:
        raise ObsidianVaultError("vault manifest fields are invalid")
    if (
        vault["schema_version"] != VAULT_SCHEMA_VERSION
        or vault["template_version"] != TEMPLATE_VERSION
    ):
        raise ObsidianVaultError("vault schema version is invalid")
    if not isinstance(vault["project_id"], str) or not vault["project_id"]:
        raise ObsidianVaultError("vault project_id is invalid")
    if type(vault["state_revision"]) is not int or vault["state_revision"] < 0:
        raise ObsidianVaultError("vault state revision is invalid")
    _sha(vault["state_sha256"], "vault.state_sha256")
    _timestamp(vault["generated_at"])
    if vault["authority"] != _AUTHORITY:
        raise ObsidianVaultError("vault authority is invalid")
    sources = vault["sources"]
    if not isinstance(sources, dict) or set(sources) != _SOURCE_KEYS:
        raise ObsidianVaultError("vault sources are invalid")
    for field, value in sources.items():
        _sha(value, f"sources.{field}")
    files = vault["files"]
    if not isinstance(files, list) or len(files) != len(_FILES):
        raise ObsidianVaultError("vault file list is invalid")
    expected_paths = [path for path, _ in _FILES]
    if [item.get("path") for item in files] != expected_paths:
        raise ObsidianVaultError("vault file set is invalid")
    for item in files:
        if set(item) != {
            "path",
            "content",
            "content_sha256",
            "source_projection_sha256",
            "utf8_bytes",
        }:
            raise ObsidianVaultError("vault file entry is invalid")
        if not isinstance(item["content"], str):
            raise ObsidianVaultError("vault file content is invalid")
        _sha(item["content_sha256"], "vault file digest")
        _sha(item["source_projection_sha256"], "vault file source digest")
        if (
            type(item["utf8_bytes"]) is not int
            or item["utf8_bytes"] <= 0
            or item["utf8_bytes"] > MAX_FILE_BYTES
        ):
            raise ObsidianVaultError("vault file size is invalid")
        content = item["content"].encode("utf-8")
        if (
            len(content) != item["utf8_bytes"]
            or _bytes_digest(content) != item["content_sha256"]
        ):
            raise ObsidianVaultError("vault file digest is invalid")
    vault_sha256 = _sha(vault["vault_sha256"], "vault_sha256")
    unsigned = {key: value for key, value in vault.items() if key != "vault_sha256"}
    unsigned = {key: value for key, value in unsigned.items() if key != "signature"}
    if vault_sha256 != _digest(unsigned):
        raise ObsidianVaultError("vault manifest digest is invalid")
    signed = {key: value for key, value in vault.items() if key != "signature"}
    if not signer.verify(signed, vault.get("signature")):
        raise ObsidianVaultError("vault manifest signature is invalid")
    return vault


def _rendered_bytes(vault: dict[str, Any], path: str) -> bytes:
    entry = next(item for item in vault["files"] if item["path"] == path)
    return entry["content"].encode("utf-8")


def write_obsidian_vault(
    root: str | Path,
    vault: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
) -> None:
    """Write a new vault only into an empty or absent directory."""
    vault = _validate_vault_document(vault, signer=signer)
    root = Path(root)
    if root.exists():
        if not root.is_dir():
            raise ObsidianVaultError("vault path is not a directory")
        if any(root.iterdir()):
            raise ObsidianVaultError("vault destination is non-empty")
    else:
        root.mkdir(parents=True, exist_ok=False)
    for entry in vault["files"]:
        path = root / entry["path"]
        content = _rendered_bytes(vault, entry["path"])
        if _bytes_digest(content) != entry["content_sha256"]:
            raise ObsidianVaultError("vault file digest is invalid")
        path.write_bytes(content)
    (root / MANIFEST_NAME).write_bytes(_canonical(vault))


def validate_obsidian_vault(
    root: str | Path,
    expected_vault: dict[str, Any] | None = None,
    *,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    """Validate the generated file set and return the immutable manifest."""
    root = Path(root)
    if not root.is_dir():
        raise ObsidianVaultError("vault path is not a directory")
    try:
        vault = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObsidianVaultError("vault manifest is unreadable") from exc
    _validate_vault_document(vault, signer=signer)
    if expected_vault is not None and vault != _validate_vault_document(
        expected_vault, signer=signer
    ):
        raise ObsidianVaultError("vault manifest differs from expected source")
    expected_files = {MANIFEST_NAME, *(entry["path"] for entry in vault["files"])}
    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    if actual_files != expected_files:
        raise ObsidianVaultError("vault file set contains unmanaged files")
    for entry in vault["files"]:
        content = (root / entry["path"]).read_bytes()
        if (
            len(content) != entry["utf8_bytes"]
            or _bytes_digest(content) != entry["content_sha256"]
        ):
            raise ObsidianVaultError("vault generated file digest differs")
    return vault


__all__ = [
    "MANIFEST_NAME",
    "MAX_FILE_BYTES",
    "TEMPLATE_VERSION",
    "VAULT_SCHEMA_VERSION",
    "ObsidianVaultError",
    "build_obsidian_vault",
    "validate_obsidian_vault",
    "write_obsidian_vault",
]
