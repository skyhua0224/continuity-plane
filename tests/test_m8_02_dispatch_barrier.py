"""M8-02 write-side dispatch barrier and durable runner integration."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from context_control_plane.durable_checkpoint_gate import (
    compose_durable_checkpoint_receipt,
)
from context_control_plane.durable_operation_runner import (
    DurableOperationRunnerError,
    LocalDurableOperationRunner,
)
from context_control_plane.durable_operation_store import SQLiteDurableOperationStore
from context_control_plane.durable_operation_trace import (
    LocalDurableOperationTraceRecorder,
)
from context_control_plane.durable_state_authority import compose_durable_state_receipt
from context_control_plane.shared_work_ledger import ClaimLifecycleError, WorkLedger
from tests.test_m8_01_durable_operation_store import (
    _continuation_state_for,
    _prepared_operation,
)


def _scope(ref: str = "artifact/m8-02") -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": ref}


def _ledger(
    operation: dict[str, Any] | None = None,
) -> tuple[WorkLedger, dict[str, Any]]:
    project_id = operation["project_id"] if operation else "project-m8-01"
    work_id = operation["work_id"] if operation else "work-m8-01"
    claim_id = operation["claim_id"] if operation else "claim-m8-01"
    project_revision = operation["authority"]["project_revision"] if operation else 42
    scope = operation["effect"]["scope_ref"] if operation else _scope()
    ledger = WorkLedger(
        project_id=project_id,
        project_revision=project_revision,
        works=[
            {
                "work_id": work_id,
                "status": "ready",
                "identity_key": "identity-m8-02-dispatch",
                "scope_refs": [scope],
            }
        ],
        max_ttl_ms=10_000,
    )
    acquired = ledger.acquire_claim(
        work_id=work_id,
        actor_ref="actor-m8-02",
        expected_project_revision=project_revision,
        observed_at="2026-08-16T10:00:00+00:00",
        requested_ttl_ms=10_000,
        claim_id=claim_id,
        scope_owners=[scope],
    )
    return ledger, acquired["claim"]


def _dispatch_arguments(claim: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "effect_id": "effect-m8-01-store",
        "effect_key": "effect-key-m8-01-store",
        "request_sha256": "5" * 64,
        "claim_id": claim["claim_id"],
        "work_id": claim["work_id"],
        "actor_ref": claim["actor_ref"],
        "expected_project_revision": claim["expected_project_revision"],
        "expected_claim_revision": claim["claim_revision"],
        "lease_epoch": claim["lease_epoch"],
        "fence": claim["lease_epoch"],
        "observed_at": "2026-08-16T10:00:01+00:00",
        "operation": "write-artifact",
        "scope_ref": _scope(),
    }


class _EffectAdapter:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-effect-adapter/v1alpha1",
        "adapter_id": "fixture.idempotent-effect/v1",
        "adapter_version": "1.0.0",
        "implementation_sha256": "9" * 64,
        "source_ref": "fixture://m8-02/effect",
        "idempotency_modes": ["effect-key"],
        "status_lookups": ["none", "supported"],
        "replay_policies": ["never", "safe"],
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self) -> None:
        self.apply_calls = 0

    def lookup(self, effect_key: str, request_sha256: str) -> dict[str, Any]:
        return {"status": "absent"}

    def apply(self, effect_key: str, request_sha256: str) -> dict[str, Any]:
        self.apply_calls += 1
        return {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": "artifact://sha256/" + "4" * 64,
            "settlement_ref": "settlement://m8-02/one",
        }


class _StateAuthority:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-authority-adapter/v1alpha1",
        "adapter_id": "fixture.state-mcp-authority/v1",
        "adapter_version": "1.0.0",
        "authority_interface": "state-mcp",
        "receipt_schema_version": "context.durable-state-receipt/v1alpha1",
        "source_ref": "fixture://m8-02/state",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def commit_intent(self, operation: dict[str, Any]) -> dict[str, Any]:
        return compose_durable_state_receipt(
            operation,
            action="authorize",
            request_id="request-m8-02-authorize",
            request_sha256="5" * 64,
            revision=operation["authority"]["project_revision"] + 1,
            event_head={"sequence_no": 1268, "event_sha256": "6" * 64},
            result_ref=None,
            registry_digest="a" * 64,
            state_response_sha256="7" * 64,
            reconciled=False,
        )

    def commit_state(self, operation: dict[str, Any]) -> dict[str, Any]:
        return compose_durable_state_receipt(
            operation,
            action="complete",
            request_id="request-m8-02-complete",
            request_sha256="8" * 64,
            revision=operation["authority"]["project_revision"] + 3,
            event_head={"sequence_no": 1270, "event_sha256": "9" * 64},
            result_ref=operation["result_ref"],
            registry_digest="a" * 64,
            state_response_sha256="b" * 64,
            reconciled=False,
        )


class _CheckpointGate:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-checkpoint-adapter/v1alpha1",
        "adapter_id": "fixture.checkpoint-gate/v1",
        "adapter_version": "1.0.0",
        "source_ref": "fixture://m8-02/checkpoint",
        "receipt_schema_version": "context.durable-checkpoint-gate/v1alpha1",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def verify(self, operation: dict[str, Any]) -> dict[str, Any]:
        return compose_durable_checkpoint_receipt(
            operation, critical_projection_sha256="c" * 64
        )


class _LedgerDispatchAuthority:
    def __init__(self, ledger: WorkLedger, claim: dict[str, Any]) -> None:
        self.ledger = ledger
        self.claim = claim
        self.calls = 0

    def start_dispatch(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        effect = operation["effect"]
        return self.ledger.start_effect_dispatch(
            request_id="dispatch-" + operation["operation_id"],
            effect_id=effect["effect_id"],
            effect_key=effect["effect_key"],
            request_sha256=effect["request_sha256"],
            claim_id=self.claim["claim_id"],
            work_id=self.claim["work_id"],
            actor_ref=self.claim["actor_ref"],
            expected_project_revision=self.claim["expected_project_revision"],
            expected_claim_revision=self.claim["claim_revision"],
            lease_epoch=self.claim["lease_epoch"],
            fence=self.claim["lease_epoch"],
            observed_at="2026-08-16T10:00:01+00:00",
            operation=effect["operation"],
            scope_ref=effect["scope_ref"],
        )["receipt"]


class _DenyDispatchAuthority:
    def __init__(self) -> None:
        self.calls = 0

    def start_dispatch(self, operation: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise ClaimLifecycleError("claim_not_active")


def _trace(path: Path) -> LocalDurableOperationTraceRecorder:
    return LocalDurableOperationTraceRecorder(
        output_path=path,
        source={
            "kind": "durable_operation",
            "provider": "local",
            "adapter": "context.durable-runner/v1",
            "source_ref": "component://context.durable-runner",
        },
    )


class M802DispatchBarrierTests(unittest.TestCase):
    def test_dispatch_start_is_a_hash_bound_idempotent_write_transition(self) -> None:
        ledger, claim = _ledger()
        arguments = _dispatch_arguments(claim, request_id="dispatch-1")

        first = ledger.start_effect_dispatch(**arguments)
        snapshot_after = ledger.snapshot()
        replay = ledger.start_effect_dispatch(**arguments)

        self.assertEqual(first, replay)
        self.assertEqual(ledger.snapshot(), snapshot_after)
        self.assertEqual(first["effect"]["status"], "started")
        self.assertEqual(first["effect"]["lease_epoch"], claim["lease_epoch"])
        self.assertEqual(first["receipt"]["fence"], claim["lease_epoch"])
        self.assertEqual(first["receipt"]["request_sha256"], "5" * 64)
        self.assertRegex(first["receipt"]["receipt_sha256"], r"^[0-9a-f]{64}$")

        changed = copy.deepcopy(arguments)
        changed["request_sha256"] = "6" * 64
        with self.assertRaisesRegex(ClaimLifecycleError, "request_replay_conflict"):
            ledger.start_effect_dispatch(**changed)

    def test_revoke_winning_before_dispatch_produces_no_started_effect(self) -> None:
        ledger, claim = _ledger()
        revoked = ledger.revoke_claim(
            claim_id=claim["claim_id"],
            revoker_ref="admin-m8-02",
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at="2026-08-16T10:00:01+00:00",
            reason="worker revoked",
        )
        arguments = _dispatch_arguments(
            claim,
            request_id="dispatch-after-revoke",
        )
        arguments["expected_project_revision"] = revoked["project_revision"]

        with self.assertRaisesRegex(ClaimLifecycleError, "claim_not_active"):
            ledger.start_effect_dispatch(**arguments)

        self.assertEqual(ledger.snapshot()["effects"], [])

    def test_runner_requires_dispatch_receipt_before_adapter_apply(self) -> None:
        prepared = _prepared_operation()
        ledger, claim = _ledger(prepared)
        dispatch = _LedgerDispatchAuthority(ledger, claim)
        adapter = _EffectAdapter()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteDurableOperationStore(root / "operation.sqlite3")
            store.initialize()
            terminal = LocalDurableOperationRunner(
                store=store,
                effect_adapter=adapter,
                authority_adapter=_StateAuthority(),
                dispatch_authority=dispatch,
                checkpoint_gate=_CheckpointGate(),
                trace_recorder=_trace(root / "trace.jsonl"),
                continuation_state=_continuation_state_for,
                clock=iter(
                    f"2026-08-16T10:00:0{index}+00:00" for index in range(1, 9)
                ).__next__,
            ).run(prepared)

        self.assertEqual(terminal["phase"], "terminal")
        self.assertEqual(dispatch.calls, 1)
        self.assertEqual(adapter.apply_calls, 1)
        self.assertTrue(terminal["start_ref"].startswith("state-dispatch://sha256/"))

    def test_denied_dispatch_stops_runner_before_adapter_apply(self) -> None:
        prepared = _prepared_operation()
        dispatch = _DenyDispatchAuthority()
        adapter = _EffectAdapter()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteDurableOperationStore(root / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=adapter,
                authority_adapter=_StateAuthority(),
                dispatch_authority=dispatch,
                checkpoint_gate=_CheckpointGate(),
                trace_recorder=_trace(root / "trace.jsonl"),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:01+00:00",
            )
            with self.assertRaisesRegex(DurableOperationRunnerError, "dispatch"):
                runner.run(prepared)

        self.assertEqual(dispatch.calls, 1)
        self.assertEqual(adapter.apply_calls, 0)


if __name__ == "__main__":
    unittest.main()
