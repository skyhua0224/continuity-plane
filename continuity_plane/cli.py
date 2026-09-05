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
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .artifact_store import ArtifactRef, LocalArtifactStore
from .bounded_code_search import bounded_git_search
from .code_index import build_code_index, lookup_code_index
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
from .light_observability import build_observation_report
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
from .workspace_binding import (
    WorkspaceBindingError,
    register_control_root,
    resolve_control_root,
)
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

VERSION = "0.1.0a12"
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
_SOURCE_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s]{1,960}$")
_DELIVERY_EFFECTS = {
    "source-control.local",
    "source-control.history-rewrite",
    "source-control.push",
    "source-control.pr",
    "source-control.merge",
    "source-control.release",
    "deployment.deploy",
    "remote-effect.install-verification",
    "package-publish.publish",
}
_DELIVERY_WORKSPACE_REGISTRY_SCHEMA_VERSION = (
    "context.delivery-workspace-registry/v1alpha1"
)


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
    activation_source_validator: Any | None = None,
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
        activation_source_validator=activation_source_validator,
    )


def _initialize_new_project(args: argparse.Namespace) -> int:
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
    created_at = datetime.now(UTC).isoformat()
    proposal = build_attach_proposal(
        root=root,
        project_id=args.project_id,
        master_path=".continuity/MASTER.md",
        status_path=".continuity/STATUS.md",
        work_id="work-initial",
        work_title="Define the first measurable outcome",
        owner_ref="local-user",
        scope_refs=[
            {"scope_kind": "capability", "scope_ref": "project-governance"}
        ],
        created_at=created_at,
    )
    _write_json_atomic(target / "attach-proposal.json", proposal)

    evidence = _build_attach_evidence(proposal, observed_at=created_at)
    initial_work = copy.deepcopy(_initial_state(args.project_id)["works"][0])
    initial_work["evidence_ids"] = [evidence["evidence_id"]]
    initial_work["revision"] = 1
    service = _state_service(
        store,
        authorizer=_LocalAttachAuthorizer(),
        event_id_factory=lambda request_id: f"event-{request_id}",
    )
    request_id = f"init-{proposal['proposal_sha256'][:16]}"
    committed = service.call_tool(
        COMMIT_TOOL,
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": request_id,
            "project_id": args.project_id,
            "expected_revision": 0,
            "causation_ref": f"init:{proposal['proposal_sha256']}",
            "correlation_ref": f"project:{args.project_id}",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "evidence",
                    "object_id": evidence["evidence_id"],
                    "value": evidence,
                },
                {
                    "collection": "works",
                    "object_id": initial_work["work_id"],
                    "value": initial_work,
                },
            ],
        },
        context=RequestContext("local-user", "local-attach-approved"),
    )
    if not committed["ok"]:
        raise ValueError(committed["error"]["message"])
    read_result = _read_state_result(store, args.project_id)
    checkpoint_ref = publish_checkpoint(
        read_result,
        _checkpoint_store(root),
        canonical_plan_sha256=_canonical_master_sha256(proposal),
    )
    _write_json_atomic(_checkpoint_ref_file(root), checkpoint_ref.to_document())
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


def _init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    target = root / ".continuity"
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        existing = next(iter(target.iterdir()), target) if target.is_dir() else target
        raise FileExistsError(
            f"initialization would overwrite: {existing}"
        ) from exc
    try:
        return _initialize_new_project(args)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


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


def _latest_codex_session_start(codex_home: Path) -> dict[str, Any] | None:
    candidates = sorted(
        codex_home.glob("plugins/data/continuity-plane*/live-events/*.jsonl"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 64 * 1024))
                lines = stream.read().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict) and event.get("event_type") == "session-start":
                return event
    return None


