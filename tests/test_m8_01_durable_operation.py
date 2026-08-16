"""M8-01 provider-neutral durable operation contract."""

from __future__ import annotations

import copy
import unittest
from itertools import pairwise

from context_control_plane.durable_operation import (
    DurableOperationError,
    advance_durable_operation,
    compose_durable_operation,
    evaluate_durable_operation_recovery,
    validate_durable_operation,
    validate_durable_operation_chain,
)


class M801DurableOperationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.arguments = {
            "operation_id": "operation/m8-01/unit",
            "project_id": "project-context-control-plane",
            "work_id": "M8-01",
            "claim_id": "claim/m8-01/unit",
            "authority": {
                "project_revision": 58,
                "event_head": {"sequence_no": 1267, "event_sha256": "a" * 64},
            },
            "effect": {
                "effect_id": "effect/m8-01/unit",
                "effect_key": "external-write:m8-01:unit",
                "operation": "external-write",
                "scope_ref": {
                    "scope_kind": "effect",
                    "scope_ref": "durable-operation-unit",
                },
                "adapter_id": "fixture.idempotent-effect/v1",
                "request_sha256": "b" * 64,
                "replay_policy": "never",
                "idempotency_mode": "effect-key",
                "status_lookup": "none",
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
                "run_id": "run/m8-01/unit",
                "correlation_id": "correlation/m8-01",
            },
            "observed_at": "2026-08-16T10:00:00+08:00",
        }

    def compose(self, **changes):
        return compose_durable_operation(**{**self.arguments, **changes})

    def test_prepared_operation_binds_state_cursor_effect_checkpoint_and_trace(self) -> None:
        operation = self.compose()

        self.assertEqual(validate_durable_operation(operation), operation)
        self.assertEqual(operation["phase"], "prepared")
        self.assertEqual(operation["record_revision"], 0)
        self.assertEqual(operation["attempt_count"], 0)
        self.assertEqual(operation["continuation_sha256"], "d" * 64)
        self.assertEqual(operation["trace_binding"], self.arguments["trace_binding"])
        self.assertIsNone(operation["intent_ref"])
        self.assertIsNone(operation["start_ref"])
        self.assertIsNone(operation["settlement_ref"])
        self.assertIsNone(operation["result_ref"])
        self.assertIsNone(operation["state_commit_ref"])
        self.assertFalse(operation["state_write_authority"])
        self.assertFalse(operation["provider_native_authority"])

    def test_effect_scope_uses_the_canonical_m3_contract(self) -> None:
        changed = copy.deepcopy(self.arguments["effect"])
        changed["scope_ref"] = {
            "scope_kind": "environment",
            "scope_ref": "local://m8-01-test",
        }

        with self.assertRaisesRegex(DurableOperationError, "scope"):
            self.compose(effect=changed)

    def test_operation_progresses_through_an_append_only_hash_chain(self) -> None:
        prepared = self.compose()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="f" * 64,
            start_ref="attempt://effect/m8-01/unit/1",
        )
        unknown = advance_durable_operation(
            started,
            phase="outcome-unknown",
            observed_at="2026-08-16T10:00:03+08:00",
            continuation_sha256="0" * 64,
            reconciliation_reason="process-exited-before-settlement",
        )
        settled = advance_durable_operation(
            unknown,
            phase="effect-settled",
            observed_at="2026-08-16T10:00:04+08:00",
            continuation_sha256="3" * 64,
            settlement_ref="settlement://effect/m8-01/unit",
            result_ref="artifact://sha256/" + "4" * 64,
            reconciliation_reason="provider-status-confirmed",
        )
        committed = advance_durable_operation(
            settled,
            phase="response-committed",
            observed_at="2026-08-16T10:00:05+08:00",
            continuation_sha256="5" * 64,
            state_commit_ref="state-event://effect-completed/2",
        )
        terminal = advance_durable_operation(
            committed,
            phase="terminal",
            observed_at="2026-08-16T10:00:06+08:00",
            continuation_sha256="6" * 64,
        )

        chain = [prepared, intent, started, unknown, settled, committed, terminal]
        validate_durable_operation_chain(chain)
        self.assertEqual([item["record_revision"] for item in chain], list(range(7)))
        self.assertEqual(started["attempt_count"], 1)
        self.assertEqual(terminal["attempt_count"], 1)
        for previous, current in pairwise(chain):
            self.assertEqual(current["previous_record_sha256"], previous["record_sha256"])

    def test_immutable_identity_and_effect_reservation_cannot_drift(self) -> None:
        operation = self.compose()
        changed = copy.deepcopy(operation)
        changed["effect"]["effect_key"] = "external-write:forged"

        with self.assertRaisesRegex(DurableOperationError, "immutable"):
            advance_durable_operation(
                changed,
                phase="intent-committed",
                observed_at="2026-08-16T10:00:01+08:00",
                continuation_sha256="e" * 64,
                intent_ref="state-event://effect-authorized/1",
                previous=operation,
            )

    def test_recovery_never_replays_an_ambiguous_never_effect(self) -> None:
        prepared = self.compose()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="f" * 64,
            start_ref="attempt://effect/m8-01/unit/1",
        )

        receipt = evaluate_durable_operation_recovery(started)

        self.assertEqual(receipt["recovery_action"], "manual")
        self.assertFalse(receipt["automatic_effect_replay"])
        self.assertEqual(receipt["reason"], "never-effect-outcome-unknown")
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["provider_native_authority"])

    def test_safe_effect_uses_same_key_idempotent_retry_after_unknown_outcome(self) -> None:
        effect = copy.deepcopy(self.arguments["effect"])
        effect["replay_policy"] = "safe"
        prepared = self.compose(effect=effect)
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="f" * 64,
            start_ref="attempt://effect/m8-01/unit/1",
        )

        receipt = evaluate_durable_operation_recovery(started)

        self.assertEqual(receipt["recovery_action"], "verify-or-retry-idempotent")
        self.assertTrue(receipt["automatic_effect_replay"])
        self.assertEqual(receipt["effect_key"], effect["effect_key"])

    def test_settled_effect_only_commits_state_and_terminal_only_cleans_up(self) -> None:
        prepared = self.compose()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="f" * 64,
            start_ref="attempt://effect/m8-01/unit/1",
        )
        settled = advance_durable_operation(
            started,
            phase="effect-settled",
            observed_at="2026-08-16T10:00:03+08:00",
            continuation_sha256="3" * 64,
            settlement_ref="settlement://effect/m8-01/unit",
            result_ref="artifact://sha256/" + "4" * 64,
        )
        committed = advance_durable_operation(
            settled,
            phase="response-committed",
            observed_at="2026-08-16T10:00:04+08:00",
            continuation_sha256="5" * 64,
            state_commit_ref="state-event://effect-completed/2",
        )
        terminal = advance_durable_operation(
            committed,
            phase="terminal",
            observed_at="2026-08-16T10:00:05+08:00",
            continuation_sha256="6" * 64,
        )

        self.assertEqual(
            evaluate_durable_operation_recovery(settled)["recovery_action"],
            "commit-state",
        )
        self.assertEqual(
            evaluate_durable_operation_recovery(committed)["recovery_action"],
            "finalize",
        )
        self.assertEqual(
            evaluate_durable_operation_recovery(terminal)["recovery_action"],
            "terminal",
        )


if __name__ == "__main__":
    unittest.main()
