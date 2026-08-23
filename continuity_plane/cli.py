"""Release-neutral command line interface."""

from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .artifact_store import ArtifactRef, LocalArtifactStore
from .checkpoint import (
    CheckpointError,
    CheckpointStaleError,
    publish_checkpoint,
    restore_checkpoint,
)
from .local_state_bundle import (
    LocalStateBundleError,
    export_local_state,
    import_local_state,
    rollback_local_state,
)
from .recovery_envelope import (
    RecoveryEnvelopeError,
    compose_recovery_envelope,
    load_interaction_cursor,
    load_recovery_skill_lock,
)
from .route_apply import apply_route
from .status_projection import render_status_projection
from .sticky_router import canonical_route_decision_bytes, route_task_input
from .sqlite_state_store import SQLiteStateStore
from .canonical_attach import (
    CanonicalAttachError,
    build_attach_proposal,
    validate_attach_proposal,
)
from .state_mcp import (
    CLAIM_TOOL,
    COMMIT_TOOL,
    LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_COMPLETION_TOOL,
    LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_ACTIVATION_TOOL,
    LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_TRANSITION_TOOL,
    LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
    LOCAL_CLAIM_RECOVERY_TOOL,
    READ_TOOL,
    RequestContext,
    StateMCPService,
)

VERSION = "0.1.0a6"
_PROJECT_FIELDS = {
    "schema_version",
    "project_id",
    "display_name",
    "runtime_profile",
    "state_store",
    "collaboration",
    "governance",
    "authority",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_STATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _template(name: str) -> str:
    return (
        resources.files("continuity_plane.templates")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _project_file(root: Path) -> Path:
    return root / ".continuity" / "project.yaml"


def _load_project(root: Path) -> dict[str, Any]:
    path = _project_file(root)
    if not path.is_file():
        raise FileNotFoundError(f"project profile is missing: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != _PROJECT_FIELDS:
        raise ValueError("project profile fields are invalid")
    if document["schema_version"] != "context.project/v1alpha1":
        raise ValueError("project profile schema is unsupported")
    if (
        not isinstance(document["project_id"], str)
        or _ID_RE.fullmatch(document["project_id"]) is None
    ):
        raise ValueError("project_id is invalid")
    if document["runtime_profile"] != "local-embedded":
        raise ValueError("public default profile must be local-embedded")
    if document["state_store"] != {
        "adapter": "sqlite",
        "path": ".continuity/state.sqlite3",
    }:
        raise ValueError("default state store must be local SQLite")
    return document


def _state_file(root: Path, project: dict[str, Any]) -> Path:
    return root / project["state_store"]["path"]


def _open_state_store(root: Path, project: dict[str, Any]) -> SQLiteStateStore:
    path = _state_file(root, project)
    if not path.is_file():
        raise FileNotFoundError(f"state database is missing: {path}")
    store = SQLiteStateStore(path)
    store.initialize()
    return store


def _initial_state(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": "context.typed-state/v1alpha1",
        "project": {
            "project_id": project_id,
            "revision": 0,
            "governance_ref": ".continuity/MASTER.md@version-1",
            "active_work_ids": [],
            "primary_work_id": None,
            "current_decision_ids": [],
            "active_constraint_ids": [],
            "open_blocker_ids": [],
            "effect_high_watermark": 0,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "works": [
            {
                "work_id": "work-initial",
                "kind": "work",
                "title": "Define the first measurable outcome",
                "status": "proposed",
                "parent_work_id": None,
                "dependency_ids": [],
                "owner_refs": [],
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "project-governance"}
                ],
                "overlap_candidate_ids": [],
                "dedupe_status": "clear",
                "supersedes_work_id": None,
                "evidence_ids": [],
                "blocker_ids": [],
                "revision": 0,
            }
        ],
        "claims": [],
        "ideas": [],
        "decisions": [],
        "constraints": [],
        "evidence": [],
        "blockers": [],
        "effects": [],
    }


class _LocalCliAuthorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return action == "state.read" and context.authorization_ref == "local-cli"


class _LocalAttachAuthorizer:
    """Allow only an explicit local attach approval to commit and claim."""

    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return context.authorization_ref == "local-attach-approved" and action in {
            "state.read",
            "state.commit",
            "state.claim",
        }


class _LocalWorkflowAuthorizer:
    """Allow an explicit local workflow transition through State MCP."""

    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return (
            context.authorization_ref == "local-workflow-approved"
            and action
            in {
                "state.read",
                "state.commit",
                "state.claim",
                "state.work.complete",
                "state.work.activate",
                "state.work.transition",
                "state.claim.recovery",
                "state.route.apply",
            }
        )


def _state_service(
    store: SQLiteStateStore,
    *,
    authorizer: Any | None = None,
    clock: Any | None = None,
    event_id_factory: Any | None = None,
    transition_checkpoint_publisher: Any | None = None,
) -> StateMCPService:
    registry_digest = hashlib.sha256(b"context.public-runtime/v1").hexdigest()
    return StateMCPService(
        store,
        authorizer=authorizer or _LocalCliAuthorizer(),
        registry_digest=registry_digest,
        clock=clock or (lambda: datetime.now(UTC).isoformat()),
        event_id_factory=event_id_factory
        or (lambda request_id: f"event-{request_id}-{uuid.uuid4().hex}"),
        transition_checkpoint_publisher=transition_checkpoint_publisher,
    )


def _init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if _ID_RE.fullmatch(args.project_id) is None:
        raise ValueError("project-id must be a lowercase bounded identifier")
    target = root / ".continuity"
    outputs = [
        target / "project.yaml",
        target / "MASTER.md",
        target / "STATUS.md",
        target / "MASTER.en.md",
        target / "STATUS.en.md",
    ]
    existing = [path for path in outputs if path.exists()]
    if existing:
        raise FileExistsError(f"initialization would overwrite: {existing[0]}")
    target.mkdir(parents=True, exist_ok=True)
    (target / ".gitignore").write_text(
        "# Continuity runtime and generated projections\n"
        "state.sqlite3\nstate.sqlite3-*\nartifacts/\n"
        "checkpoint-ref.json\nresume-packet.json\nstatus-projection.json\n"
        "STATUS.current.md\nSTATUS.current.en.md\ninteraction-cursors/\nskill-locks/\n",
        encoding="utf-8",
    )
    display_name = args.display_name or args.project_id
    profile = _template("project.yaml").replace("__PROJECT_ID__", args.project_id)
    profile = profile.replace("__DISPLAY_NAME__", display_name)
    (target / "project.yaml").write_text(profile, encoding="utf-8")
    (target / "MASTER.md").write_text(_template("MASTER.md"), encoding="utf-8")
    (target / "STATUS.md").write_text(_template("STATUS.md"), encoding="utf-8")
    (target / "MASTER.en.md").write_text(_template("MASTER.en.md"), encoding="utf-8")
    (target / "STATUS.en.md").write_text(_template("STATUS.en.md"), encoding="utf-8")
    project = _load_project(root)
    store = SQLiteStateStore(_state_file(root, project))
    store.initialize()
    store.create_project(_initial_state(args.project_id))
    print(
        json.dumps(
            {
                "status": "initialized",
                "project_id": args.project_id,
                "root": str(root),
                "profile": "local-embedded",
            },
            sort_keys=True,
        )
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for key in ("master_path", "status_path"):
        path = root / project["governance"][key]
        if not path.is_file():
            raise FileNotFoundError(f"governance document is missing: {path}")
    _open_state_store(root, project)
    print(json.dumps({"status": "passed", "project_id": project["project_id"]}))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    _open_state_store(root, project)
    sqlite_version = sqlite3.sqlite_version
    print(
        json.dumps(
            {
                "status": "ready",
                "project_id": project["project_id"],
                "python": sys.version.split()[0],
                "sqlite": sqlite_version,
                "runtime_profile": project["runtime_profile"],
                "external_services_required": 0,
            },
            sort_keys=True,
        )
    )
    return 0


def _state_show(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    store = _open_state_store(root, project)
    response = _state_service(store).call_tool(
        "context.state.read",
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": f"cli-read-{uuid.uuid4().hex}",
            "project_id": project["project_id"],
        },
        context=RequestContext("local-user", "local-cli"),
    )
    if not response["ok"]:
        raise RuntimeError(response["error"]["message"])
    result = response["result"]
    print(
        json.dumps(
            {
                "status": "ok",
                "project_id": project["project_id"],
                "revision": result["revision"],
                "event_head": result["event_head"],
                "state": result["snapshot"],
            },
            sort_keys=True,
        )
    )
    return 0


def _status_render(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    with contextlib.redirect_stdout(io.StringIO()):
        _resume(argparse.Namespace(root=str(root), interaction_cursor=None, skill_lock=None))
    packet = json.loads((root / ".continuity/resume-packet.json").read_text(encoding="utf-8"))
    status_path = root / ".continuity/STATUS.current.md"
    status_en_path = root / ".continuity/STATUS.current.en.md"
    _write_text_atomic(status_path, render_status_projection(packet, language="zh-CN"))
    _write_text_atomic(status_en_path, render_status_projection(packet, language="en"))
    projection = {
        "schema_version": "context.status-projection/v1alpha1",
        "project_id": packet["project_id"],
        "revision": packet["revision"],
        "source_packet_sha256": packet["packet_sha256"],
        "status_sha256": hashlib.sha256(status_path.read_bytes()).hexdigest(),
        "status_en_sha256": hashlib.sha256(status_en_path.read_bytes()).hexdigest(),
        "state_write_authority": False,
        "completion_authority": False,
        "projection_sha256": "0" * 64,
    }
    projection["projection_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in projection.items()
                if key != "projection_sha256"
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _write_json_atomic(root / ".continuity/status-projection.json", projection)
    print(
        json.dumps(
            {
                "status": "rendered",
                "project_id": packet["project_id"],
                "revision": packet["revision"],
                "status_path": str(status_path),
                "status_en_path": str(status_en_path),
            },
            sort_keys=True,
        )
    )
    return 0


def _parse_scope(raw: str) -> dict[str, str]:
    if not isinstance(raw, str) or ":" not in raw:
        raise ValueError("scope must use kind:reference")
    kind, reference = raw.split(":", 1)
    if not kind or not reference:
        raise ValueError("scope must use kind:reference")
    return {"scope_kind": kind, "scope_ref": reference}


def _load_current_attach_proposal(
    root: Path,
    project_id: str,
    *,
    verify_sources: bool,
) -> dict[str, Any]:
    path = root / ".continuity/attach-proposal.json"
    try:
        proposal = json.loads(path.read_text(encoding="utf-8"))
        validate_attach_proposal(root, proposal, verify_sources=verify_sources)
    except (OSError, json.JSONDecodeError, CanonicalAttachError) as exc:
        raise ValueError(str(exc) or "attach proposal is unavailable or invalid") from exc
    if proposal["project_id"] != project_id:
        raise ValueError("proposal project_id does not match project profile")
    return proposal


def _canonical_master_sha256(proposal: dict[str, Any]) -> str:
    sources = [item for item in proposal["sources"] if item["kind"] == "master"]
    if len(sources) != 1:
        raise ValueError("attach proposal must bind exactly one canonical master")
    return sources[0]["content_sha256"]


def _read_state_result(
    store: SQLiteStateStore,
    project_id: str,
) -> dict[str, Any]:
    response = _state_service(store).call_tool(
        READ_TOOL,
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": f"cli-read-{uuid.uuid4().hex}",
            "project_id": project_id,
        },
        context=RequestContext("local-user", "local-cli"),
    )
    if not response["ok"]:
        raise ValueError(response["error"]["message"])
    return response["result"]


def _checkpoint_store(root: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(root / ".continuity/artifacts")
    store.initialize()
    return store


def _checkpoint_ref_file(root: Path) -> Path:
    return root / ".continuity/checkpoint-ref.json"


def _transition_pending_checkpoint_file(root: Path) -> Path:
    return root / ".continuity/checkpoint-ref.transition-pending.json"


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _attach_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal = build_attach_proposal(
        root=root,
        project_id=project["project_id"],
        master_path=args.master,
        status_path=args.status,
        work_id=args.work_id,
        work_title=args.work_title,
        owner_ref=args.owner_ref,
        scope_refs=[_parse_scope(item) for item in args.scope],
        created_at=datetime.now(UTC).isoformat(),
    )
    path = root / ".continuity/attach-proposal.json"
    path.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "planned",
                "project_id": project["project_id"],
                "proposal": str(path),
                "proposal_sha256": proposal["proposal_sha256"],
                "state_write_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _attach_refresh(args: argparse.Namespace) -> int:
    """Rebind an existing proposal to current sources without State writes."""
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal_path = root / args.proposal
    try:
        old = json.loads(proposal_path.read_text(encoding="utf-8"))
        validate_attach_proposal(root, old, verify_sources=False)
    except (OSError, json.JSONDecodeError, CanonicalAttachError) as exc:
        raise ValueError(str(exc) or "attach proposal is unavailable or invalid") from exc
    if old["project_id"] != project["project_id"]:
        raise ValueError("proposal project_id does not match project profile")
    paths = {item["kind"]: item["path"] for item in old["sources"]}
    refreshed = build_attach_proposal(
        root=root,
        project_id=project["project_id"],
        master_path=paths["master"],
        status_path=paths["status"],
        work_id=old["work"]["work_id"],
        work_title=old["work"]["title"],
        owner_ref=old["work"]["owner_ref"],
        scope_refs=copy.deepcopy(old["work"]["scope_refs"]),
        created_at=datetime.now(UTC).isoformat(),
    )
    _write_json_atomic(proposal_path, refreshed)
    changed = sorted(
        source["kind"]
        for source in refreshed["sources"]
        for previous in old["sources"]
        if source["kind"] == previous["kind"]
        and source["content_sha256"] != previous["content_sha256"]
    )
    print(
        json.dumps(
            {
                "status": "refreshed",
                "project_id": project["project_id"],
                "old_proposal_sha256": old["proposal_sha256"],
                "proposal_sha256": refreshed["proposal_sha256"],
                "changed_sources": changed,
                "state_write_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _attach_approve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal_path = root / args.proposal
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        validate_attach_proposal(root, proposal, verify_sources=True)
    except (OSError, json.JSONDecodeError, CanonicalAttachError) as exc:
        raise ValueError(str(exc)) from exc
    if proposal["project_id"] != project["project_id"]:
        raise ValueError("proposal project_id does not match project profile")
    if _ID_RE.fullmatch(args.actor_ref) is None or _ID_RE.fullmatch(args.claim_id) is None:
        raise ValueError("actor-ref and claim-id must be bounded identifiers")

    store = _open_state_store(root, project)
    current = store.read_project(project["project_id"])
    now = datetime.now(UTC)
    now_text = now.isoformat()
    source_digest = hashlib.sha256(
        "".join(source["content_sha256"] for source in proposal["sources"]).encode()
    ).hexdigest()
    evidence_id = f"evidence-attach-{proposal['proposal_sha256'][:16]}"
    evidence = {
        "evidence_id": evidence_id,
        "kind": "artifact",
        "artifact_ref": f"artifact://sha256/{source_digest}",
        "content_sha256": source_digest,
        "validity": "verified",
        "observed_at": now_text,
        "verified_at": now_text,
    }
    context = RequestContext(args.actor_ref, "local-attach-approved")
    service = _state_service(
        store,
        authorizer=_LocalAttachAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request: f"event-{request}",
    )
    if current["project"]["revision"] != 0:
        imported = next(
            (
                item
                for item in current["works"]
                if item["work_id"] == proposal["work"]["work_id"]
            ),
            None,
        )
        claim = next(
            (
                item
                for item in current["claims"]
                if item["claim_id"] == args.claim_id
            ),
            None,
        )
        if imported is not None and claim is not None and claim["status"] == "active":
            if (
                imported["title"] != proposal["work"]["title"]
                or imported["owner_refs"] != [proposal["work"]["owner_ref"]]
                or imported["scope_refs"] != proposal["work"]["scope_refs"]
                or claim["actor_ref"] != args.actor_ref
                or claim["work_id"] != imported["work_id"]
            ):
                raise ValueError("active attach identity differs from the new proposal")
            evidence_by_id = {
                item["evidence_id"]: item for item in current["evidence"]
            }
            source_already_bound = any(
                evidence_by_id[item_id]["content_sha256"] == source_digest
                for item_id in imported["evidence_ids"]
            )
            if source_already_bound:
                print(
                    json.dumps(
                        {
                            "status": "already-attached",
                            "project_id": project["project_id"],
                            "revision": current["project"]["revision"],
                            "work_id": imported["work_id"],
                            "claim_id": claim["claim_id"],
                        },
                        sort_keys=True,
                    )
                )
                return 0
            refreshed_work = copy.deepcopy(imported)
            refreshed_work["evidence_ids"].append(evidence_id)
            refreshed_work["revision"] += 1
            refresh_request = {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": f"attach-refresh-{proposal['proposal_sha256'][:16]}",
                "project_id": project["project_id"],
                "expected_revision": current["project"]["revision"],
                "causation_ref": f"attach:{proposal['proposal_sha256']}",
                "correlation_ref": f"project:{project['project_id']}",
                "supersedes_event_id": None,
                "changes": [
                    {"collection": "evidence", "object_id": evidence_id, "value": evidence},
                    {
                        "collection": "works",
                        "object_id": refreshed_work["work_id"],
                        "value": refreshed_work,
                    },
                ],
            }
            refreshed = service.call_tool(COMMIT_TOOL, refresh_request, context=context)
            if not refreshed["ok"]:
                raise ValueError(refreshed["error"]["message"])
            print(
                json.dumps(
                    {
                        "status": "refreshed",
                        "project_id": project["project_id"],
                        "proposal_sha256": proposal["proposal_sha256"],
                        "revision": refreshed["result"]["revision"],
                        "work_id": refreshed_work["work_id"],
                        "claim_id": claim["claim_id"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        raise ValueError("project state has advanced; attach requires a new proposal")

    initial_work = next(
        item for item in current["works"] if item["work_id"] == "work-initial"
    )
    initial_work = copy.deepcopy(initial_work)
    initial_work["status"] = "rejected"
    initial_work["revision"] = 1
    work = {
        "work_id": proposal["work"]["work_id"],
        "kind": "work",
        "title": proposal["work"]["title"],
        "status": "ready",
        "parent_work_id": None,
        "dependency_ids": [],
        "owner_refs": [proposal["work"]["owner_ref"]],
        "scope_refs": copy.deepcopy(proposal["work"]["scope_refs"]),
        "overlap_candidate_ids": [],
        "dedupe_status": "clear",
        "supersedes_work_id": None,
        "evidence_ids": [evidence_id],
        "blocker_ids": [],
        "revision": 0,
    }
    request_id = f"attach-{proposal['proposal_sha256'][:16]}"
    commit_request = {
        "schema_version": "context.state-mcp-request/v1alpha1",
        "request_id": request_id,
        "project_id": project["project_id"],
        "expected_revision": 0,
        "causation_ref": f"attach:{proposal['proposal_sha256']}",
        "correlation_ref": f"project:{project['project_id']}",
        "supersedes_event_id": None,
        "changes": [
            {"collection": "works", "object_id": "work-initial", "value": initial_work},
            {"collection": "evidence", "object_id": evidence_id, "value": evidence},
            {"collection": "works", "object_id": work["work_id"], "value": work},
        ],
    }
    committed = service.call_tool(COMMIT_TOOL, commit_request, context=context)
    if not committed["ok"]:
        raise ValueError(committed["error"]["message"])
    claim_request = {
        "schema_version": "context.state-mcp-request/v1alpha1",
        "request_id": f"{request_id}-claim",
        "project_id": project["project_id"],
        "expected_revision": 1,
        "work_id": work["work_id"],
        "claim_id": args.claim_id,
        "scope_owners": copy.deepcopy(work["scope_refs"]),
        "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
        "causation_ref": f"attach:{proposal['proposal_sha256']}",
        "correlation_ref": f"project:{project['project_id']}",
    }
    claimed = service.call_tool(CLAIM_TOOL, claim_request, context=context)
    if not claimed["ok"]:
        raise ValueError(claimed["error"]["message"])
    print(
        json.dumps(
            {
                "status": "attached",
                "project_id": project["project_id"],
                "proposal_sha256": proposal["proposal_sha256"],
                "work_id": work["work_id"],
                "claim_id": args.claim_id,
                "revision": claimed["result"]["revision"],
                "state_write_authority": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _resume(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal_path = root / ".continuity/attach-proposal.json"
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        validate_attach_proposal(root, proposal, verify_sources=False)
    except (OSError, json.JSONDecodeError, CanonicalAttachError) as exc:
        raise ValueError("attach proposal is unavailable or invalid") from exc
    try:
        validate_attach_proposal(root, proposal, verify_sources=True)
        source_fresh = True
    except CanonicalAttachError:
        source_fresh = False

    store = _open_state_store(root, project)
    read_result = _read_state_result(store, project["project_id"])
    snapshot = read_result["snapshot"]
    active_ids = snapshot["project"]["active_work_ids"]
    primary_work_id = snapshot["project"]["primary_work_id"]
    idle = not active_ids and primary_work_id is None
    if not idle and (
        len(active_ids) != 1 or primary_work_id != active_ids[0]
    ):
        raise ValueError("resume requires one active Work or a valid idle State")
    if idle:
        if any(item["status"] == "active" for item in snapshot["claims"]):
            raise ValueError("idle resume cannot retain an active claim")
        work = None
        claim = None
        lease_valid = True
    else:
        work = next(
            item for item in snapshot["works"] if item["work_id"] == active_ids[0]
        )
        claims = [
            item
            for item in snapshot["claims"]
            if item["work_id"] == work["work_id"] and item["status"] == "active"
        ]
        if len(claims) != 1:
            raise ValueError("resume requires exactly one active claim")
        claim = claims[0]
        now = datetime.now(UTC)
        lease_expires = datetime.fromisoformat(
            claim["lease_expires_at"].replace("Z", "+00:00")
        )
        lease_valid = lease_expires > now
    event_head = read_result["event_head"]
    if event_head is None:
        raise ValueError("resume requires an append-only Event head")
    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("resume requires a valid checkpoint ref") from exc
    try:
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except CheckpointStaleError as exc:
        if not idle or not source_fresh:
            raise ValueError(str(exc)) from exc
        checkpoint_ref = publish_checkpoint(
            read_result,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        _write_json_atomic(_checkpoint_ref_file(root), checkpoint_ref.to_document())
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except CheckpointError as exc:
        raise ValueError(str(exc)) from exc
    try:
        interaction_cursor = load_interaction_cursor(args.interaction_cursor)
        skill_lock = load_recovery_skill_lock(args.skill_lock)
    except RecoveryEnvelopeError as exc:
        raise ValueError(str(exc)) from exc
    current_decision_ids = set(snapshot["project"]["current_decision_ids"])
    active_constraint_ids = set(snapshot["project"]["active_constraint_ids"])
    open_blocker_ids = set(snapshot["project"]["open_blocker_ids"])
    next_action = "activate-next-work" if idle else "continue-active-work"
    if (
        not idle
        and interaction_cursor is not None
        and interaction_cursor["response_mode"] == "answer-current-input"
    ):
        next_action = "answer-current-input"
    if not source_fresh or not lease_valid:
        next_action = "remain-read-only"
    packet = compose_recovery_envelope(
        project_id=project["project_id"],
        revision=snapshot["project"]["revision"],
        event_head=event_head,
        checkpoint_ref=checkpoint_ref.to_document(),
        active_work=(
            None
            if work is None
            else {
                key: copy.deepcopy(work[key])
                for key in (
                    "work_id",
                    "title",
                    "status",
                    "revision",
                    "scope_refs",
                    "evidence_ids",
                )
            }
        ),
        claim=(
            None
            if claim is None
            else {
                key: copy.deepcopy(claim[key])
                for key in (
                    "claim_id",
                    "actor_ref",
                    "status",
                    "lease_expires_at",
                    "scope_owners",
                )
            }
        ),
        current_decisions=[
            {
                "decision_id": item["decision_id"],
                "statement": item["statement"],
                "evidence_ids": copy.deepcopy(item["evidence_ids"]),
            }
            for item in snapshot["decisions"]
            if item["decision_id"] in current_decision_ids and item["status"] == "accepted"
        ],
        current_constraints=[
            {
                "constraint_id": item["constraint_id"],
                "statement": item["statement"],
                "scope_work_ids": copy.deepcopy(item["scope_work_ids"]),
                "evidence_ids": copy.deepcopy(item["evidence_ids"]),
            }
            for item in snapshot["constraints"]
            if item["constraint_id"] in active_constraint_ids and item["status"] == "active"
        ],
        open_blockers=[
            {
                "blocker_id": item["blocker_id"],
                "reason": item["reason"],
                "blocked_work_ids": copy.deepcopy(item["blocked_work_ids"]),
                "evidence_ids": copy.deepcopy(item["evidence_ids"]),
            }
            for item in snapshot["blockers"]
            if item["blocker_id"] in open_blocker_ids and item["status"] == "open"
        ],
        return_point_work_id=(
            None
            if work is None
            else (work.get("return_point_work_id") or work.get("parent_work_id"))
        ),
        effect_high_watermark=snapshot["project"]["effect_high_watermark"],
        proposal_sha256=proposal["proposal_sha256"],
        source_fresh=source_fresh,
        lease_valid=lease_valid,
        next_action=next_action,
        interaction_cursor=interaction_cursor,
        skill_lock=skill_lock,
    )
    path = root / ".continuity/resume-packet.json"
    path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(packet, ensure_ascii=False, sort_keys=True))
    return 0


def _checkpoint_create(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    store = _open_state_store(root, project)
    read_result = _read_state_result(store, project["project_id"])
    artifact_store = _checkpoint_store(root)
    try:
        checkpoint_ref = publish_checkpoint(
            read_result,
            artifact_store,
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
    except CheckpointError as exc:
        raise ValueError(str(exc)) from exc
    _write_json_atomic(_checkpoint_ref_file(root), checkpoint_ref.to_document())
    print(
        json.dumps(
            {
                "status": "created",
                "project_id": project["project_id"],
                "revision": read_result["revision"],
                "event_head": read_result["event_head"],
                "checkpoint_ref": checkpoint_ref.to_document(),
            },
            sort_keys=True,
        )
    )
    return 0


def _checkpoint_verify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("checkpoint ref is unavailable or invalid") from exc
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    try:
        restored = restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=read_result["snapshot"]["project"][
                "governance_ref"
            ],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except CheckpointError as exc:
        raise ValueError(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "verified",
                "project_id": project["project_id"],
                "revision": restored.manifest["revision"],
                "event_head": restored.manifest["event_head"],
                "active_work_ids": restored.manifest["active_work_ids"],
                "primary_work_id": restored.manifest["primary_work_id"],
                "checkpoint_ref": checkpoint_ref.to_document(),
            },
            sort_keys=True,
        )
    )
    return 0


def _completion_evidence(
    root: Path,
    checkpoint_ref: ArtifactRef,
    evidence_files: list[str],
    *,
    observed_at: str,
) -> list[dict[str, Any]]:
    artifact_store = _checkpoint_store(root)
    evidence = [
        {
            "evidence_id": f"evidence-checkpoint-{checkpoint_ref.digest[:16]}",
            "kind": "artifact",
            "artifact_ref": checkpoint_ref.uri,
            "content_sha256": checkpoint_ref.digest,
            "validity": "verified",
            "observed_at": observed_at,
            "verified_at": observed_at,
        }
    ]
    seen = {evidence[0]["evidence_id"]}
    for raw_path in evidence_files:
        path = Path(raw_path).resolve()
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"evidence file is unavailable or invalid: {path}")
        size = path.stat().st_size
        if size <= 0 or size > 16 * 1024 * 1024:
            raise ValueError(f"evidence file is outside the 16 MiB bound: {path}")
        ref = artifact_store.put_bytes(path.read_bytes())
        evidence_id = f"evidence-test-{ref.digest[:16]}"
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        evidence.append(
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": ref.uri,
                "content_sha256": ref.digest,
                "validity": "verified",
                "observed_at": observed_at,
                "verified_at": observed_at,
            }
        )
    if len(evidence) == 1:
        raise ValueError("at least one non-checkpoint evidence file is required")
    return evidence


def _verified_workspace(
    project_root: Path,
    workspace_root: str,
    *,
    expected_head: str,
    expected_ref: str | None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    if not workspace.is_dir() or re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise ValueError("transition_gate:workspace_identity")

    def git(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("transition_gate:workspace_git")
        return completed.stdout.strip()

    project_common = Path(git(project_root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    workspace_common = Path(git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if project_common.resolve() != workspace_common.resolve():
        raise ValueError("transition_gate:workspace_repository")
    head_commit = git(workspace, "rev-parse", "HEAD")
    if head_commit != expected_head:
        raise ValueError("transition_gate:workspace_head")
    if git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("transition_gate:workspace_clean")
    expected_ref_commit: str | None = None
    if expected_ref is not None:
        if not expected_ref.strip() or len(expected_ref) > 500:
            raise ValueError("transition_gate:workspace_ref")
        expected_ref_commit = git(workspace, "rev-parse", "--verify", expected_ref)
        if expected_ref_commit != head_commit:
            raise ValueError("transition_gate:workspace_ref")
    return {
        "head_commit": head_commit,
        "clean": True,
        "expected_ref": expected_ref,
        "expected_ref_commit": expected_ref_commit,
    }


def _work_complete(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (args.work_id, args.claim_id, args.actor_ref):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("Work, claim, and actor identifiers must be bounded")
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("checkpoint ref is unavailable or invalid") from exc
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    snapshot = read_result["snapshot"]
    work = next(
        (item for item in snapshot["works"] if item["work_id"] == args.work_id),
        None,
    )
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    already_completed = (
        work is not None
        and claim is not None
        and work["status"] == "completed"
        and claim["status"] == "released"
        and claim["work_id"] == work["work_id"]
        and claim["actor_ref"] == args.actor_ref
    )
    if already_completed:
        evidence_by_sha = {
            item["content_sha256"]
            for item in snapshot["evidence"]
            if item["evidence_id"] in work["evidence_ids"]
            and item["validity"] == "verified"
        }
        for raw_path in args.evidence_file:
            path = Path(raw_path).resolve()
            if (
                not path.is_file()
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() not in evidence_by_sha
            ):
                raise ValueError("completed Work evidence does not match the retry")
        final_checkpoint = publish_checkpoint(
            read_result,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        restore_checkpoint(
            final_checkpoint,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
        _write_json_atomic(
            _checkpoint_ref_file(root), final_checkpoint.to_document()
        )
        print(
            json.dumps(
                {
                    "status": "already-completed",
                    "project_id": project["project_id"],
                    "work_id": args.work_id,
                    "claim_id": args.claim_id,
                    "revision": read_result["revision"],
                    "event_head": read_result["event_head"],
                    "checkpoint_ref": final_checkpoint.to_document(),
                    "checkpoint_verified": True,
                    "evidence_ids": work["evidence_ids"],
                },
                sort_keys=True,
            )
        )
        return 0
    if not already_completed:
        try:
            restore_checkpoint(
                checkpoint_ref,
                _checkpoint_store(root),
                expected_project_id=project["project_id"],
                expected_revision=read_result["revision"],
                expected_event_head=read_result["event_head"],
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=_canonical_master_sha256(proposal),
                expected_registry_digest=read_result["registry_digest"],
            )
        except CheckpointError as exc:
            raise ValueError(str(exc)) from exc
    now_text = datetime.now(UTC).isoformat()
    evidence = _completion_evidence(
        root,
        checkpoint_ref,
        args.evidence_file,
        observed_at=now_text,
    )
    evidence_digest = hashlib.sha256(
        json.dumps(
            [item["content_sha256"] for item in evidence],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    request = {
        "schema_version": LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
        "request_id": f"complete-{args.work_id}-{evidence_digest[:16]}",
        "project_id": project["project_id"],
        "expected_revision": read_result["revision"],
        "work_id": args.work_id,
        "claim_id": args.claim_id,
        "checkpoint_ref": checkpoint_ref.to_document(),
        "evidence": evidence,
        "causation_ref": f"verification:{evidence_digest}",
        "correlation_ref": f"checkpoint:{checkpoint_ref.digest}",
    }
    service = _state_service(
        state_store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
    )
    response = service.call_tool(
        LOCAL_WORK_COMPLETION_TOOL,
        request,
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
    )
    if not response["ok"]:
        raise ValueError(response["error"]["message"])
    result = response["result"]
    final_checkpoint = publish_checkpoint(
        {
            "snapshot": result["snapshot"],
            "revision": result["revision"],
            "event_head": result["event_head"],
            "registry_digest": result["registry_digest"],
            "capabilities": result["capabilities"],
        },
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    restore_checkpoint(
        final_checkpoint,
        _checkpoint_store(root),
        expected_project_id=project["project_id"],
        expected_revision=result["revision"],
        expected_event_head=result["event_head"],
        expected_governance_ref=result["snapshot"]["project"]["governance_ref"],
        expected_plan_sha256=_canonical_master_sha256(proposal),
        expected_registry_digest=result["registry_digest"],
    )
    _write_json_atomic(_checkpoint_ref_file(root), final_checkpoint.to_document())
    print(
        json.dumps(
            {
                "status": (
                    "already-completed"
                    if result["already_completed"]
                    else "completed"
                ),
                "project_id": project["project_id"],
                "work_id": args.work_id,
                "claim_id": args.claim_id,
                "revision": result["revision"],
                "event_head": result["event_head"],
                "checkpoint_ref": final_checkpoint.to_document(),
                "checkpoint_verified": True,
                "evidence_ids": result["evidence_ids"],
            },
            sort_keys=True,
        )
    )
    return 0


def _work_transition(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    store = _open_state_store(root, project)
    before = _read_state_result(store, project["project_id"])
    before_event_head = copy.deepcopy(before["event_head"])
    pending_checkpoint_path = _transition_pending_checkpoint_file(root)

    def deny(gate: str, *, code: str = "integrity", message: str | None = None) -> int:
        current = _read_state_result(store, project["project_id"])
        rollback_verified = (
            current["revision"] == before["revision"]
            and current["event_head"] == before_event_head
        )
        if pending_checkpoint_path.exists():
            pending_checkpoint_path.unlink()
        print(
            json.dumps(
                {
                    "status": "denied",
                    "project_id": project["project_id"],
                    "failed_gate": gate,
                    "error_code": code,
                    "message": message or f"transition gate failed: {gate}",
                    "revision": current["revision"],
                    "event_head": current["event_head"],
                    "rollback_verified": rollback_verified,
                    "state_changed": not rollback_verified,
                    "next_action": f"satisfy-gate:{gate}",
                    "completion_authority": False,
                },
                sort_keys=True,
            )
        )
        return 2

    for value in (
        args.work_id,
        args.claim_id,
        args.actor_ref,
        args.return_work_id,
        args.successor_claim_id,
        args.resolved_blocker_id,
    ):
        if _STATE_ID_RE.fullmatch(value) is None:
            return deny("transition_identity", code="invalid_request")
    if args.work_id == args.return_work_id:
        return deny("predeclared_return_point", code="invalid_request")
    successor_scopes = [_parse_scope(item) for item in args.successor_scope]
    remaining_id = args.remaining_blocker_id
    remaining_reason = args.remaining_blocker_reason
    if (remaining_id is None) != (remaining_reason is None):
        return deny("remaining_blocker_contract", code="invalid_request")
    if remaining_id is not None and (
        _STATE_ID_RE.fullmatch(remaining_id) is None
        or not remaining_reason.strip()
        or len(remaining_reason.strip()) > 2_000
    ):
        return deny("remaining_blocker_contract", code="invalid_request")

    try:
        proposal = _load_current_attach_proposal(
            root,
            project["project_id"],
            verify_sources=True,
        )
    except ValueError as exc:
        return deny("source_fresh", message=str(exc))
    try:
        workspace = _verified_workspace(
            root,
            args.workspace_root,
            expected_head=args.expected_head,
            expected_ref=args.expected_ref,
        )
    except ValueError as exc:
        marker = str(exc)
        gate = marker.removeprefix("transition_gate:")
        return deny(gate, message=marker)

    snapshot = before["snapshot"]
    child = next(
        (item for item in snapshot["works"] if item["work_id"] == args.work_id),
        None,
    )
    old_claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    return_work = next(
        (
            item
            for item in snapshot["works"]
            if item["work_id"] == args.return_work_id
        ),
        None,
    )
    resolved_blocker = next(
        (
            item
            for item in snapshot["blockers"]
            if item["blocker_id"] == args.resolved_blocker_id
        ),
        None,
    )
    successor_claim = next(
        (
            item
            for item in snapshot["claims"]
            if item["claim_id"] == args.successor_claim_id
        ),
        None,
    )
    remaining_blocker = (
        None
        if remaining_id is None
        else next(
            (
                item
                for item in snapshot["blockers"]
                if item["blocker_id"] == remaining_id
            ),
            None,
        )
    )

    evidence_hashes: list[str] = []
    try:
        for raw_path in args.evidence_file:
            path = Path(raw_path).resolve()
            if not path.is_file() or path.is_symlink():
                raise ValueError("evidence file is unavailable or invalid")
            size = path.stat().st_size
            if size <= 0 or size > 16 * 1024 * 1024:
                raise ValueError("evidence file is outside the 16 MiB bound")
            evidence_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError, ValueError) as exc:
        return deny("evidence", message=str(exc))

    already_transitioned = (
        child is not None
        and child["status"] == "completed"
        and child["parent_work_id"] == args.return_work_id
        and old_claim is not None
        and old_claim["status"] == "released"
        and old_claim["work_id"] == child["work_id"]
        and old_claim["actor_ref"] == args.actor_ref
        and return_work is not None
        and return_work["status"] == "active"
        and return_work["scope_refs"] == successor_scopes
        and snapshot["project"]["primary_work_id"] == return_work["work_id"]
        and successor_claim is not None
        and successor_claim["status"] == "active"
        and successor_claim["work_id"] == return_work["work_id"]
        and successor_claim["actor_ref"] == args.actor_ref
        and successor_claim["scope_owners"] == successor_scopes
        and resolved_blocker is not None
        and resolved_blocker["status"] == "resolved"
        and (
            remaining_id is None
            or (
                remaining_blocker is not None
                and remaining_blocker["status"] == "open"
                and remaining_blocker["reason"] == remaining_reason.strip()
                and remaining_id in return_work["blocker_ids"]
            )
        )
    )
    if already_transitioned:
        evidence_by_sha = {
            item["content_sha256"]: item
            for item in snapshot["evidence"]
            if item["evidence_id"] in child["evidence_ids"]
            and item["validity"] == "verified"
        }
        if not set(evidence_hashes).issubset(evidence_by_sha):
            return deny("evidence_replay")
        try:
            checkpoint_ref = ArtifactRef.from_document(
                json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
            )
            restore_checkpoint(
                checkpoint_ref,
                _checkpoint_store(root),
                expected_project_id=project["project_id"],
                expected_revision=before["revision"],
                expected_event_head=before["event_head"],
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=_canonical_master_sha256(proposal),
                expected_registry_digest=before["registry_digest"],
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
            return deny("checkpoint_verified", message=str(exc))
        resume_output = io.StringIO()
        with contextlib.redirect_stdout(resume_output):
            _resume(argparse.Namespace(root=str(root), interaction_cursor=None, skill_lock=None))
        packet = json.loads(resume_output.getvalue())
        print(
            json.dumps(
                {
                    "status": "already-transitioned",
                    "project_id": project["project_id"],
                    "revision": before["revision"],
                    "event_head": before["event_head"],
                    "active_work_id": args.return_work_id,
                    "claim_id": args.successor_claim_id,
                    "checkpoint_ref": checkpoint_ref.to_document(),
                    "checkpoint_verified": packet["checkpoint_verified"],
                    "source_fresh": packet["source_fresh"],
                    "lease_valid": packet["lease_valid"],
                    "read_only": packet["read_only"],
                    "resume_packet": packet,
                    "completion_policy": {
                        "status": "granted",
                        "authority": "checkpoint-bound-local-transition",
                        "scope_expanded": False,
                    },
                    "completion_authority": False,
                },
                sort_keys=True,
            )
        )
        return 0

    if child is None or old_claim is None or return_work is None or resolved_blocker is None:
        return deny("declared_transition_objects", code="not_found")
    try:
        current_checkpoint = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
        restore_checkpoint(
            current_checkpoint,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=before["revision"],
            expected_event_head=before["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=before["registry_digest"],
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
        return deny("checkpoint_verified", message=str(exc))

    now = datetime.now(UTC)
    now_text = now.isoformat()
    try:
        evidence = _completion_evidence(
            root,
            current_checkpoint,
            args.evidence_file,
            observed_at=now_text,
        )
    except ValueError as exc:
        return deny("evidence", message=str(exc))
    evidence_ids = [item["evidence_id"] for item in evidence]
    remaining = (
        None
        if remaining_id is None
        else {
            "blocker_id": remaining_id,
            "reason": remaining_reason.strip(),
            "evidence_ids": evidence_ids,
        }
    )
    transition_basis = {
        "project_id": project["project_id"],
        "expected_revision": before["revision"],
        "work_id": args.work_id,
        "claim_id": args.claim_id,
        "return_point_work_id": args.return_work_id,
        "resolved_blocker_id": args.resolved_blocker_id,
        "successor_claim_id": args.successor_claim_id,
        "successor_scope_owners": successor_scopes,
        "checkpoint_digest": current_checkpoint.digest,
        "evidence_ids": evidence_ids,
        "source_proposal_sha256": proposal["proposal_sha256"],
        "workspace_verification": workspace,
        "remaining_blocker": remaining,
    }
    transition_sha256 = hashlib.sha256(
        json.dumps(
            transition_basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def publish_and_verify(read_result: dict[str, Any]) -> dict[str, Any]:
        checkpoint_ref = publish_checkpoint(
            read_result,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=read_result["snapshot"]["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
        _write_json_atomic(pending_checkpoint_path, checkpoint_ref.to_document())
        return checkpoint_ref.to_document()

    service = _state_service(
        store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
        transition_checkpoint_publisher=publish_and_verify,
    )
    response = service.call_tool(
        LOCAL_WORK_TRANSITION_TOOL,
        {
            "schema_version": LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION,
            "request_id": f"transition-{transition_sha256}",
            "project_id": project["project_id"],
            "expected_revision": before["revision"],
            "work_id": args.work_id,
            "claim_id": args.claim_id,
            "checkpoint_ref": current_checkpoint.to_document(),
            "evidence": evidence,
            "return_point_work_id": args.return_work_id,
            "resolved_blocker_id": args.resolved_blocker_id,
            "successor_claim_id": args.successor_claim_id,
            "successor_scope_owners": successor_scopes,
            "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
            "source_proposal_sha256": proposal["proposal_sha256"],
            "workspace_verification": workspace,
            "remaining_blocker": remaining,
            "causation_ref": f"checkpoint:{current_checkpoint.digest}",
            "correlation_ref": f"return:{args.return_work_id}",
        },
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
    )
    if not response["ok"]:
        marker = response["error"]["message"]
        gate = (
            marker.removeprefix("transition_gate:")
            if marker.startswith("transition_gate:")
            else {
                "conflict": "expected_revision",
                "busy": "state_store_busy",
                "integrity": "state_integrity",
                "capability": "state_store_capability",
            }.get(response["error"]["code"], "state_transition")
        )
        return deny(
            gate,
            code=response["error"]["code"],
            message=marker,
        )

    result = response["result"]
    final_checkpoint = ArtifactRef.from_document(result["checkpoint_ref"])
    if pending_checkpoint_path.exists():
        os.replace(pending_checkpoint_path, _checkpoint_ref_file(root))
    else:
        _write_json_atomic(_checkpoint_ref_file(root), final_checkpoint.to_document())
    final_read = _read_state_result(store, project["project_id"])
    try:
        restore_checkpoint(
            final_checkpoint,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=final_read["revision"],
            expected_event_head=final_read["event_head"],
            expected_governance_ref=final_read["snapshot"]["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=final_read["registry_digest"],
        )
    except CheckpointError as exc:
        return deny("checkpoint_postcommit", message=str(exc))
    resume_output = io.StringIO()
    with contextlib.redirect_stdout(resume_output):
        _resume(argparse.Namespace(root=str(root), interaction_cursor=None, skill_lock=None))
    packet = json.loads(resume_output.getvalue())
    print(
        json.dumps(
            {
                "status": "transitioned",
                "project_id": project["project_id"],
                "revision": result["revision"],
                "event_head": result["event_head"],
                "completed_work_id": args.work_id,
                "active_work_id": args.return_work_id,
                "claim_id": args.successor_claim_id,
                "checkpoint_ref": final_checkpoint.to_document(),
                "checkpoint_verified": packet["checkpoint_verified"],
                "source_fresh": packet["source_fresh"],
                "lease_valid": packet["lease_valid"],
                "read_only": packet["read_only"],
                "resume_packet": packet,
                "completion_policy": result["completion_policy"],
                "completion_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _work_suspend_dependency(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (
        args.work_id,
        args.claim_id,
        args.actor_ref,
        args.dependency_work_id,
    ):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("Work, claim, actor, and dependency IDs must be bounded")
    if args.work_id == args.dependency_work_id:
        raise ValueError("dependency Work must differ from the suspended Work")
    reason = args.reason.strip()
    if not reason or len(reason) > 2_000:
        raise ValueError("dependency reason must be bounded and non-empty")
    dependency_scopes = [_parse_scope(item) for item in args.dependency_scope]
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    store = _open_state_store(root, project)
    read_result = _read_state_result(store, project["project_id"])
    snapshot = read_result["snapshot"]
    work = next(
        (item for item in snapshot["works"] if item["work_id"] == args.work_id),
        None,
    )
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    if (
        work is None
        or work["status"] != "active"
        or snapshot["project"]["primary_work_id"] != work["work_id"]
    ):
        raise ValueError("suspend-dependency requires the current primary active Work")
    if (
        claim is None
        or claim["status"] != "active"
        or claim["work_id"] != work["work_id"]
        or claim["actor_ref"] != args.actor_ref
    ):
        raise ValueError("suspend-dependency claim does not match the active Work")
    if any(
        item["work_id"] == args.dependency_work_id for item in snapshot["works"]
    ):
        raise ValueError("dependency Work identity is already in use")

    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
        raise ValueError("current checkpoint cannot authorize dependency suspension") from exc

    transition_basis = {
        "project_id": project["project_id"],
        "project_revision": read_result["revision"],
        "work_id": work["work_id"],
        "work_revision": work["revision"],
        "claim_id": claim["claim_id"],
        "dependency_work_id": args.dependency_work_id,
        "dependency_title": args.dependency_work_title,
        "dependency_scopes": dependency_scopes,
        "reason": reason,
        "checkpoint_ref": checkpoint_ref.to_document(),
        "completion_authority": False,
    }
    transition_bytes = json.dumps(
        transition_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    transition_sha256 = hashlib.sha256(transition_bytes).hexdigest()
    artifact_ref = _checkpoint_store(root).put_bytes(transition_bytes)
    evidence_id = f"evidence-dependency-{transition_sha256[:16]}"
    blocker_id = f"blocker-dependency-{transition_sha256[:16]}"
    now_text = datetime.now(UTC).isoformat()
    evidence = {
        "evidence_id": evidence_id,
        "kind": "artifact",
        "artifact_ref": artifact_ref.uri,
        "content_sha256": artifact_ref.digest,
        "validity": "verified",
        "observed_at": now_text,
        "verified_at": now_text,
    }
    blocker = {
        "blocker_id": blocker_id,
        "status": "open",
        "reason": reason,
        "blocked_work_ids": [work["work_id"]],
        "evidence_ids": [evidence_id],
        "opened_at": now_text,
        "resolved_at": None,
        "supersedes_blocker_id": None,
    }
    suspended = copy.deepcopy(work)
    suspended["blocker_ids"] = sorted({*suspended["blocker_ids"], blocker_id})
    suspended["revision"] += 1
    dependency = {
        "work_id": args.dependency_work_id,
        "kind": "work",
        "title": args.dependency_work_title,
        "status": "ready",
        "parent_work_id": work["work_id"],
        "dependency_ids": [],
        "owner_refs": [args.actor_ref],
        "scope_refs": copy.deepcopy(dependency_scopes),
        "overlap_candidate_ids": [],
        "dedupe_status": "clear",
        "supersedes_work_id": None,
        "evidence_ids": [evidence_id],
        "blocker_ids": [],
        "revision": 0,
    }
    service = _state_service(
        store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
    )
    prepared = service.call_tool(
        COMMIT_TOOL,
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": f"suspend-dependency-prepare-{transition_sha256[:16]}",
            "project_id": project["project_id"],
            "expected_revision": read_result["revision"],
            "causation_ref": f"checkpoint:{checkpoint_ref.digest}",
            "correlation_ref": f"dependency:{args.dependency_work_id}",
            "supersedes_event_id": None,
            "changes": [
                {"collection": "evidence", "object_id": evidence_id, "value": evidence},
                {"collection": "blockers", "object_id": blocker_id, "value": blocker},
                {
                    "collection": "works",
                    "object_id": suspended["work_id"],
                    "value": suspended,
                },
                {
                    "collection": "works",
                    "object_id": dependency["work_id"],
                    "value": dependency,
                },
            ],
        },
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
    )
    if not prepared["ok"]:
        raise ValueError(prepared["error"]["message"])

    prepared_read = _read_state_result(store, project["project_id"])
    prepared_snapshot = prepared_read["snapshot"]
    prepared_work = next(
        item for item in prepared_snapshot["works"] if item["work_id"] == args.work_id
    )
    prepared_dependency = next(
        item
        for item in prepared_snapshot["works"]
        if item["work_id"] == args.dependency_work_id
    )
    route_checkpoint = publish_checkpoint(
        prepared_read,
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    _write_json_atomic(_checkpoint_ref_file(root), route_checkpoint.to_document())
    route_input = {
        "schema_version": "context.task-route-request/v1alpha1",
        "request_id": f"suspend-dependency-route-{transition_sha256[:16]}",
        "project_id": project["project_id"],
        "expected_project_revision": prepared_read["revision"],
        "active_work_id": prepared_work["work_id"],
        "expected_active_work_revision": prepared_work["revision"],
        "input_ref": f"artifact://sha256/{transition_sha256}",
        "input_sha256": transition_sha256,
        "input_kind": "switch",
        "classifier_confidence_millionths": 1_000_000,
        "classifier_provenance_ref": artifact_ref.uri,
        "target_work_id": prepared_dependency["work_id"],
        "user_authorization_candidate": True,
        "authorization_candidate_ref": "opaque://candidate/dependency-transition",
        "evidence_refs": [artifact_ref.uri],
    }
    decision = route_task_input(route_input, prepared_snapshot)
    decision_sha256 = hashlib.sha256(
        canonical_route_decision_bytes(decision)
    ).hexdigest()
    route_result = apply_route(
        store,
        {
            "schema_version": "context.task-route-apply-request/v1alpha1",
            "request_id": route_input["request_id"],
            "project_id": project["project_id"],
            "proposal_sha256": decision_sha256,
            "operation": "switch",
            "expected_project_revision": prepared_read["revision"],
            "expected_active_work_id": prepared_work["work_id"],
            "expected_active_work_revision": prepared_work["revision"],
            "target_work_id": prepared_dependency["work_id"],
            "target_work_revision": prepared_dependency["revision"],
            "authorization_ref": "local-workflow-approved",
            "checkpoint_ref": route_checkpoint.to_document(),
            "checkpoint_binding": {
                "checkpoint_revision": prepared_read["revision"],
                "checkpoint_event_head": prepared_read["event_head"],
                "return_work_id": prepared_work["work_id"],
                "return_work_revision": prepared_work["revision"],
            },
            "child_work": None,
            "correction_changes": None,
            "supersedes_event_id": None,
            "causation_ref": f"checkpoint:{route_checkpoint.digest}",
            "correlation_ref": f"dependency:{prepared_dependency['work_id']}",
        },
        decision=decision,
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
        artifact_store=_checkpoint_store(root),
        expected_plan_sha256=_canonical_master_sha256(proposal),
        expected_registry_digest=prepared_read["registry_digest"],
    )
    final_read = _read_state_result(store, project["project_id"])
    final_checkpoint = publish_checkpoint(
        final_read,
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    _write_json_atomic(_checkpoint_ref_file(root), final_checkpoint.to_document())
    active_claim = next(
        item
        for item in route_result["snapshot"]["claims"]
        if item["work_id"] == args.dependency_work_id and item["status"] == "active"
    )
    print(
        json.dumps(
            {
                "status": "dependency-transitioned",
                "project_id": project["project_id"],
                "revision": route_result["revision"],
                "event_head": route_result["event_head"],
                "suspended_work_id": args.work_id,
                "suspended_work_status": "ready",
                "blocker_id": blocker_id,
                "active_work_id": args.dependency_work_id,
                "claim_id": active_claim["claim_id"],
                "scope_owners": active_claim["scope_owners"],
                "return_frame": route_result["return_frame"],
                "checkpoint_ref": final_checkpoint.to_document(),
                "next_action": "continue-active-work",
                "completion_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _work_complete_dependency_return(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (
        args.work_id,
        args.claim_id,
        args.actor_ref,
        args.return_work_id,
        args.return_claim_id,
        args.resolved_blocker_id,
    ):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("Work, claim, actor, and blocker IDs must be bounded")
    if args.work_id == args.return_work_id:
        raise ValueError("dependency Work must differ from the return Work")
    remaining_reason = args.remaining_blocker_reason.strip()
    if not remaining_reason or len(remaining_reason) > 2_000:
        raise ValueError("remaining blocker reason must be bounded and non-empty")
    return_scopes = [_parse_scope(item) for item in args.return_scope]

    store = _open_state_store(root, project)
    before = _read_state_result(store, project["project_id"])
    dependency = next(
        (
            item
            for item in before["snapshot"]["works"]
            if item["work_id"] == args.work_id
        ),
        None,
    )
    return_work = next(
        (
            item
            for item in before["snapshot"]["works"]
            if item["work_id"] == args.return_work_id
        ),
        None,
    )
    if dependency is None or dependency["parent_work_id"] != args.return_work_id:
        raise ValueError("dependency Work is not bound to the requested return Work")
    if return_work is None:
        raise ValueError("return Work was not found")
    if return_work["owner_refs"] != [args.actor_ref]:
        raise ValueError("return Work owner does not match the actor")
    if return_work["scope_refs"] != return_scopes:
        raise ValueError("return scope does not match the suspended Work")
    resolved_blocker = next(
        (
            item
            for item in before["snapshot"]["blockers"]
            if item["blocker_id"] == args.resolved_blocker_id
        ),
        None,
    )
    if (
        resolved_blocker is None
        or args.return_work_id not in resolved_blocker["blocked_work_ids"]
    ):
        raise ValueError("dependency blocker does not protect the return Work")

    completion_output = io.StringIO()
    with contextlib.redirect_stdout(completion_output):
        _work_complete(args)
    completion = json.loads(completion_output.getvalue())

    after_completion = _read_state_result(store, project["project_id"])
    snapshot = after_completion["snapshot"]
    dependency = next(
        item for item in snapshot["works"] if item["work_id"] == args.work_id
    )
    return_work = next(
        item for item in snapshot["works"] if item["work_id"] == args.return_work_id
    )
    resolved_blocker = next(
        item
        for item in snapshot["blockers"]
        if item["blocker_id"] == args.resolved_blocker_id
    )
    if dependency["status"] != "completed":
        raise ValueError("dependency Work did not reach completed state")
    if return_work["status"] not in {"ready", "active"}:
        raise ValueError("return Work is not eligible for reactivation")

    transition_basis = {
        "project_id": project["project_id"],
        "dependency_work_id": args.work_id,
        "return_work_id": args.return_work_id,
        "resolved_blocker_id": args.resolved_blocker_id,
        "remaining_blocker_reason": remaining_reason,
        "evidence_ids": sorted(dependency["evidence_ids"]),
        "remaining_gate_passed": False,
    }
    transition_bytes = json.dumps(
        transition_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    transition_sha256 = hashlib.sha256(transition_bytes).hexdigest()
    transition_ref = _checkpoint_store(root).put_bytes(transition_bytes)
    transition_evidence_id = f"evidence-return-{transition_sha256[:16]}"
    remaining_blocker_id = f"blocker-external-{transition_sha256[:16]}"
    now_text = datetime.now(UTC).isoformat()

    existing_remaining = next(
        (
            item
            for item in snapshot["blockers"]
            if item["blocker_id"] == remaining_blocker_id
        ),
        None,
    )
    transition_is_applied = (
        resolved_blocker["status"] == "resolved"
        and existing_remaining is not None
        and existing_remaining["status"] == "open"
        and remaining_blocker_id in return_work["blocker_ids"]
        and args.resolved_blocker_id not in return_work["blocker_ids"]
    )
    if not transition_is_applied:
        if resolved_blocker["status"] != "open":
            raise ValueError("dependency blocker is not open or already transitioned")
        transition_evidence = {
            "evidence_id": transition_evidence_id,
            "kind": "artifact",
            "artifact_ref": transition_ref.uri,
            "content_sha256": transition_ref.digest,
            "validity": "verified",
            "observed_at": now_text,
            "verified_at": now_text,
        }
        resolved = copy.deepcopy(resolved_blocker)
        resolved["status"] = "resolved"
        resolved["resolved_at"] = now_text
        remaining_blocker = {
            "blocker_id": remaining_blocker_id,
            "status": "open",
            "reason": remaining_reason,
            "blocked_work_ids": [args.return_work_id],
            "evidence_ids": sorted(
                {*dependency["evidence_ids"], transition_evidence_id}
            ),
            "opened_at": now_text,
            "resolved_at": None,
            "supersedes_blocker_id": None,
        }
        returned = copy.deepcopy(return_work)
        returned["blocker_ids"] = sorted(
            {
                *(
                    item
                    for item in returned["blocker_ids"]
                    if item != args.resolved_blocker_id
                ),
                remaining_blocker_id,
            }
        )
        returned["revision"] += 1
        service = _state_service(
            store,
            authorizer=_LocalWorkflowAuthorizer(),
            clock=lambda: now_text,
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        transitioned = service.call_tool(
            COMMIT_TOOL,
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": f"dependency-return-{transition_sha256[:16]}",
                "project_id": project["project_id"],
                "expected_revision": after_completion["revision"],
                "causation_ref": f"completion:{args.work_id}",
                "correlation_ref": f"return:{args.return_work_id}",
                "supersedes_event_id": None,
                "changes": [
                    {
                        "collection": "evidence",
                        "object_id": transition_evidence_id,
                        "value": transition_evidence,
                    },
                    {
                        "collection": "blockers",
                        "object_id": resolved["blocker_id"],
                        "value": resolved,
                    },
                    {
                        "collection": "blockers",
                        "object_id": remaining_blocker_id,
                        "value": remaining_blocker,
                    },
                    {
                        "collection": "works",
                        "object_id": returned["work_id"],
                        "value": returned,
                    },
                ],
            },
            context=RequestContext(args.actor_ref, "local-workflow-approved"),
        )
        if not transitioned["ok"]:
            raise ValueError(transitioned["error"]["message"])

    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    prepared_read = _read_state_result(store, project["project_id"])
    prepared_checkpoint = publish_checkpoint(
        prepared_read,
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    _write_json_atomic(_checkpoint_ref_file(root), prepared_checkpoint.to_document())

    activation_output = io.StringIO()
    activation_args = argparse.Namespace(
        root=str(root),
        work_id=args.return_work_id,
        work_title=return_work["title"],
        owner_ref=args.actor_ref,
        claim_id=args.return_claim_id,
        scope=args.return_scope,
    )
    with contextlib.redirect_stdout(activation_output):
        _work_activate(activation_args)
    activation = json.loads(activation_output.getvalue())

    final_read = _read_state_result(store, project["project_id"])
    final_checkpoint = publish_checkpoint(
        final_read,
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    _write_json_atomic(_checkpoint_ref_file(root), final_checkpoint.to_document())
    final_snapshot = final_read["snapshot"]
    final_remaining = next(
        item
        for item in final_snapshot["blockers"]
        if item["blocker_id"] == remaining_blocker_id
    )
    print(
        json.dumps(
            {
                "status": "dependency-completed-returned",
                "project_id": project["project_id"],
                "revision": final_read["revision"],
                "event_head": final_read["event_head"],
                "completed_work_id": args.work_id,
                "completion_evidence_ids": completion["evidence_ids"],
                "resolved_blocker_id": args.resolved_blocker_id,
                "remaining_blocker_id": remaining_blocker_id,
                "remaining_blocker_reason": final_remaining["reason"],
                "remaining_gate_passed": False,
                "active_work_id": args.return_work_id,
                "claim_id": activation["claim_id"],
                "scope_owners": return_scopes,
                "checkpoint_ref": final_checkpoint.to_document(),
                "next_action": "continue-active-work",
                "completion_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _work_activate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (args.work_id, args.owner_ref, args.claim_id):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("Work, owner, and claim identifiers must be bounded")
    scope_refs = [_parse_scope(item) for item in args.scope]
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    state_store = _open_state_store(root, project)
    snapshot = state_store.read_project(project["project_id"])
    work = next(
        (item for item in snapshot["works"] if item["work_id"] == args.work_id),
        None,
    )
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    identity_matches = work is not None and (
        work["title"] == args.work_title
        and work["owner_refs"] == [args.owner_ref]
        and work["scope_refs"] == scope_refs
    )
    if (
        identity_matches
        and work["status"] == "active"
        and claim is not None
        and claim["status"] == "active"
        and claim["work_id"] == work["work_id"]
        and claim["actor_ref"] == args.owner_ref
    ):
        print(
            json.dumps(
                {
                    "status": "already-active",
                    "project_id": project["project_id"],
                    "work_id": work["work_id"],
                    "claim_id": claim["claim_id"],
                    "revision": snapshot["project"]["revision"],
                },
                sort_keys=True,
            )
        )
        return 0
    if snapshot["project"]["active_work_ids"]:
        raise ValueError("successor activation requires no current active Work")
    if work is not None and (not identity_matches or work["status"] != "ready"):
        raise ValueError("successor Work identity or status conflicts")
    if claim is not None:
        raise ValueError("successor claim identity is already in use")

    now = datetime.now(UTC)
    now_text = now.isoformat()
    service = _state_service(
        state_store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
    )
    context = RequestContext(args.owner_ref, "local-workflow-approved")
    revision = snapshot["project"]["revision"]
    if work is None:
        source_evidence_id = f"evidence-attach-{proposal['proposal_sha256'][:16]}"
        if not any(
            item["evidence_id"] == source_evidence_id
            and item["validity"] == "verified"
            for item in snapshot["evidence"]
        ):
            raise ValueError("current canonical source evidence is not in State")
        work = {
            "work_id": args.work_id,
            "kind": "work",
            "title": args.work_title,
            "status": "ready",
            "parent_work_id": None,
            "dependency_ids": [],
            "owner_refs": [args.owner_ref],
            "scope_refs": copy.deepcopy(scope_refs),
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [source_evidence_id],
            "blocker_ids": [],
            "revision": 0,
        }
        prepared = service.call_tool(
            COMMIT_TOOL,
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": (
                    f"activate-prepare-{args.work_id}-"
                    f"{proposal['proposal_sha256'][:16]}"
                ),
                "project_id": project["project_id"],
                "expected_revision": revision,
                "causation_ref": f"source:{proposal['proposal_sha256']}",
                "correlation_ref": f"work:{args.work_id}",
                "supersedes_event_id": None,
                "changes": [
                    {
                        "collection": "works",
                        "object_id": work["work_id"],
                        "value": work,
                    }
                ],
            },
            context=context,
        )
        if not prepared["ok"]:
            raise ValueError(prepared["error"]["message"])
        revision = prepared["result"]["revision"]

    claimed = service.call_tool(
        CLAIM_TOOL,
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": f"activate-claim-{args.work_id}-{args.claim_id}",
            "project_id": project["project_id"],
            "expected_revision": revision,
            "work_id": args.work_id,
            "claim_id": args.claim_id,
            "scope_owners": copy.deepcopy(scope_refs),
            "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
            "causation_ref": f"source:{proposal['proposal_sha256']}",
            "correlation_ref": f"work:{args.work_id}",
        },
        context=context,
    )
    if not claimed["ok"]:
        raise ValueError(claimed["error"]["message"])
    print(
        json.dumps(
            {
                "status": "activated",
                "project_id": project["project_id"],
                "work_id": args.work_id,
                "claim_id": args.claim_id,
                "revision": claimed["result"]["revision"],
                "event_head": claimed["result"]["event_head"],
            },
            sort_keys=True,
        )
    )
    return 0


def _work_activate_atomic(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (args.work_id, args.owner_ref, args.claim_id):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("Work, owner, and claim identifiers must be bounded")
    scope_refs = [_parse_scope(item) for item in args.scope]
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    snapshot = read_result["snapshot"]
    source_evidence_id = f"evidence-attach-{proposal['proposal_sha256'][:16]}"
    if not any(
        item["evidence_id"] == source_evidence_id
        and item["validity"] == "verified"
        for item in snapshot["evidence"]
    ):
        raise ValueError("current canonical source evidence is not in State")
    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=snapshot["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except CheckpointStaleError:
        checkpoint_ref = publish_checkpoint(
            read_result,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        _write_json_atomic(_checkpoint_ref_file(root), checkpoint_ref.to_document())
    except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
        raise ValueError("idle activation requires a verified checkpoint") from exc

    now = datetime.now(UTC)
    now_text = now.isoformat()
    pending_checkpoint_path = _transition_pending_checkpoint_file(root)

    def publish_and_verify(candidate: dict[str, Any]) -> dict[str, Any]:
        final_ref = publish_checkpoint(
            candidate,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        restore_checkpoint(
            final_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=candidate["revision"],
            expected_event_head=candidate["event_head"],
            expected_governance_ref=candidate["snapshot"]["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=candidate["registry_digest"],
        )
        _write_json_atomic(pending_checkpoint_path, final_ref.to_document())
        return final_ref.to_document()

    request_basis = {
        "project_id": project["project_id"],
        "revision": read_result["revision"],
        "work_id": args.work_id,
        "work_title": args.work_title,
        "owner_ref": args.owner_ref,
        "claim_id": args.claim_id,
        "scope_owners": scope_refs,
        "source_proposal_sha256": proposal["proposal_sha256"],
        "checkpoint_digest": checkpoint_ref.digest,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(
            request_basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    service = _state_service(
        state_store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
        transition_checkpoint_publisher=publish_and_verify,
    )
    response = service.call_tool(
        LOCAL_WORK_ACTIVATION_TOOL,
        {
            "schema_version": LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION,
            "request_id": f"activate-{request_sha256}",
            "project_id": project["project_id"],
            "expected_revision": read_result["revision"],
            "work_id": args.work_id,
            "work_title": args.work_title,
            "owner_ref": args.owner_ref,
            "claim_id": args.claim_id,
            "scope_owners": copy.deepcopy(scope_refs),
            "source_evidence_id": source_evidence_id,
            "source_proposal_sha256": proposal["proposal_sha256"],
            "checkpoint_ref": checkpoint_ref.to_document(),
            "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
            "causation_ref": f"source:{proposal['proposal_sha256']}",
            "correlation_ref": f"work:{args.work_id}",
        },
        context=RequestContext(args.owner_ref, "local-workflow-approved"),
    )
    if not response["ok"]:
        if pending_checkpoint_path.exists():
            pending_checkpoint_path.unlink()
        marker = response["error"]["message"]
        gate = marker.removeprefix("activation_gate:")
        print(
            json.dumps(
                {
                    "status": "denied",
                    "project_id": project["project_id"],
                    "failed_gate": gate,
                    "error_code": response["error"]["code"],
                    "revision": read_result["revision"],
                    "event_head": read_result["event_head"],
                    "state_changed": False,
                    "next_action": f"satisfy-gate:{gate}",
                },
                sort_keys=True,
            )
        )
        return 2
    result = response["result"]
    final_checkpoint = ArtifactRef.from_document(result["checkpoint_ref"])
    if pending_checkpoint_path.exists():
        os.replace(pending_checkpoint_path, _checkpoint_ref_file(root))
    else:
        _write_json_atomic(_checkpoint_ref_file(root), final_checkpoint.to_document())
    print(
        json.dumps(
            {
                "status": (
                    "already-active" if result["already_activated"] else "activated"
                ),
                "project_id": project["project_id"],
                "work_id": args.work_id,
                "claim_id": args.claim_id,
                "revision": result["revision"],
                "event_head": result["event_head"],
                "checkpoint_ref": final_checkpoint.to_document(),
                "checkpoint_verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _work_recover(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    for value in (args.claim_id, args.actor_ref):
        if _STATE_ID_RE.fullmatch(value) is None:
            raise ValueError("claim and actor identifiers must be bounded")
    if args.action == "reclaim" and _STATE_ID_RE.fullmatch(args.new_claim_id) is None:
        raise ValueError("new claim identifier must be bounded")
    proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=True,
    )
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    snapshot = read_result["snapshot"]
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    if claim is None:
        raise ValueError("claim was not found")
    if args.action == "reclaim" and claim["status"] == "expired":
        successor = next(
            (
                item
                for item in snapshot["claims"]
                if item["claim_id"] == args.new_claim_id
            ),
            None,
        )
        replay_matches = (
            successor is not None
            and successor["status"] == "active"
            and claim["actor_ref"] == args.actor_ref
            and successor["actor_ref"] == args.actor_ref
            and successor["work_id"] == claim["work_id"]
            and successor["scope_owners"] == claim["scope_owners"]
            and successor["claimed_at"] == claim["released_at"]
            and snapshot["project"]["primary_work_id"] == successor["work_id"]
        )
        if not replay_matches:
            raise ValueError("reclaim replay does not match the active successor claim")
        try:
            checkpoint_ref = ArtifactRef.from_document(
                json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
            )
            restore_checkpoint(
                checkpoint_ref,
                _checkpoint_store(root),
                expected_project_id=project["project_id"],
                expected_revision=read_result["revision"],
                expected_event_head=read_result["event_head"],
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=_canonical_master_sha256(proposal),
                expected_registry_digest=read_result["registry_digest"],
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
            raise ValueError("reclaim replay requires the verified current checkpoint") from exc
        print(
            json.dumps(
                {
                    "status": "already-reclaimed",
                    "project_id": project["project_id"],
                    "claim_id": successor["claim_id"],
                    "revision": read_result["revision"],
                    "event_head": read_result["event_head"],
                    "lease_expires_at": successor["lease_expires_at"],
                    "checkpoint_ref": checkpoint_ref.to_document(),
                    "checkpoint_verified": True,
                },
                sort_keys=True,
            )
        )
        return 0
    now_text = datetime.now(UTC).isoformat()
    pending_checkpoint_path = _transition_pending_checkpoint_file(root)

    def publish_and_verify(candidate: dict[str, Any]) -> dict[str, Any]:
        checkpoint_ref = publish_checkpoint(
            candidate,
            _checkpoint_store(root),
            canonical_plan_sha256=_canonical_master_sha256(proposal),
        )
        restore_checkpoint(
            checkpoint_ref,
            _checkpoint_store(root),
            expected_project_id=project["project_id"],
            expected_revision=candidate["revision"],
            expected_event_head=candidate["event_head"],
            expected_governance_ref=candidate["snapshot"]["project"]["governance_ref"],
            expected_plan_sha256=_canonical_master_sha256(proposal),
            expected_registry_digest=candidate["registry_digest"],
        )
        _write_json_atomic(pending_checkpoint_path, checkpoint_ref.to_document())
        return checkpoint_ref.to_document()

    service = _state_service(
        state_store,
        authorizer=_LocalWorkflowAuthorizer(),
        clock=lambda: now_text,
        event_id_factory=lambda request_id: f"event-{request_id}",
        transition_checkpoint_publisher=publish_and_verify,
    )
    request = {
        "schema_version": LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
        "request_id": (
            f"{args.action}-{args.claim_id}-r{read_result['revision']}-"
            f"ttl{args.lease_ttl_ms}-{args.new_claim_id or 'live'}"
        ),
        "project_id": project["project_id"],
        "action": args.action,
        "expected_revision": read_result["revision"],
        "claim_id": args.claim_id,
        "new_claim_id": args.new_claim_id if args.action == "reclaim" else None,
        "actor_ref": args.actor_ref,
        "scope_owners": copy.deepcopy(claim["scope_owners"]),
        "lease_ttl_ms": args.lease_ttl_ms,
        "causation_ref": f"recovery:{args.claim_id}",
        "correlation_ref": f"project:{project['project_id']}",
    }
    response = service.call_tool(
        LOCAL_CLAIM_RECOVERY_TOOL,
        request,
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
    )
    if not response["ok"]:
        pending_checkpoint_path.unlink(missing_ok=True)
        raise ValueError(response["error"]["message"])
    result = response["result"]
    checkpoint_ref = ArtifactRef.from_document(result["checkpoint_ref"])
    if pending_checkpoint_path.exists():
        os.replace(pending_checkpoint_path, _checkpoint_ref_file(root))
    else:
        _write_json_atomic(_checkpoint_ref_file(root), checkpoint_ref.to_document())
    print(
        json.dumps(
            {
                "status": args.action,
                "project_id": project["project_id"],
                "claim_id": result["claim"]["claim_id"],
                "revision": result["revision"],
                "event_head": result["event_head"],
                "lease_expires_at": result["claim"]["lease_expires_at"],
                "checkpoint_ref": checkpoint_ref.to_document(),
                "checkpoint_verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _export_state(args: argparse.Namespace) -> int:
    try:
        receipt = export_local_state(
            Path(args.root),
            Path(args.output),
        )
    except LocalStateBundleError as exc:
        raise ValueError(str(exc)) from exc
    print(json.dumps({"status": "exported", **receipt}, sort_keys=True))
    return 0


def _import_state(args: argparse.Namespace) -> int:
    try:
        receipt = import_local_state(
            Path(args.root),
            Path(args.bundle),
            replace=args.replace,
        )
    except LocalStateBundleError as exc:
        raise ValueError(str(exc)) from exc
    print(json.dumps({"status": "imported", **receipt}, sort_keys=True))
    return 0


def _rollback_state(args: argparse.Namespace) -> int:
    try:
        receipt = rollback_local_state(Path(args.root))
    except LocalStateBundleError as exc:
        raise ValueError(str(exc)) from exc
    print(json.dumps({"status": "rolled-back", **receipt}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="continuity")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize a local project profile")
    init.add_argument("--root", default=".")
    init.add_argument("--project-id", required=True)
    init.add_argument("--display-name")
    init.set_defaults(handler=_init)
    verify = commands.add_parser("verify", help="verify the local project profile")
    verify.add_argument("--root", default=".")
    verify.set_defaults(handler=_verify)
    doctor = commands.add_parser("doctor", help="check local runtime prerequisites")
    doctor.add_argument("--root", default=".")
    doctor.set_defaults(handler=_doctor)
    state = commands.add_parser("state", help="read local authoritative state")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    show = state_commands.add_parser("show", help="show the current typed snapshot")
    show.add_argument("--root", default=".")
    show.set_defaults(handler=_state_show)
    status = commands.add_parser("status", help="render current status projections")
    status_commands = status.add_subparsers(dest="status_command", required=True)
    status_render = status_commands.add_parser("render", help="render current bilingual STATUS")
    status_render.add_argument("--root", default=".")
    status_render.set_defaults(handler=_status_render)
    attach = commands.add_parser("attach", help="attach existing governance documents")
    attach_commands = attach.add_subparsers(dest="attach_command", required=True)
    plan = attach_commands.add_parser("plan", help="create a candidate import proposal")
    plan.add_argument("--root", default=".")
    plan.add_argument("--master", required=True)
    plan.add_argument("--status", required=True)
    plan.add_argument("--work-id", required=True)
    plan.add_argument("--work-title", required=True)
    plan.add_argument("--owner-ref", required=True)
    plan.add_argument("--scope", action="append", required=True)
    plan.set_defaults(handler=_attach_plan)
    refresh = attach_commands.add_parser(
        "refresh", help="rebind a proposal to current governance sources"
    )
    refresh.add_argument("--root", default=".")
    refresh.add_argument("--proposal", default=".continuity/attach-proposal.json")
    refresh.set_defaults(handler=_attach_refresh)
    approve = attach_commands.add_parser("approve", help="approve and activate a proposal")
    approve.add_argument("--root", default=".")
    approve.add_argument("--proposal", default=".continuity/attach-proposal.json")
    approve.add_argument("--actor-ref", required=True)
    approve.add_argument("--claim-id", required=True)
    approve.set_defaults(handler=_attach_approve)
    resume = commands.add_parser("resume", help="compose the current bounded resume packet")
    resume.add_argument("--root", default=".")
    resume.add_argument("--interaction-cursor", default=None)
    resume.add_argument("--skill-lock", default=None)
    resume.set_defaults(handler=_resume)
    checkpoint = commands.add_parser(
        "checkpoint", help="create or verify an immutable local checkpoint"
    )
    checkpoint_commands = checkpoint.add_subparsers(
        dest="checkpoint_command", required=True
    )
    checkpoint_create = checkpoint_commands.add_parser(
        "create", help="publish the current State as immutable artifacts"
    )
    checkpoint_create.add_argument("--root", default=".")
    checkpoint_create.set_defaults(handler=_checkpoint_create)
    checkpoint_verify = checkpoint_commands.add_parser(
        "verify", help="verify the latest checkpoint against current authority"
    )
    checkpoint_verify.add_argument("--root", default=".")
    checkpoint_verify.set_defaults(handler=_checkpoint_verify)
    work = commands.add_parser("work", help="transition claimed Work")
    work_commands = work.add_subparsers(dest="work_command", required=True)
    work_complete = work_commands.add_parser(
        "complete", help="atomically complete Work and release its claim"
    )
    work_complete.add_argument("--root", default=".")
    work_complete.add_argument("--work-id", required=True)
    work_complete.add_argument("--claim-id", required=True)
    work_complete.add_argument("--actor-ref", required=True)
    work_complete.add_argument("--evidence-file", action="append", required=True)
    work_complete.set_defaults(handler=_work_complete)
    work_transition = work_commands.add_parser(
        "transition",
        help="atomically complete a dependency and claim its declared return point",
    )
    work_transition.add_argument("--root", default=".")
    work_transition.add_argument("--work-id", required=True)
    work_transition.add_argument("--claim-id", required=True)
    work_transition.add_argument("--actor-ref", required=True)
    work_transition.add_argument("--return-work-id", required=True)
    work_transition.add_argument("--successor-claim-id", required=True)
    work_transition.add_argument("--successor-scope", action="append", required=True)
    work_transition.add_argument("--resolved-blocker-id", required=True)
    work_transition.add_argument("--remaining-blocker-id", default=None)
    work_transition.add_argument("--remaining-blocker-reason", default=None)
    work_transition.add_argument("--workspace-root", required=True)
    work_transition.add_argument("--expected-head", required=True)
    work_transition.add_argument("--expected-ref", default=None)
    work_transition.add_argument("--evidence-file", action="append", required=True)
    work_transition.set_defaults(handler=_work_transition)
    work_suspend_dependency = work_commands.add_parser(
        "suspend-dependency",
        help="suspend incomplete Work and activate a checkpoint-bound prerequisite",
    )
    work_suspend_dependency.add_argument("--root", default=".")
    work_suspend_dependency.add_argument("--work-id", required=True)
    work_suspend_dependency.add_argument("--claim-id", required=True)
    work_suspend_dependency.add_argument("--actor-ref", required=True)
    work_suspend_dependency.add_argument("--dependency-work-id", required=True)
    work_suspend_dependency.add_argument("--dependency-work-title", required=True)
    work_suspend_dependency.add_argument(
        "--dependency-scope", action="append", required=True
    )
    work_suspend_dependency.add_argument("--reason", required=True)
    work_suspend_dependency.set_defaults(handler=_work_suspend_dependency)
    work_activate = work_commands.add_parser(
        "activate", help="add and claim the next source-bound Work"
    )
    work_activate.add_argument("--root", default=".")
    work_activate.add_argument("--work-id", required=True)
    work_activate.add_argument("--work-title", required=True)
    work_activate.add_argument("--owner-ref", required=True)
    work_activate.add_argument("--claim-id", required=True)
    work_activate.add_argument("--scope", action="append", required=True)
    work_activate.set_defaults(handler=_work_activate_atomic)
    work_recover = work_commands.add_parser(
        "recover", help="heartbeat or reclaim a local legacy claim"
    )
    work_recover.add_argument("action", choices=("heartbeat", "reclaim"))
    work_recover.add_argument("--root", default=".")
    work_recover.add_argument("--claim-id", required=True)
    work_recover.add_argument("--actor-ref", required=True)
    work_recover.add_argument("--new-claim-id", default=None)
    work_recover.add_argument("--lease-ttl-ms", type=int, default=8 * 60 * 60 * 1000)
    work_recover.set_defaults(handler=_work_recover)
    export = commands.add_parser("export", help="export local-embedded State")
    export.add_argument("--root", default=".")
    export.add_argument("--output", required=True)
    export.set_defaults(handler=_export_state)
    import_command = commands.add_parser(
        "import", help="import a verified local-embedded State bundle"
    )
    import_command.add_argument("--root", default=".")
    import_command.add_argument("--bundle", required=True)
    import_command.add_argument("--replace", action="store_true")
    import_command.set_defaults(handler=_import_state)
    rollback = commands.add_parser(
        "rollback", help="swap to the previous verified local State bundle"
    )
    rollback.add_argument("--root", default=".")
    rollback.set_defaults(handler=_rollback_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
