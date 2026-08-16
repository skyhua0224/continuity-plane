"""Persistent fixtures for M8-01 process-crash verification."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, ClassVar

import yaml

from ..artifact_store import LocalArtifactStore
from ..checkpoint import publish_checkpoint
from ..durable_checkpoint_gate import (
    LocalDurableCheckpointGate,
    compose_durable_checkpoint_receipt,
)
from ..durable_continuation import compose_durable_continuation
from ..durable_operation import compose_durable_operation
from ..durable_operation_runner import LocalDurableOperationRunner
from ..durable_operation_store import (
    DurableOperationStoreNotFound,
    SQLiteDurableOperationStore,
)
from ..durable_operation_trace import LocalDurableOperationTraceRecorder
from ..durable_state_authority import (
    StateMCPDurableAuthorityAdapter,
    compose_durable_state_receipt,
    durable_state_receipt_ref,
)
from ..durable_state_migration import migrate_typed_state_v4_to_v5
from ..idea_review import migrate_typed_state_v3_to_v4
from ..sqlite_state_store import SQLiteStateNotFound, SQLiteStateStore
from ..state_mcp import RequestContext, StateMCPService
from ..typed_state_migration import migrate_v2alpha1_to_v3alpha1


class SQLiteEffectFixture:
    """A durable external system with effect-key idempotency and status lookup."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-effect-adapter/v1alpha1",
        "adapter_id": "fixture.sqlite-idempotent-effect/v1",
        "adapter_version": "1.0.0",
        "implementation_sha256": "9" * 64,
        "source_ref": "fixture://m8-01/sqlite-idempotent-effect",
        "idempotency_modes": ["effect-key"],
        "status_lookups": ["none", "supported"],
        "replay_policies": ["safe"],
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    effect_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    result_ref TEXT NOT NULL,
                    settlement_ref TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS counters (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    apply_attempts INTEGER NOT NULL,
                    duplicate_attempts INTEGER NOT NULL,
                    semantic_effects INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO counters(
                    singleton, apply_attempts, duplicate_attempts, semantic_effects
                ) VALUES (1, 0, 0, 0);
                """
            )

    def lookup(self, effect_key: str, request_sha256: str) -> dict[str, str]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT request_sha256, result_ref, settlement_ref
                FROM effects WHERE effect_key = ?
                """,
                (effect_key,),
            ).fetchone()
        if row is None:
            return {"status": "absent"}
        if row["request_sha256"] != request_sha256:
            raise ValueError("effect key was reused for a different request")
        return {
            "status": "settled",
            "request_sha256": row["request_sha256"],
            "result_ref": row["result_ref"],
            "settlement_ref": row["settlement_ref"],
        }

    def apply(self, effect_key: str, request_sha256: str) -> dict[str, str]:
        result_ref = "artifact://sha256/" + hashlib.sha256(
            f"result:{effect_key}:{request_sha256}".encode()
        ).hexdigest()
        settlement_ref = "settlement://fixture/" + effect_key
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_sha256, result_ref, settlement_ref
                FROM effects WHERE effect_key = ?
                """,
                (effect_key,),
            ).fetchone()
            connection.execute(
                "UPDATE counters SET apply_attempts = apply_attempts + 1 WHERE singleton = 1"
            )
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    connection.rollback()
                    raise ValueError("effect key was reused for a different request")
                connection.execute(
                    """
                    UPDATE counters SET duplicate_attempts = duplicate_attempts + 1
                    WHERE singleton = 1
                    """
                )
                connection.commit()
                return {
                    "status": "settled",
                    "request_sha256": row["request_sha256"],
                    "result_ref": row["result_ref"],
                    "settlement_ref": row["settlement_ref"],
                }
            connection.execute(
                """
                INSERT INTO effects(effect_key, request_sha256, result_ref, settlement_ref)
                VALUES (?, ?, ?, ?)
                """,
                (effect_key, request_sha256, result_ref, settlement_ref),
            )
            connection.execute(
                "UPDATE counters SET semantic_effects = semantic_effects + 1 "
                "WHERE singleton = 1"
            )
            connection.commit()
        return {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": result_ref,
            "settlement_ref": settlement_ref,
        }

    def metrics(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            counters = connection.execute(
                "SELECT apply_attempts, duplicate_attempts, semantic_effects "
                "FROM counters WHERE singleton = 1"
            ).fetchone()
            physical_effects = connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        return {
            "apply_attempts": counters["apply_attempts"],
            "duplicate_effects": counters["duplicate_attempts"],
            "semantic_effects": counters["semantic_effects"],
            "physical_effects": physical_effects,
        }


class SQLiteAuthorityFixture:
    """A durable idempotent stand-in for State MCP intent and completion commits."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-authority-adapter/v1alpha1",
        "adapter_id": "fixture.sqlite-state-mcp/v1",
        "adapter_version": "1.0.0",
        "authority_interface": "state-mcp",
        "receipt_schema_version": "context.durable-state-receipt/v1alpha1",
        "source_ref": "fixture://m8-01/sqlite-state-mcp",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commits (
                    operation_id TEXT NOT NULL,
                    commit_kind TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    PRIMARY KEY (operation_id, commit_kind)
                )
                """
            )

    def _commit(self, operation: dict[str, Any], kind: str, evidence_ref: str) -> str:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT evidence_ref FROM commits
                WHERE operation_id = ? AND commit_kind = ?
                """,
                (operation["operation_id"], kind),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row["evidence_ref"] != evidence_ref:
                    raise ValueError("authority commit replay changed its evidence ref")
                return row["evidence_ref"]
            connection.execute(
                """
                INSERT INTO commits(operation_id, commit_kind, evidence_ref)
                VALUES (?, ?, ?)
                """,
                (operation["operation_id"], kind, evidence_ref),
            )
            connection.commit()
        return evidence_ref

    def commit_intent(self, operation: dict[str, Any]) -> dict[str, Any]:
        receipt = compose_durable_state_receipt(
            operation,
            action="authorize",
            request_id="request-m8-01-live-authorize",
            request_sha256="5" * 64,
            revision=operation["authority"]["project_revision"] + 1,
            event_head={"sequence_no": 1268, "event_sha256": "6" * 64},
            result_ref=None,
            registry_digest="a" * 64,
            state_response_sha256="7" * 64,
            reconciled=False,
        )
        self._commit(operation, "intent", durable_state_receipt_ref(receipt))
        return receipt

    def commit_state(self, operation: dict[str, Any]) -> dict[str, Any]:
        receipt = compose_durable_state_receipt(
            operation,
            action="complete",
            request_id="request-m8-01-live-complete",
            request_sha256="8" * 64,
            revision=operation["authority"]["project_revision"] + 2,
            event_head={"sequence_no": 1269, "event_sha256": "9" * 64},
            result_ref=operation["result_ref"],
            registry_digest="a" * 64,
            state_response_sha256="b" * 64,
            reconciled=False,
        )
        self._commit(operation, "state", durable_state_receipt_ref(receipt))
        return receipt

    def metrics(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT commit_kind, COUNT(*) AS count FROM commits GROUP BY commit_kind"
            ).fetchall()
        counts = {row["commit_kind"]: row["count"] for row in rows}
        return {
            "intent_commits": counts.get("intent", 0),
            "state_commits": counts.get("state", 0),
        }