def _codex_plugin_status(codex_home: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        config = {}
    plugins = config.get("plugins")
    plugins = plugins if isinstance(plugins, dict) else {}

    def enabled(plugin_id: str) -> bool:
        value = plugins.get(plugin_id)
        return isinstance(value, dict) and value.get("enabled") is True

    def mcp_auto_approved(plugin_id: str, server_id: str) -> bool:
        plugin = plugins.get(plugin_id)
        plugin = plugin if isinstance(plugin, dict) else {}
        servers = plugin.get("mcp_servers")
        servers = servers if isinstance(servers, dict) else {}
        server = servers.get(server_id)
        server = server if isinstance(server, dict) else {}
        return (
            server.get("enabled") is True
            and server.get("default_tools_approval_mode") == "approve"
        )

    state_id = "continuity-plane-state@continuity-plane"
    search_id = "continuity-plane-search@continuity-plane"
    state_mcp_auto_approved = mcp_auto_approved(state_id, "continuity")
    search_mcp_auto_approved = mcp_auto_approved(
        search_id, "continuity-search"
    )
    hooks = config.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    hook_state = hooks.get("state")
    hook_state = hook_state if isinstance(hook_state, dict) else {}
    trusted_hooks = sum(
        isinstance(key, str)
        and key.startswith("continuity-plane@continuity-plane:")
        and isinstance(value, dict)
        and isinstance(value.get("trusted_hash"), str)
        for key, value in hook_state.items()
    )
    event = _latest_codex_session_start(codex_home)
    session_start_observed = event is not None and event.get("plugin_loaded") is True
    core_enabled = enabled("continuity-plane@continuity-plane")
    state_enabled = enabled(state_id)
    search_enabled = enabled(search_id)
    configured = (
        core_enabled
        and (not state_enabled or state_mcp_auto_approved)
        and (not search_enabled or search_mcp_auto_approved)
    )
    ready = configured and trusted_hooks >= 3
    status = "active" if ready and session_start_observed else "configured" if ready else "misconfigured"
    return {
        "status": status,
        "core_enabled": core_enabled,
        "search_enabled": search_enabled,
        "state_enabled": state_enabled,
        "mcp_auto_approved": state_mcp_auto_approved,
        "search_mcp_auto_approved": search_mcp_auto_approved,
        "trusted_hooks": trusted_hooks,
        "expected_hooks": 3,
        "session_start_observed": session_start_observed,
        "last_session_start_success": (
            event.get("success") is True if event is not None else None
        ),
        "last_observed_at": event.get("observed_at") if event is not None else None,
    }


def _doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    _open_state_store(root, project)
    sqlite_version = sqlite3.sqlite_version
    response = {
        "status": "ready",
        "project_id": project["project_id"],
        "python": sys.version.split()[0],
        "sqlite": sqlite_version,
        "runtime_profile": project["runtime_profile"],
        "external_services_required": 0,
    }
    if args.codex_home is not None:
        response["codex_plugin"] = _codex_plugin_status(
            Path(args.codex_home).expanduser().resolve()
        )
    print(json.dumps(response, sort_keys=True))
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


def _context_search(args: argparse.Namespace) -> int:
    receipt = bounded_git_search(
        Path(args.root),
        query=args.query,
        max_results=args.max_results,
        max_output_bytes=args.max_output_bytes,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _context_index(args: argparse.Namespace) -> int:
    receipt = build_code_index(
        Path(args.root),
        cache_path=args.cache_path,
        max_files=args.max_files,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _context_lookup(args: argparse.Namespace) -> int:
    receipt = lookup_code_index(
        Path(args.root),
        query=args.query,
        cache_path=args.cache_path,
        max_results=args.max_results,
        max_output_bytes=args.max_output_bytes,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def _status_projection_document(
    root: Path, packet: dict[str, Any], status_text: str, status_en_text: str
) -> dict[str, Any]:
    projection = {
        "schema_version": "context.status-projection/v1alpha1",
        "project_id": packet["project_id"],
        "revision": packet["revision"],
        "source_packet_sha256": packet["packet_sha256"],
        "status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        "status_en_sha256": hashlib.sha256(status_en_text.encode("utf-8")).hexdigest(),
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
    return projection


def _status_projection_is_current(
    root: Path, expected: dict[str, Any], status_text: str, status_en_text: str
) -> bool:
    try:
        current = json.loads(
            (root / ".continuity/status-projection.json").read_text(encoding="utf-8")
        )
        actual_status = (root / ".continuity/STATUS.current.md").read_text(
            encoding="utf-8"
        )
        actual_status_en = (root / ".continuity/STATUS.current.en.md").read_text(
            encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError):
        return False
    return current == expected and actual_status == status_text and actual_status_en == status_en_text


def _ensure_status_projection(root: Path, packet: dict[str, Any]) -> dict[str, Any]:
    status_text = render_status_projection(packet, language="zh-CN")
    status_en_text = render_status_projection(packet, language="en")
    projection = _status_projection_document(root, packet, status_text, status_en_text)
    if not _status_projection_is_current(root, projection, status_text, status_en_text):
        _write_text_atomic(root / ".continuity/STATUS.current.md", status_text)
        _write_text_atomic(root / ".continuity/STATUS.current.en.md", status_en_text)
        _write_json_atomic(root / ".continuity/status-projection.json", projection)
        if not _status_projection_is_current(
            root, projection, status_text, status_en_text
        ):
            raise ValueError("status projection refresh could not be verified")
    return projection


def _status_render(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    with contextlib.redirect_stdout(io.StringIO()):
        _resume(argparse.Namespace(root=str(root), interaction_cursor=None, skill_lock=None))
    packet = json.loads(
        (root / ".continuity/resume-packet.json").read_text(encoding="utf-8")
    )
    _ensure_status_projection(root, packet)
    status_path = root / ".continuity/STATUS.current.md"
    status_en_path = root / ".continuity/STATUS.current.en.md"
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


def _attach_source_digest(proposal: dict[str, Any]) -> str:
    return hashlib.sha256(
        "".join(source["content_sha256"] for source in proposal["sources"]).encode()
    ).hexdigest()


def _build_attach_evidence(
    proposal: dict[str, Any], *, observed_at: str | None = None
) -> dict[str, Any]:
    source_digest = _attach_source_digest(proposal)
    timestamp = observed_at or datetime.now(UTC).isoformat()
    return {
        "evidence_id": f"evidence-attach-{proposal['proposal_sha256'][:16]}",
        "kind": "artifact",
        "artifact_ref": f"artifact://sha256/{source_digest}",
        "content_sha256": source_digest,
        "validity": "verified",
        "observed_at": timestamp,
        "verified_at": timestamp,
    }


def _verified_attach_evidence(
    snapshot: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    source_digest = _attach_source_digest(proposal)
    candidates = sorted(
        (
            item
            for item in snapshot["evidence"]
            if item["evidence_id"].startswith("evidence-attach-")
            and item["kind"] == "artifact"
            and item["content_sha256"] == source_digest
            and item["validity"] == "verified"
        ),
        key=lambda item: item["evidence_id"],
    )
    if not candidates:
        raise ValueError("current canonical source evidence is not in State")
    return candidates[0]


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


def _delivery_workspace_registry_file(root: Path) -> Path:
    return root / ".continuity/local/delivery-workspaces.json"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _delivery_registry_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in document.items()
                if key != "registry_sha256"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _git_common_dir_sha256(root: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError("transition_gate:workspace_git")
    return hashlib.sha256(
        str(Path(completed.stdout.strip()).resolve()).encode("utf-8")
    ).hexdigest()


def _validate_delivery_workspace_registry(
    root: Path,
    project: dict[str, Any],
    document: Any,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "project_id",
        "project_profile_sha256",
        "workspaces",
        "registry_sha256",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version")
        != _DELIVERY_WORKSPACE_REGISTRY_SCHEMA_VERSION
        or document.get("project_id") != project["project_id"]
        or document.get("project_profile_sha256") != _file_sha256(_project_file(root))
        or document.get("registry_sha256") != _delivery_registry_digest(document)
        or not isinstance(document.get("workspaces"), list)
    ):
        raise ValueError("transition_gate:workspace_registry")
    seen: set[str] = set()
    for item in document["workspaces"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "workspace_id",
                "workspace_root",
                "repository_sha256",
                "allowed_effects",
            }
            or not isinstance(item.get("workspace_id"), str)
            or _ID_RE.fullmatch(item["workspace_id"]) is None
            or item["workspace_id"] in seen
            or not isinstance(item.get("workspace_root"), str)
            or not Path(item["workspace_root"]).is_absolute()
            or not isinstance(item.get("repository_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["repository_sha256"]) is None
            or not isinstance(item.get("allowed_effects"), list)
            or not item["allowed_effects"]
            or item["allowed_effects"] != sorted(set(item["allowed_effects"]))
            or not set(item["allowed_effects"]) <= _DELIVERY_EFFECTS
        ):
            raise ValueError("transition_gate:workspace_registry")
        seen.add(item["workspace_id"])
    if document["workspaces"] != sorted(
        document["workspaces"], key=lambda item: item["workspace_id"]
    ):
        raise ValueError("transition_gate:workspace_registry")
    return document


def _load_delivery_workspace_registry(
    root: Path,
    project: dict[str, Any],
) -> dict[str, Any]:
    path = _delivery_workspace_registry_file(root)
    try:
        if path.is_symlink():
            raise ValueError("transition_gate:workspace_registry")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("transition_gate:workspace_registry") from exc
    return _validate_delivery_workspace_registry(root, project, document)


def _workspace_register(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    project = _load_project(root)
    if _ID_RE.fullmatch(args.workspace_id) is None:
        raise ValueError("workspace identifier is invalid")
    allowed_effects = sorted(set(args.allow_effect))
    if (
        len(allowed_effects) != len(args.allow_effect)
        or not allowed_effects
        or not set(allowed_effects) <= _DELIVERY_EFFECTS
    ):
        raise ValueError("workspace allowed effects are invalid")
    workspace = Path(args.workspace_root).resolve()
    if not workspace.is_dir():
        raise ValueError("transition_gate:workspace_identity")
    project_repository = _git_common_dir_sha256(root)
    workspace_repository = _git_common_dir_sha256(workspace)
    if project_repository == workspace_repository:
        raise ValueError("transition_gate:workspace_repository")
    foreign_profile = workspace / ".continuity/project.yaml"
    if foreign_profile.is_file():
        try:
            foreign = yaml.safe_load(foreign_profile.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("transition_gate:workspace_registry") from exc
        if not isinstance(foreign, dict) or foreign.get("project_id") != project["project_id"]:
            raise ValueError("transition_gate:workspace_foreign_authority")
    path = _delivery_workspace_registry_file(root)
    if path.exists():
        registry = _load_delivery_workspace_registry(root, project)
    else:
        registry = {
            "schema_version": _DELIVERY_WORKSPACE_REGISTRY_SCHEMA_VERSION,
            "project_id": project["project_id"],
            "project_profile_sha256": _file_sha256(_project_file(root)),
            "workspaces": [],
            "registry_sha256": "",
        }
    existing = next(
        (
            item
            for item in registry["workspaces"]
            if item["workspace_id"] == args.workspace_id
        ),
        None,
    )
    entry = {
        "workspace_id": args.workspace_id,
        "workspace_root": str(workspace),
        "repository_sha256": workspace_repository,
        "allowed_effects": allowed_effects,
    }
    if existing is not None and existing != entry:
        raise ValueError("transition_gate:workspace_registration_conflict")
    if existing is None:
        registry["workspaces"].append(entry)
        registry["workspaces"].sort(key=lambda item: item["workspace_id"])
    registry["registry_sha256"] = _delivery_registry_digest(registry)
    _validate_delivery_workspace_registry(root, project, registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, registry)
    path.chmod(0o600)
    print(
        json.dumps(
            {
                "status": "already-registered" if existing is not None else "registered",
                "project_id": project["project_id"],
                "workspace_id": args.workspace_id,
                "repository_sha256": workspace_repository,
                "allowed_effects": allowed_effects,
                "state_write_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


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


def _refreshed_attach_proposal(
    root: Path,
    project_id: str,
    old: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    paths = {item["kind"]: item["path"] for item in old["sources"]}
    refreshed = build_attach_proposal(
        root=root,
        project_id=project_id,
        master_path=paths["master"],
        status_path=paths["status"],
        work_id=old["work"]["work_id"],
        work_title=old["work"]["title"],
        owner_ref=old["work"]["owner_ref"],
        scope_refs=copy.deepcopy(old["work"]["scope_refs"]),
        created_at=datetime.now(UTC).isoformat(),
    )
    changed = sorted(
        source["kind"]
        for source in refreshed["sources"]
        for previous in old["sources"]
        if source["kind"] == previous["kind"]
        and source["content_sha256"] != previous["content_sha256"]
    )
    return refreshed, changed


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
    refreshed, changed = _refreshed_attach_proposal(
        root, project["project_id"], old
    )
    if not changed:
        print(
            json.dumps(
                {
                    "status": "unchanged",
                    "project_id": project["project_id"],
                    "old_proposal_sha256": old["proposal_sha256"],
                    "proposal_sha256": old["proposal_sha256"],
                    "changed_sources": [],
                    "state_rebind_required": False,
                    "state_write_authority": False,
                },
                sort_keys=True,
            )
        )
        return 0
    _write_json_atomic(proposal_path, refreshed)
    print(
        json.dumps(
            {
                "status": "refreshed",
                "project_id": project["project_id"],
                "old_proposal_sha256": old["proposal_sha256"],
                "proposal_sha256": refreshed["proposal_sha256"],
                "changed_sources": changed,
                "state_rebind_required": True,
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
    source_digest = _attach_source_digest(proposal)
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
    initial_candidates = [
        item for item in current["works"] if item["work_id"] == "work-initial"
    ]
    unattached_baseline = (
        current["project"]["revision"] in {0, 1}
        and current["project"]["active_work_ids"] == []
        and current["project"]["primary_work_id"] is None
        and current["claims"] == []
        and len(current["works"]) == 1
        and len(initial_candidates) == 1
        and initial_candidates[0]["status"] == "proposed"
    )
    if not unattached_baseline:
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
    initial_work["revision"] += 1
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
        "expected_revision": current["project"]["revision"],
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
        "expected_revision": committed["result"]["revision"],
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
    persist = getattr(args, "persist", True)
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
        if not persist:
            raise ValueError("inspect requires a current verified checkpoint") from exc
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
    if idle and not source_fresh:
        next_action = "rebind-source-and-activate-next-work"
    elif not source_fresh or not lease_valid:
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
    if persist:
        path = root / ".continuity/resume-packet.json"
        _write_json_atomic(path, packet)
        try:
            _ensure_status_projection(root, packet)
        except (OSError, ValueError) as exc:
            raise ValueError("status projection refresh failed") from exc
    print(json.dumps(packet, ensure_ascii=False, sort_keys=True))
    return 0


def _inspect(args: argparse.Namespace) -> int:
    return _resume(
        argparse.Namespace(
            root=args.root,
            interaction_cursor=None,
            skill_lock=None,
            persist=False,
        )
    )


def _capture_json_handler(handler: Any, args: argparse.Namespace) -> dict[str, Any]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = handler(args)
    if result not in (None, 0):
        raise ValueError("continuation operation failed")
    try:
        document = json.loads(output.getvalue().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("continuation operation returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("continuation operation returned an invalid document")
    return document


def _autorun_ledger(root: Path) -> sqlite3.Connection:
    path = root / ".continuity/autorun.sqlite3"
    connection = sqlite3.connect(path, timeout=2.0)
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS continuation_runs (
            session_sha256 TEXT NOT NULL,
            project_root_sha256 TEXT NOT NULL,
            checkpoint_digest TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            next_action TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (session_sha256, project_root_sha256, checkpoint_digest, claim_id)
        )
        """
    )
    return connection


def _autorun_resume_packet(root: Path) -> dict[str, Any]:
    return _capture_json_handler(
        _resume,
        argparse.Namespace(root=str(root), interaction_cursor=None, skill_lock=None),
    )


def _autorun_reclaim_id(session_id: str, claim_id: str, checkpoint_digest: str) -> str:
    digest = hashlib.sha256(
        f"{session_id}:{claim_id}:{checkpoint_digest}".encode("utf-8")
    ).hexdigest()
    return f"autorun-reclaim-{digest[:40]}"


def _autorun_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _autorun(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    session_id = args.session_id
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
        raise ValueError("session-id must be a bounded non-empty string")
    if args.actor_ref is not None and _STATE_ID_RE.fullmatch(args.actor_ref) is None:
        raise ValueError("actor-ref must be a bounded identifier")
    if args.claim_id is not None and _STATE_ID_RE.fullmatch(args.claim_id) is None:
        raise ValueError("claim-id must be a bounded identifier")
    if type(args.max_attempts) is not int or not 1 <= args.max_attempts <= 5:
        raise ValueError("max-attempts must be between 1 and 5")
    if type(args.heartbeat_window_ms) is not int or not 0 <= args.heartbeat_window_ms <= 86_400_000:
        raise ValueError("heartbeat-window-ms is outside the allowed range")

    packet: dict[str, Any] | None = None
    recovery_receipts: list[dict[str, Any]] = []
    for _ in range(args.max_attempts):
        try:
            packet = _autorun_resume_packet(root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                json.dumps(
                    {
                        "status": "paused",
                        "project_id": None,
                        "failed_gate": "resume",
                        "error": str(exc),
                        "state_event_created": False,
                    },
                    sort_keys=True,
                )
            )
            return 2
        active = packet.get("active_work")
        claim = packet.get("claim")
        checkpoint = packet.get("checkpoint_ref")
        if not isinstance(active, dict) or not isinstance(claim, dict):
            print(
                json.dumps(
                    {
                        "status": "paused",
                        "project_id": packet.get("project_id"),
                        "failed_gate": "active_work",
                        "next_action": packet.get("next_action"),
                        "state_event_created": False,
                    },
                    sort_keys=True,
                )
            )
            return 2
        if args.actor_ref is not None and claim.get("actor_ref") != args.actor_ref:
            print(json.dumps({"status": "paused", "failed_gate": "actor_ref", "state_event_created": False}, sort_keys=True))
            return 2
        if args.claim_id is not None and claim.get("claim_id") != args.claim_id:
            print(json.dumps({"status": "paused", "failed_gate": "claim_id", "state_event_created": False}, sort_keys=True))
            return 2
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("digest"), str):
            print(json.dumps({"status": "paused", "failed_gate": "checkpoint", "state_event_created": False}, sort_keys=True))
            return 2
        claim_id = claim.get("claim_id")
        actor_ref = claim.get("actor_ref")
        if not isinstance(claim_id, str) or not isinstance(actor_ref, str):
            print(json.dumps({"status": "paused", "failed_gate": "claim", "state_event_created": False}, sort_keys=True))
            return 2
        lease_expires = claim.get("lease_expires_at")
        try:
            lease_at = datetime.fromisoformat(str(lease_expires).replace("Z", "+00:00"))
        except ValueError:
            print(json.dumps({"status": "paused", "failed_gate": "lease", "state_event_created": False}, sort_keys=True))
            return 2
        now = datetime.now(UTC)
        lease_valid = lease_at > now
        source_fresh = packet.get("source_fresh") is True
        heartbeat_due = lease_valid and (
            lease_at - now <= timedelta(milliseconds=args.heartbeat_window_ms)
        )
        if not source_fresh or not lease_valid or heartbeat_due:
            action = "heartbeat" if lease_valid else "reclaim"
            recover_args = argparse.Namespace(
                root=str(root),
                action=action,
                claim_id=claim_id,
                actor_ref=actor_ref,
                new_claim_id=(
                    None
                    if action == "heartbeat"
                    else _autorun_reclaim_id(session_id, claim_id, checkpoint["digest"])
                ),
                lease_ttl_ms=28_800_000,
            )
            try:
                receipt = _capture_json_handler(_work_recover, recover_args)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(
                    json.dumps(
                        {
                            "status": "paused",
                            "project_id": packet.get("project_id"),
                            "failed_gate": "source_fresh" if not source_fresh else "lease_valid",
                            "error": str(exc),
                            "state_event_created": False,
                        },
                        sort_keys=True,
                    )
                )
                return 2
            recovery_receipts.append(receipt)
            continue
        ledger = _autorun_ledger(root)
        try:
            ledger.execute("BEGIN IMMEDIATE")
            key = (
                _autorun_hash(session_id),
                _autorun_hash(str(root)),
                checkpoint["digest"],
                claim_id,
            )
            existing = ledger.execute(
                "SELECT 1 FROM continuation_runs WHERE session_sha256 = ? AND project_root_sha256 = ? AND checkpoint_digest = ? AND claim_id = ?",
                key,
            ).fetchone()
            if existing is not None:
                ledger.commit()
                print(
                    json.dumps(
                        {
                            "status": "already-continued",
                            "project_id": packet.get("project_id"),
                            "work_id": active.get("work_id"),
                            "claim_id": claim_id,
                            "checkpoint_digest": checkpoint["digest"],
                            "next_action": packet.get("next_action"),
                            "resume_packet": packet,
                            "state_event_created": False,
                            "recovery_receipts": recovery_receipts,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            ledger.execute(
                "INSERT INTO continuation_runs (session_sha256, project_root_sha256, checkpoint_digest, claim_id, next_action, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*key, str(packet.get("next_action")), now.isoformat()),
            )
            ledger.commit()
        finally:
            ledger.close()
        print(
            json.dumps(
                {
                    "status": "continued",
                    "project_id": packet.get("project_id"),
                    "work_id": active.get("work_id"),
                    "claim_id": claim_id,
                    "checkpoint_digest": checkpoint["digest"],
                    "next_action": packet.get("next_action"),
                    "resume_packet": packet,
                    "state_event_created": False,
                    "recovery_receipts": recovery_receipts,
                },
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps({"status": "paused", "failed_gate": "continuation_retry_exhausted", "state_event_created": False}, sort_keys=True))
    return 2


def _refresh_projection_after_state_change(root: Path) -> dict[str, Any]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _resume(
            argparse.Namespace(
                root=str(root), interaction_cursor=None, skill_lock=None
            )
        )
    packet = json.loads(output.getvalue())
    projection = _ensure_status_projection(root, packet)
    return {
        "projection_revision": projection["revision"],
        "projection_path": str(root / ".continuity/STATUS.current.md"),
        "projection_stale": False,
    }


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
        verify_sources=False,
    )
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    try:
        validate_attach_proposal(root, proposal, verify_sources=True)
    except CanonicalAttachError:
        print(
            json.dumps(
                {
                    "status": "denied",
                    "project_id": project["project_id"],
                    "revision": read_result["revision"],
                    "event_head": read_result["event_head"],
                    "failed_gate": "source_rebind_required",
                    "state_changed": False,
                    "next_action": "rebind-source-and-activate-next-work",
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        checkpoint_ref = ArtifactRef.from_document(
            json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("checkpoint ref is unavailable or invalid") from exc
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


def _verified_delivery_workspace(
    project_root: Path,
    workspace_root: str,
    *,
    workspace_id: str | None,
    allowed_effects: list[str],
    expected_head: str,
    expected_ref: str | None,
) -> dict[str, Any]:
    """Bind delivery authority to the exact committed base and pending patch."""
    workspace = Path(workspace_root).resolve()
    if not workspace.is_dir() or re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise ValueError("transition_gate:workspace_identity")

    def git_text(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("transition_gate:workspace_git")
        return completed.stdout.strip()

    def git_bytes(root: Path, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("transition_gate:workspace_git")
        return completed.stdout

    project_common = Path(
        git_text(
            project_root,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    workspace_common = Path(
        git_text(
            workspace,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    external_workspace = project_common.resolve() != workspace_common.resolve()
    repository_sha256 = _git_common_dir_sha256(workspace)
    registry_sha256: str | None = None
    if external_workspace:
        if workspace_id is None:
            raise ValueError("transition_gate:workspace_repository")
        project = _load_project(project_root)
        registry = _load_delivery_workspace_registry(project_root, project)
        registry_sha256 = registry["registry_sha256"]
        registered = next(
            (
                item
                for item in registry["workspaces"]
                if item["workspace_id"] == workspace_id
            ),
            None,
        )
        if (
            registered is None
            or Path(registered["workspace_root"]).resolve() != workspace
            or registered["repository_sha256"] != repository_sha256
            or not set(allowed_effects) <= set(registered["allowed_effects"])
        ):
            raise ValueError("transition_gate:workspace_registration")
    elif workspace_id is not None:
        raise ValueError("transition_gate:workspace_registration")
    head_commit = git_text(workspace, "rev-parse", "HEAD")
    if head_commit != expected_head:
        raise ValueError("transition_gate:workspace_head")
    expected_ref_commit: str | None = None
    if expected_ref is not None:
        if not expected_ref.strip() or len(expected_ref) > 500:
            raise ValueError("transition_gate:workspace_ref")
        expected_ref_commit = git_text(workspace, "rev-parse", "--verify", expected_ref)
        if expected_ref_commit != head_commit:
            raise ValueError("transition_gate:workspace_ref")

    status = git_bytes(
        workspace,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
    )
    delta = hashlib.sha256(b"context.workspace-delta/v1alpha1\0")
    tracked_diff = git_bytes(workspace, "diff", "--binary", "HEAD", "--")
    delta.update(len(tracked_diff).to_bytes(8, "big"))
    delta.update(tracked_diff)
    untracked = git_bytes(
        workspace,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    for encoded_path in sorted(item for item in untracked if item):
        relative = Path(os.fsdecode(encoded_path))
        candidate = (workspace / relative).resolve(strict=False)
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("transition_gate:workspace_path") from exc
        payload = (
            os.readlink(workspace / relative).encode("utf-8")
            if (workspace / relative).is_symlink()
            else (workspace / relative).read_bytes()
        )
        delta.update(len(encoded_path).to_bytes(8, "big"))
        delta.update(encoded_path)
        delta.update(len(payload).to_bytes(8, "big"))
        delta.update(hashlib.sha256(payload).digest())
    return {
        "workspace_id": workspace_id,
        "external_workspace": external_workspace,
        "repository_sha256": repository_sha256,
        "registry_sha256": registry_sha256,
        "head_commit": head_commit,
        "clean": not bool(status),
        "worktree_delta_sha256": delta.hexdigest(),
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
        projection_receipt = _refresh_projection_after_state_change(root)
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
                    **projection_receipt,
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
    projection_receipt = _refresh_projection_after_state_change(root)
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
                **projection_receipt,
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
        workspace_common = _git_common_dir_sha256(Path(args.workspace_root).resolve())
        project_common = _git_common_dir_sha256(root)
        if workspace_common == project_common:
            workspace = _verified_workspace(
                root,
                args.workspace_root,
                expected_head=args.expected_head,
                expected_ref=args.expected_ref,
            )
        else:
            repo_scopes = [
                scope["scope_ref"]
                for scope in successor_scopes
                if scope["scope_kind"] == "repo"
            ]
            if len(repo_scopes) != 1:
                raise ValueError("transition_gate:workspace_repository")
            repo_ref = repo_scopes[0]
            if repo_ref.startswith("repo://"):
                workspace_id = repo_ref.removeprefix("repo://")
            elif repo_ref.startswith("//"):
                # CLI scope parsing historically stores `repo://id` as kind
                # `repo` with reference `//id`; accept both encodings.
                workspace_id = repo_ref.removeprefix("//")
            else:
                workspace_id = repo_ref
            if not workspace_id or _ID_RE.fullmatch(workspace_id) is None:
                raise ValueError("transition_gate:workspace_repository")
            effect_scopes = [
                scope["scope_ref"]
                for scope in successor_scopes
                if scope["scope_kind"] == "effect"
            ]
            workspace = _verified_delivery_workspace(
                root,
                args.workspace_root,
                workspace_id=workspace_id,
                allowed_effects=effect_scopes,
                expected_head=args.expected_head,
                expected_ref=args.expected_ref,
            )
            workspace = {
                key: workspace[key]
                for key in (
                    "head_commit",
                    "clean",
                    "expected_ref",
                    "expected_ref_commit",
                )
            }
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
    try:
        source_evidence = _verified_attach_evidence(snapshot, proposal)
    except ValueError as exc:
        return deny("source_fresh", message=str(exc))
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
                    "source_evidence_rebound": False,
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
        "source_evidence_id": source_evidence["evidence_id"],
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
            "source_evidence_id": source_evidence["evidence_id"],
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
                "source_evidence_rebound": result["source_evidence_rebound"],
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
    execution_class = args.execution_class
    delivery_values = (
        args.source_ref,
        args.predecessor_work_id,
        args.implementation_evidence_id,
        args.workspace_id,
        args.workspace_root,
        args.expected_head,
        args.expected_ref,
        args.allow_effect,
    )
    if execution_class == "standard" and any(value is not None for value in delivery_values):
        raise ValueError("standard activation cannot carry a delivery binding")
    if execution_class == "delivery":
        if (
            args.source_ref is None
            or args.predecessor_work_id is None
            or not args.implementation_evidence_id
            or args.workspace_root is None
            or args.expected_head is None
            or not args.allow_effect
        ):
            raise ValueError(
                "delivery activation requires source, predecessor, implementation "
                "evidence, workspace head, and allowed effects"
            )
        if _SOURCE_REF_RE.fullmatch(args.source_ref) is None:
            raise ValueError("delivery source-ref must be one bounded opaque URI")
        if _STATE_ID_RE.fullmatch(args.predecessor_work_id) is None:
            raise ValueError("delivery predecessor Work identifier is invalid")
        implementation_evidence_ids = sorted(set(args.implementation_evidence_id))
        if len(implementation_evidence_ids) != len(args.implementation_evidence_id) or any(
            _STATE_ID_RE.fullmatch(item) is None for item in implementation_evidence_ids
        ):
            raise ValueError("delivery implementation evidence IDs are invalid")
        allowed_effects = sorted(set(args.allow_effect))
        if (
            len(allowed_effects) != len(args.allow_effect)
            or not set(allowed_effects) <= _DELIVERY_EFFECTS
        ):
            raise ValueError("delivery allowed effects are invalid")
        workspace_verification = _verified_delivery_workspace(
            root,
            args.workspace_root,
            workspace_id=args.workspace_id,
            allowed_effects=allowed_effects,
            expected_head=args.expected_head,
            expected_ref=args.expected_ref,
        )
        if args.workspace_id is not None:
            scope_refs.append(
                {
                    "scope_kind": "repo",
                    "scope_ref": f"repo://{args.workspace_id}",
                }
            )
        scope_refs.extend(
            {"scope_kind": "effect", "scope_ref": item}
            for item in allowed_effects
        )
    else:
        implementation_evidence_ids = []
        allowed_effects = []
        workspace_verification = None
    original_proposal = _load_current_attach_proposal(
        root,
        project["project_id"],
        verify_sources=False,
    )
    proposal = original_proposal
    changed_sources: list[str] = []
    try:
        validate_attach_proposal(root, proposal, verify_sources=True)
    except CanonicalAttachError:
        proposal, changed_sources = _refreshed_attach_proposal(
            root, project["project_id"], original_proposal
        )
        validate_attach_proposal(root, proposal, verify_sources=True)
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    snapshot = read_result["snapshot"]
    if changed_sources and (
        snapshot["project"]["active_work_ids"]
        or snapshot["project"]["primary_work_id"] is not None
        or any(item["status"] == "active" for item in snapshot["claims"])
    ):
        raise ValueError("stale source successor activation requires idle State")
    source_evidence = None
    try:
        source_evidence = _verified_attach_evidence(snapshot, proposal)
    except ValueError as exc:
        if str(exc) != "current canonical source evidence is not in State":
            raise
        source_evidence = _build_attach_evidence(proposal)
    expected_source_evidence_id = (
        f"evidence-attach-{proposal['proposal_sha256'][:16]}"
    )
    if source_evidence["evidence_id"] != expected_source_evidence_id:
        source_evidence = _build_attach_evidence(proposal)
    source_evidence_id = source_evidence["evidence_id"]
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
            expected_plan_sha256=_canonical_master_sha256(original_proposal),
            expected_registry_digest=read_result["registry_digest"],
        )
    except CheckpointStaleError as exc:
        if changed_sources:
            raise ValueError(
                "idle source rebind requires the verified pre-change checkpoint"
            ) from exc
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
    delivery_contract_sha256: str | None = None
    if execution_class == "delivery":
        predecessor = next(
            (
                item
                for item in snapshot["works"]
                if item["work_id"] == args.predecessor_work_id
            ),
            None,
        )
        if predecessor is None or predecessor["status"] != "completed":
            raise ValueError("delivery predecessor Work must be completed")
        evidence_by_id = {
            item["evidence_id"]: item for item in snapshot["evidence"]
        }
        if any(
            item not in predecessor["evidence_ids"]
            or item not in evidence_by_id
            or evidence_by_id[item]["validity"] != "verified"
            for item in implementation_evidence_ids
        ):
            raise ValueError(
                "delivery implementation evidence must be verified on the predecessor"
            )
        contract = {
            "schema_version": "context.delivery-activation/v1alpha1",
            "project_id": project["project_id"],
            "work_id": args.work_id,
            "source_ref": args.source_ref,
            "predecessor_work_id": predecessor["work_id"],
            "implementation_evidence_ids": implementation_evidence_ids,
            "workspace_verification": workspace_verification,
            "allowed_effects": allowed_effects,
            "source_evidence_id": source_evidence_id,
            "source_proposal_sha256": proposal["proposal_sha256"],
            "state_write_authority": False,
            "completion_authority": False,
        }
        contract_bytes = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        delivery_contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        contract_ref = _checkpoint_store(root).put_bytes(contract_bytes)
        delivery_evidence_id = (
            f"evidence-delivery-{delivery_contract_sha256[:16]}"
        )
        delivery_evidence = {
            "evidence_id": delivery_evidence_id,
            "kind": "artifact",
            "artifact_ref": contract_ref.uri,
            "content_sha256": contract_ref.digest,
            "validity": "verified",
            "observed_at": now_text,
            "verified_at": now_text,
        }
        delivery_work = next(
            (item for item in snapshot["works"] if item["work_id"] == args.work_id),
            None,
        )
        delivery_evidence_ids = [
            source_evidence_id,
            *implementation_evidence_ids,
            delivery_evidence_id,
        ]
        if delivery_work is None:
            delivery_work = {
                "work_id": args.work_id,
                "kind": "work",
                "title": args.work_title,
                "status": "ready",
                "parent_work_id": predecessor["work_id"],
                "dependency_ids": [],
                "owner_refs": [args.owner_ref],
                "scope_refs": copy.deepcopy(scope_refs),
                "overlap_candidate_ids": [],
                "dedupe_status": "clear",
                "supersedes_work_id": None,
                "evidence_ids": delivery_evidence_ids,
                "blocker_ids": [],
                "revision": 0,
            }
            prepare_service = _state_service(
                state_store,
                authorizer=_LocalWorkflowAuthorizer(),
                clock=lambda: now_text,
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            prepared = prepare_service.call_tool(
                COMMIT_TOOL,
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": (
                        f"activate-delivery-prepare-{delivery_contract_sha256[:16]}"
                    ),
                    "project_id": project["project_id"],
                    "expected_revision": read_result["revision"],
                    "causation_ref": args.source_ref,
                    "correlation_ref": f"delivery:{args.work_id}",
                    "supersedes_event_id": None,
                    "changes": [
                        *(
                            [
                                {
                                    "collection": "evidence",
                                    "object_id": source_evidence["evidence_id"],
                                    "value": source_evidence,
                                }
                            ]
                            if not any(
                                item["evidence_id"] == source_evidence["evidence_id"]
                                for item in snapshot["evidence"]
                            )
                            else []
                        ),
                        {
                            "collection": "evidence",
                            "object_id": delivery_evidence_id,
                            "value": delivery_evidence,
                        },
                        {
                            "collection": "works",
                            "object_id": delivery_work["work_id"],
                            "value": delivery_work,
                        },
                    ],
                },
                context=RequestContext(args.owner_ref, "local-workflow-approved"),
            )
            if not prepared["ok"]:
                raise ValueError(prepared["error"]["message"])
            read_result = _read_state_result(state_store, project["project_id"])
            snapshot = read_result["snapshot"]
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
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=_canonical_master_sha256(proposal),
                expected_registry_digest=read_result["registry_digest"],
            )
            _write_json_atomic(
                _checkpoint_ref_file(root), checkpoint_ref.to_document()
            )
        elif not (
            delivery_work["status"] in {"ready", "active"}
            and delivery_work["title"] == args.work_title
            and delivery_work["parent_work_id"] == predecessor["work_id"]
            and delivery_work["owner_refs"] == [args.owner_ref]
            and delivery_work["scope_refs"] == scope_refs
            and delivery_work["evidence_ids"] == delivery_evidence_ids
            and delivery_evidence_id in evidence_by_id
        ):
            raise ValueError("delivery activation binding conflicts with existing Work")
    pending_checkpoint_path = _transition_pending_checkpoint_file(root)

    proposal_path = root / ".continuity/attach-proposal.json"

    def validate_activation_source(
        proposal_sha256: str,
        evidence: dict[str, Any],
    ) -> bool:
        try:
            current_proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            validate_attach_proposal(root, current_proposal, verify_sources=True)
        except (OSError, json.JSONDecodeError, CanonicalAttachError):
            return False
        source_digest = _attach_source_digest(proposal)
        return (
            proposal_sha256 == proposal["proposal_sha256"]
            and current_proposal == proposal
            and evidence.get("evidence_id")
            == f"evidence-attach-{proposal['proposal_sha256'][:16]}"
            and evidence.get("kind") == "artifact"
            and evidence.get("artifact_ref")
            == f"artifact://sha256/{source_digest}"
            and evidence.get("content_sha256") == source_digest
            and evidence.get("validity") == "verified"
        )

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
        "execution_class": execution_class,
        "delivery_contract_sha256": delivery_contract_sha256,
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
        activation_source_validator=validate_activation_source,
    )
    if changed_sources:
        _write_json_atomic(proposal_path, proposal)
    try:
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
                "source_evidence": source_evidence
                if not any(
                    item["evidence_id"] == source_evidence_id
                    for item in snapshot["evidence"]
                )
                else None,
                "source_proposal_sha256": proposal["proposal_sha256"],
                "checkpoint_ref": checkpoint_ref.to_document(),
                "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
                "causation_ref": f"source:{proposal['proposal_sha256']}",
                "correlation_ref": f"work:{args.work_id}",
            },
            context=RequestContext(args.owner_ref, "local-workflow-approved"),
        )
    except Exception:
        if changed_sources:
            _write_json_atomic(proposal_path, original_proposal)
        raise
    if not response["ok"]:
        if changed_sources:
            _write_json_atomic(proposal_path, original_proposal)
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
    projection_receipt = _refresh_projection_after_state_change(root)
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
                "execution_class": execution_class,
                "delivery_contract_sha256": delivery_contract_sha256,
                "changed_sources": changed_sources,
                "source_evidence_rebound": bool(changed_sources),
                **projection_receipt,
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
        verify_sources=False,
    )
    try:
        validate_attach_proposal(root, proposal, verify_sources=True)
        source_fresh = True
    except CanonicalAttachError:
        source_fresh = False
    state_store = _open_state_store(root, project)
    read_result = _read_state_result(state_store, project["project_id"])
    snapshot = read_result["snapshot"]
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == args.claim_id),
        None,
    )
    if claim is None:
        raise ValueError("claim was not found")
    source_recovery = None
    original_proposal = proposal
    proposal_path = root / ".continuity/attach-proposal.json"
    if not source_fresh:
        active_ids = snapshot["project"]["active_work_ids"]
        primary_work_id = snapshot["project"]["primary_work_id"]
        if active_ids != [claim["work_id"]] or primary_work_id != claim["work_id"]:
            raise ValueError("source recovery requires the primary active Work")
        if claim["status"] != "active" or claim["actor_ref"] != args.actor_ref:
            raise ValueError("source recovery claim does not match the active owner")
        lease_expires = datetime.fromisoformat(
            claim["lease_expires_at"].replace("Z", "+00:00")
        )
        lease_is_valid = lease_expires > datetime.now(UTC)
        if args.action == "heartbeat" and not lease_is_valid:
            raise ValueError("stale source heartbeat requires a valid claim lease")
        if args.action == "reclaim" and lease_is_valid:
            raise ValueError("stale source reclaim requires an expired claim lease")
        try:
            prior_checkpoint = ArtifactRef.from_document(
                json.loads(_checkpoint_ref_file(root).read_text(encoding="utf-8"))
            )
            restore_checkpoint(
                prior_checkpoint,
                _checkpoint_store(root),
                expected_project_id=project["project_id"],
                expected_revision=read_result["revision"],
                expected_event_head=read_result["event_head"],
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=_canonical_master_sha256(original_proposal),
                expected_registry_digest=read_result["registry_digest"],
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, CheckpointError) as exc:
            raise ValueError(
                "source recovery requires the verified pre-change checkpoint"
            ) from exc
        refreshed, changed_sources = _refreshed_attach_proposal(
            root, project["project_id"], original_proposal
        )
        if not changed_sources:
            raise ValueError("source recovery found no changed canonical source")
        validate_attach_proposal(root, refreshed, verify_sources=True)
        source_digest = _attach_source_digest(refreshed)
        observed_at = datetime.now(UTC).isoformat()
        evidence_id = f"evidence-attach-{refreshed['proposal_sha256'][:16]}"
        source_recovery = {
            "work_id": claim["work_id"],
            "proposal_sha256": refreshed["proposal_sha256"],
            "changed_sources": changed_sources,
            "evidence": {
                "evidence_id": evidence_id,
                "kind": "artifact",
                "artifact_ref": f"artifact://sha256/{source_digest}",
                "content_sha256": source_digest,
                "validity": "verified",
                "observed_at": observed_at,
                "verified_at": observed_at,
            },
        }
        proposal = refreshed
        _write_json_atomic(proposal_path, proposal)
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
    if source_recovery is not None:
        request["source_recovery"] = source_recovery
    response = service.call_tool(
        LOCAL_CLAIM_RECOVERY_TOOL,
        request,
        context=RequestContext(args.actor_ref, "local-workflow-approved"),
    )
    if not response["ok"]:
        pending_checkpoint_path.unlink(missing_ok=True)
        if source_recovery is not None:
            _write_json_atomic(proposal_path, original_proposal)
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
                "source_recovered": result.get("source_recovered", False),
                "source_proposal_sha256": proposal["proposal_sha256"],
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


def _observe_report(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    _load_project(root)
    data_root = Path(args.data_root).resolve() if args.data_root else None
    report = build_observation_report(
        root,
        data_root=data_root,
        session_limit=args.session_limit,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
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
    doctor.add_argument("--codex-home", default=None)
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
    context = commands.add_parser(
        "context", help="query bounded current repository evidence"
    )
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_search = context_commands.add_parser(
        "search", help="search tracked current-worktree text within an output budget"
    )
    context_search.add_argument("--root", default=".")
    context_search.add_argument("--query", required=True)
    context_search.add_argument("--max-results", type=int, default=40)
    context_search.add_argument("--max-output-bytes", type=int, default=8192)
    context_search.set_defaults(handler=_context_search)
    context_index = context_commands.add_parser(
        "index", help="incrementally index tracked code symbols into user-local cache"
    )
    context_index.add_argument("--root", default=".")
    context_index.add_argument("--cache-path", default=None)
    context_index.add_argument("--max-files", type=int, default=50_000)
    context_index.set_defaults(handler=_context_index)
    context_lookup = context_commands.add_parser(
        "lookup", help="return bounded symbol and path references from the local code index"
    )
    context_lookup.add_argument("--root", default=".")
    context_lookup.add_argument("--cache-path", default=None)
    context_lookup.add_argument("--query", required=True)
    context_lookup.add_argument("--max-results", type=int, default=20)
    context_lookup.add_argument("--max-output-bytes", type=int, default=8192)
    context_lookup.set_defaults(handler=_context_lookup)
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
    inspect = commands.add_parser(
        "inspect", help="read the bounded packet without binding or writing projections"
    )
    inspect.add_argument("--root", default=".")
    inspect.set_defaults(handler=_inspect)
    autorun = commands.add_parser(
        "autorun", help="continue the current Work from a verified checkpoint"
    )
    autorun.add_argument("--root", default=".")
    autorun.add_argument("--session-id", default="local-cli")
    autorun.add_argument("--actor-ref", default=None)
    autorun.add_argument("--claim-id", default=None)
    autorun.add_argument("--heartbeat-window-ms", type=int, default=120_000)
    autorun.add_argument("--max-attempts", type=int, default=3)
    autorun.set_defaults(handler=_autorun)
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
    workspace = commands.add_parser(
        "workspace",
        help="register external delivery workspaces",
    )
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command",
        required=True,
    )
    workspace_register = workspace_commands.add_parser(
        "register",
        help="bind one external Git repository to this governance project",
    )
    workspace_register.add_argument("--root", default=".")
    workspace_register.add_argument("--workspace-id", required=True)
    workspace_register.add_argument("--workspace-root", required=True)
    workspace_register.add_argument("--allow-effect", action="append", required=True)
    workspace_register.set_defaults(handler=_workspace_register)
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
    work_activate.add_argument(
        "--execution-class", choices=("standard", "delivery"), default="standard"
    )
    work_activate.add_argument("--source-ref", default=None)
    work_activate.add_argument("--predecessor-work-id", default=None)
    work_activate.add_argument("--implementation-evidence-id", action="append")
    work_activate.add_argument("--workspace-id", default=None)
    work_activate.add_argument("--workspace-root", default=None)
    work_activate.add_argument("--expected-head", default=None)
    work_activate.add_argument("--expected-ref", default=None)
    work_activate.add_argument("--allow-effect", action="append")
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
    observe = commands.add_parser(
        "observe", help="inspect lightweight local observations"
    )
    observe_commands = observe.add_subparsers(
        dest="observe_command", required=True
    )
    observe_report = observe_commands.add_parser(
        "report", help="build a bounded offline tuning report"
    )
    observe_report.add_argument("--root", default=".")
    observe_report.add_argument("--data-root", default=None)
    observe_report.add_argument("--session-limit", type=int, default=20)
    observe_report.set_defaults(handler=_observe_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "root") and getattr(args, "command", None) != "context":
        try:
            args.root = str(resolve_control_root(args.root))
        except WorkspaceBindingError as exc:
            raise ValueError(str(exc)) from exc
    result = int(args.handler(args))
    if result == 0 and getattr(args, "command", None) == "init":
        register_control_root(args.root)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
