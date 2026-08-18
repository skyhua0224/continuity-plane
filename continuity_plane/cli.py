"""Release-neutral command line interface."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .sqlite_state_store import SQLiteStateStore
from .canonical_attach import (
    CanonicalAttachError,
    build_attach_proposal,
    validate_attach_proposal,
)
from .state_mcp import CLAIM_TOOL, COMMIT_TOOL, RequestContext, StateMCPService

VERSION = "0.1.0a1"
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


def _state_service(
    store: SQLiteStateStore,
    *,
    authorizer: Any | None = None,
    clock: Any | None = None,
    event_id_factory: Any | None = None,
) -> StateMCPService:
    registry_digest = hashlib.sha256(b"context.public-runtime/v1").hexdigest()
    return StateMCPService(
        store,
        authorizer=authorizer or _LocalCliAuthorizer(),
        registry_digest=registry_digest,
        clock=clock or (lambda: datetime.now(UTC).isoformat()),
        event_id_factory=event_id_factory
        or (lambda request_id: f"event-{request_id}-{uuid.uuid4().hex}"),
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


def _parse_scope(raw: str) -> dict[str, str]:
    if not isinstance(raw, str) or ":" not in raw:
        raise ValueError("scope must use kind:reference")
    kind, reference = raw.split(":", 1)
    if not kind or not reference:
        raise ValueError("scope must use kind:reference")
    return {"scope_kind": kind, "scope_ref": reference}


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
    snapshot = store.read_project(project["project_id"])
    events = store.read_events(project["project_id"])
    active_ids = snapshot["project"]["active_work_ids"]
    if len(active_ids) != 1 or snapshot["project"]["primary_work_id"] != active_ids[0]:
        raise ValueError("resume requires exactly one primary active Work")
    work = next(item for item in snapshot["works"] if item["work_id"] == active_ids[0])
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
    event_head = (
        None
        if not events
        else {
            "sequence_no": events[-1]["sequence_no"],
            "event_sha256": events[-1]["event_sha256"],
        }
    )
    packet = {
        "schema_version": "context.resume-packet/v1alpha1",
        "project_id": project["project_id"],
        "revision": snapshot["project"]["revision"],
        "event_head": event_head,
        "active_work": {
            key: copy.deepcopy(work[key])
            for key in (
                "work_id",
                "title",
                "status",
                "revision",
                "scope_refs",
                "evidence_ids",
            )
        },
        "claim": {
            key: copy.deepcopy(claim[key])
            for key in (
                "claim_id",
                "actor_ref",
                "status",
                "lease_expires_at",
                "scope_owners",
            )
        },
        "proposal_sha256": proposal["proposal_sha256"],
        "source_fresh": source_fresh,
        "lease_valid": lease_valid,
        "read_only": not source_fresh or not lease_valid,
        "next_action": "continue-active-work",
        "packet_sha256": "",
    }
    unsigned = {key: value for key, value in packet.items() if key != "packet_sha256"}
    packet["packet_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path = root / ".continuity/resume-packet.json"
    path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(packet, ensure_ascii=False, sort_keys=True))
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
    approve = attach_commands.add_parser("approve", help="approve and activate a proposal")
    approve.add_argument("--root", default=".")
    approve.add_argument("--proposal", default=".continuity/attach-proposal.json")
    approve.add_argument("--actor-ref", required=True)
    approve.add_argument("--claim-id", required=True)
    approve.set_defaults(handler=_attach_approve)
    resume = commands.add_parser("resume", help="compose the current bounded resume packet")
    resume.add_argument("--root", default=".")
    resume.set_defaults(handler=_resume)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