class FixtureCheckpointGate:
    """A deterministic gate receipt used only by the process-kill harness."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-checkpoint-adapter/v1alpha1",
        "adapter_id": "fixture.checkpoint-gate/v1",
        "adapter_version": "1.0.0",
        "source_ref": "fixture://m8-01/checkpoint-gate",
        "receipt_schema_version": "context.durable-checkpoint-gate/v1alpha1",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def verify(self, operation: dict[str, Any]) -> dict[str, Any]:
        return compose_durable_checkpoint_receipt(
            operation, critical_projection_sha256="c" * 64
        )


_CONTINUATION_PHASE = {
    "prepared": "prepared",
    "intent-committed": "intent-committed",
    "effect-in-flight": "effect-in-flight",
    "outcome-unknown": "effect-in-flight",
    "effect-settled": "effect-settled",
    "response-committed": "response-committed",
    "terminal": "terminal",
}
_CONTINUATION_ACTIONS = {
    "prepared": ("operation-created", "commit-intent"),
    "intent-committed": ("effect-intent-committed", "dispatch-effect"),
    "effect-in-flight": ("effect-started", "verify-effect"),
    "effect-settled": ("effect-settled", "commit-state"),
    "response-committed": ("state-response-committed", "finalize"),
    "terminal": ("operation-terminal", None),
}
_CONTINUATION_EFFECT_STATUS = {
    "prepared": "reserved",
    "intent-committed": "reserved",
    "effect-in-flight": "started",
    "effect-settled": "settled",
    "response-committed": "settled",
    "terminal": "settled",
}


def _continuation_state_for(operation: dict[str, Any], requested_phase: str) -> dict:
    phase = _CONTINUATION_PHASE[requested_phase]
    last_action, next_action = _CONTINUATION_ACTIONS[phase]
    return compose_durable_continuation(
        operation_id=operation["operation_id"],
        project_id=operation["project_id"],
        project_revision=operation["authority"]["project_revision"],
        task_id=operation["work_id"],
        task_revision=1,
        event_head=operation["authority"]["event_head"],
        phase=phase,
        last_durable_action=last_action,
        next_action=next_action,
        acknowledged_input_ids=[],
        reserved_effects=[
            {
                "effect_id": operation["effect"]["effect_id"],
                "replay_policy": operation["effect"]["replay_policy"],
                "status": _CONTINUATION_EFFECT_STATUS[phase],
            }
        ],
        response_mode="terminal" if phase == "terminal" else "continue-silently",
    )


def _prepared_fixture() -> dict[str, Any]:
    arguments = {
        "operation_id": "operation/m8-01/live-crash",
        "project_id": "project-context-control-plane",
        "work_id": "M8-01",
        "claim_id": "claim/m8-01/live-crash",
        "authority": {
            "project_revision": 58,
            "event_head": {"sequence_no": 1267, "event_sha256": "a" * 64},
        },
        "effect": {
            "effect_id": "effect/m8-01/live-crash",
            "effect_key": "external-write:m8-01:live-crash",
            "operation": "external-write",
            "scope_ref": {
                "scope_kind": "effect",
                "scope_ref": "durable-operation-live-crash",
            },
            "adapter_id": "fixture.sqlite-idempotent-effect/v1",
            "request_sha256": "b" * 64,
            "replay_policy": "safe",
            "idempotency_mode": "effect-key",
            "status_lookup": "supported",
        },
        "checkpoint_ref": {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": "c" * 64,
            "size_bytes": 4096,
            "artifact_uri": "artifact://sha256/" + "c" * 64,
        },
        "continuation_sha256": "d" * 64,
        "trace_binding": {
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "run_id": "run/m8-01/live-crash",
            "correlation_id": "correlation/m8-01",
        },
        "observed_at": "2026-08-16T10:00:00+08:00",
    }
    draft = compose_durable_operation(**arguments)
    arguments["continuation_sha256"] = _continuation_state_for(
        draft, "prepared"
    )["state_sha256"]
    return compose_durable_operation(**arguments)


def run_fixture(root: Path, *, crash_point: str | None) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    operation_store = SQLiteDurableOperationStore(root / "operation.sqlite3")
    operation_store.initialize()
    effect = SQLiteEffectFixture(root / "effect.sqlite3")
    effect.initialize()
    authority = SQLiteAuthorityFixture(root / "authority.sqlite3")
    authority.initialize()
    prepared = _prepared_fixture()
    try:
        current_revision = operation_store.read_operation(prepared["operation_id"])[
            "record_revision"
        ]
    except DurableOperationStoreNotFound:
        current_revision = 0
    tick = current_revision + 1

    def clock() -> str:
        nonlocal tick
        value = f"2026-08-16T10:00:{tick:02d}+08:00"
        tick += 1
        return value

    def fault(point: str, operation: dict[str, Any]) -> None:
        if point == crash_point:
            os.kill(os.getpid(), signal.SIGKILL)

    runner = LocalDurableOperationRunner(
        store=operation_store,
        effect_adapter=effect,
        authority_adapter=authority,
        checkpoint_gate=FixtureCheckpointGate(),
        trace_recorder=LocalDurableOperationTraceRecorder(
            output_path=root / "trace.jsonl",
            source={
                "kind": "durable_operation",
                "provider": "local",
                "adapter": "context.durable-runner/v1",
                "source_ref": "component://context.durable-runner",
            },
        ),
        continuation_state=_continuation_state_for,
        clock=clock,
        fault_hook=fault if crash_point is not None else None,
    )
    terminal = runner.run(prepared)
    history = operation_store.read_history(prepared["operation_id"])
    return {
        "schema_version": "context.durable-operation-crash-fixture/v1alpha1",
        "phase": terminal["phase"],
        "operation_sha256": terminal["record_sha256"],
        "history_phases": [item["phase"] for item in history],
        **effect.metrics(),
        **authority.metrics(),
    }


class _AllowAuthorizer:
    def authorize(self, context: Any, action: str, project_id: str) -> bool:
        return True


def _ready_state_snapshot(repository_root: Path) -> dict[str, Any]:
    fixture_set = yaml.safe_load(
        (
            repository_root / "experiments/state/m2-01-core-fixtures.yaml"
        ).read_text(encoding="utf-8")
    )
    snapshot = copy.deepcopy(
        next(
            case["document"]
            for case in fixture_set["cases"]
            if case["case_id"] == "solo-active-work"
        )
    )
    snapshot["schema_version"] = "context.typed-state/v2alpha1"
    snapshot["project"]["updated_at"] = "2026-08-10T04:00:00+08:00"
    snapshot["project"]["active_work_ids"] = []
    snapshot["project"]["primary_work_id"] = None
    snapshot["project"]["open_blocker_ids"] = []
    snapshot["blockers"] = []
    work = next(item for item in snapshot["works"] if item["work_id"] == "work-solo")
    work.update(
        {
            "status": "ready",
            "parent_work_id": "goal-m8-01",
            "owner_refs": ["actor-second"],
            "dedupe_status": "clear",
            "overlap_candidate_ids": [],
            "blocker_ids": [],
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
            "scope_refs": [
                {
                    "scope_kind": "file",
                    "scope_ref": "repo://control-plane/src/core.py",
                }
            ],
        }
    )
    common = {
        "status": "ready",
        "dependency_ids": [],
        "owner_refs": ["actor-owner"],
        "overlap_candidate_ids": [],
        "dedupe_status": "clear",
        "supersedes_work_id": None,
        "evidence_ids": [],
        "blocker_ids": [],
        "revision": 1,
        "return_point_work_id": None,
        "exit_criteria": [],
        "attempt_budget": None,
        "expires_at": None,
        "promotion_target_work_id": None,
        "mainline_authority": True,
    }
    snapshot["works"] = [
        {
            **common,
            "work_id": "campaign-m8",
            "kind": "campaign",
            "title": "M8 campaign",
            "parent_work_id": None,
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": "m8/campaign"}
            ],
        },
        {
            **common,
            "work_id": "goal-m8-01",
            "kind": "goal",
            "title": "M8-01 goal",
            "parent_work_id": "campaign-m8",
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": "m8/durable"}
            ],
        },
        work,
    ]
    snapshot["claims"] = []
    snapshot["effects"] = []
    v3 = migrate_v2alpha1_to_v3alpha1(snapshot)
    v4 = migrate_typed_state_v3_to_v4(
        v3, migrated_at="2026-08-10T04:00:00+08:00"
    )
    return migrate_typed_state_v4_to_v5(v4)


def _state_service(store: SQLiteStateStore) -> StateMCPService:
    return StateMCPService(
        store,
        authorizer=_AllowAuthorizer(),
        registry_digest="a" * 64,
        clock=lambda: "2026-08-10T05:00:00+08:00",
        event_id_factory=lambda request_id: f"event-m8-01-{request_id}",
    )


def _claim_state(service: StateMCPService, context: RequestContext) -> None:
    response = service.call_tool(
        "context.state.claim",
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "request-m8-01-real-claim",
            "project_id": "project-solo",
            "expected_revision": 7,
            "work_id": "work-solo",
            "claim_id": "claim-m8-01-real",
            "scope_owners": [
                {
                    "scope_kind": "file",
                    "scope_ref": "repo://control-plane/src/core.py",
                }
            ],
            "lease_expires_at": "2026-08-10T06:00:00+08:00",
            "causation_ref": "work:M8-01",
            "correlation_ref": "campaign:M8",
        },
        context=context,
    )
    if response.get("ok") is not True:
        raise RuntimeError("real crash fixture could not claim active Work")


def _read_state(service: StateMCPService, context: RequestContext) -> dict[str, Any]:
    response = service.call_tool(
        "context.state.read",
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "request-m8-01-real-read",
            "project_id": "project-solo",
        },
        context=context,
    )
    if response.get("ok") is not True:
        raise RuntimeError("real crash fixture could not read State")
    return response["result"]


def _prepared_real_fixture(
    read_result: dict[str, Any],
    checkpoint_ref: dict[str, Any],
    *,
    status_lookup: str = "supported",
) -> dict[str, Any]:
    arguments = {
        "operation_id": "operation/m8-01/real-crash",
        "project_id": "project-solo",
        "work_id": "work-solo",
        "claim_id": "claim-m8-01-real",
        "authority": {
            "project_revision": read_result["revision"],
            "event_head": copy.deepcopy(read_result["event_head"]),
        },
        "effect": {
            "effect_id": "effect-m8-01-real-crash",
            "effect_key": "external-write:m8-01:real-crash",
            "operation": "write-artifact",
            "scope_ref": {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/core.py",
            },
            "adapter_id": "fixture.sqlite-idempotent-effect/v1",
            "request_sha256": "b" * 64,
            "replay_policy": "safe",
            "idempotency_mode": "effect-key",
            "status_lookup": status_lookup,
        },
        "checkpoint_ref": checkpoint_ref,
        "continuation_sha256": "d" * 64,
        "trace_binding": {
            "trace_id": "3" * 32,
            "span_id": "4" * 16,
            "run_id": "run/m8-01/real-crash",
            "correlation_id": "correlation/m8-real",
        },
        "observed_at": "2026-08-10T05:00:00+08:00",
    }
    draft = compose_durable_operation(**arguments)
    arguments["continuation_sha256"] = _continuation_state_for(
        draft, "prepared"
    )["state_sha256"]
    return compose_durable_operation(**arguments)


def run_real_fixture(
    root: Path,
    *,
    repository_root: Path,
    crash_point: str | None,
    status_lookup: str = "supported",
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    operation_store = SQLiteDurableOperationStore(root / "operation.sqlite3")
    operation_store.initialize()
    state_store = SQLiteStateStore(root / "state.sqlite3")
    state_store.initialize()
    context = RequestContext("actor-second", "m8-01-real-crash")
    service = _state_service(state_store)
    try:
        state_store.read_project("project-solo")
    except SQLiteStateNotFound:
        state_store.create_project(_ready_state_snapshot(repository_root))
        _claim_state(service, context)

    artifact_store = LocalArtifactStore(root / "artifacts")
    artifact_store.initialize()
    try:
        prepared = operation_store.read_history(
            "operation/m8-01/real-crash"
        )[0]
    except DurableOperationStoreNotFound:
        read_result = _read_state(service, context)
        checkpoint_ref = publish_checkpoint(
            read_result,
            artifact_store,
            canonical_plan_sha256="e" * 64,
        )
        prepared = _prepared_real_fixture(
            read_result,
            checkpoint_ref.to_document(),
            status_lookup=status_lookup,
        )

    effect = SQLiteEffectFixture(root / "effect.sqlite3")
    effect.initialize()
    current_read = _read_state(service, context)
    gate = LocalDurableCheckpointGate(
        artifact_store=artifact_store,
        governance_ref=current_read["snapshot"]["project"]["governance_ref"],
        canonical_plan_sha256="e" * 64,
        registry_digest="a" * 64,
    )
    try:
        current_revision = operation_store.read_operation(prepared["operation_id"])[
            "record_revision"
        ]
    except DurableOperationStoreNotFound:
        current_revision = 0
    tick = current_revision + 1

    def clock() -> str:
        nonlocal tick
        value = f"2026-08-10T05:01:{tick:02d}+08:00"
        tick += 1
        return value

    def fault(point: str, operation: dict[str, Any]) -> None:
        if point == crash_point:
            os.kill(os.getpid(), signal.SIGKILL)

    runner = LocalDurableOperationRunner(
        store=operation_store,
        effect_adapter=effect,
        authority_adapter=StateMCPDurableAuthorityAdapter(service, context=context),
        checkpoint_gate=gate,
        trace_recorder=LocalDurableOperationTraceRecorder(
            output_path=root / "trace.jsonl",
            source={
                "kind": "durable_operation",
                "provider": "local",
                "adapter": "context.durable-runner/v1",
                "source_ref": "component://context.durable-runner",
            },
        ),
        continuation_state=_continuation_state_for,
        clock=clock,
        fault_hook=fault if crash_point is not None else None,
    )
    terminal = runner.run(prepared)
    history = operation_store.read_history(prepared["operation_id"])
    snapshot = state_store.read_project("project-solo")
    events = state_store.read_events("project-solo")
    state_effect = next(
        item
        for item in snapshot["effects"]
        if item["effect_id"] == prepared["effect"]["effect_id"]
    )

    def effect_event_count(status: str) -> int:
        return sum(
            any(
                change["collection"] == "effects"
                and change["object_id"] == prepared["effect"]["effect_id"]
                and change["value"]["status"] == status
                for change in event["changes"]
            )
            for event in events
        )

    return {
        "schema_version": "context.durable-operation-real-crash-fixture/v1alpha1",
        "phase": terminal["phase"],
        "operation_sha256": terminal["record_sha256"],
        "history_phases": [item["phase"] for item in history],
        "authority_backend": "state-mcp-sqlite",
        "checkpoint_backend": "content-addressed-local",
        "state_revision": snapshot["project"]["revision"],
        "state_event_count": len(events),
        "effect_state": state_effect["status"],
        **effect.metrics(),
        "intent_commits": effect_event_count("authorized"),
        "state_commits": effect_event_count("succeeded"),
    }


def canonical_receipt(receipt: dict[str, Any]) -> str:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"))
