"""M5-08 durable continuation and anti-reset behavior."""

from __future__ import annotations

import copy
import unittest

from context_control_plane.durable_continuation import (
    DurableContinuationError,
    advance_durable_continuation,
    canonical_durable_continuation_bytes,
    compose_durable_continuation,
    evaluate_durable_recovery,
    validate_durable_continuation,
    validate_durable_recovery_receipt,
)


class M508DurableContinuationTests(unittest.TestCase):
    def _authority(self) -> dict:
        return {
            "operation_id": "operation/m5-08/unit",
            "project_id": "project-context-control-plane",
            "project_revision": 54,
            "task_id": "M5-08",
            "task_revision": 3,
            "event_head": {"sequence_no": 91, "event_sha256": "a" * 64},
        }

    def _state(self, **overrides) -> dict:
        arguments = {
            **self._authority(),
            "phase": "effect-in-flight",
            "last_durable_action": "effect-intent:effect/m5-08/test",
            "next_action": "verify-effect:effect/m5-08/test",
            "acknowledged_input_ids": ["input/already-answered"],
            "reserved_effects": [
                {
                    "effect_id": "effect/m5-08/test",
                    "replay_policy": "never",
                    "status": "started",
                }
            ],
            "response_mode": "continue-silently",
        }
        arguments.update(overrides)
        return compose_durable_continuation(**arguments)

    def _reads(self) -> list[dict]:
        return [
            {
                "source_ref": "artifact://sha256/" + "b" * 64,
                "content_sha256": "b" * 64,
                "bytes_read": 2048,
            },
            {
                "source_ref": "state://project-context-control-plane/revision/54",
                "content_sha256": "c" * 64,
                "bytes_read": 512,
            },
        ]

    def _recover(self, state: dict | None = None, **overrides) -> dict:
        expected = state or self._state()
        arguments = {
            "trusted_state": copy.deepcopy(expected),
            "restored_state": copy.deepcopy(expected),
            "trusted_authority": self._authority(),
            "proposed_first_action": expected["next_action"],
            "response_input_id": None,
            "requested_effect_id": "effect/m5-08/test",
            "replay_requested": False,
            "recovery_reads": self._reads(),
            "recovery_budget_bytes": 4096,
            "observed_at": "2026-08-15T04:00:00+08:00",
        }
        arguments.update(overrides)
        return evaluate_durable_recovery(**arguments)

    def test_exact_restore_allows_only_the_bound_first_action(self):
        state = self._state()
        validate_durable_continuation(state)
        receipt = self._recover(state)
        validate_durable_recovery_receipt(receipt)
        self.assertEqual(receipt["operation_id"], "operation/m5-08/unit")
        self.assertEqual(receipt["phase"], "effect-in-flight")
        self.assertEqual(receipt["first_action"], state["next_action"])
        self.assertEqual(receipt["execution_gate"], "allow")
        self.assertEqual(receipt["acknowledged_input_replays"], 0)
        self.assertEqual(receipt["continuation_fields_recovered"], 10)
        self.assertEqual(receipt["continuation_fields_total"], 10)
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["provider_native_authority"])
        self.assertEqual(receipt["recovery_read_receipt"]["bytes_read"], 2560)
        self.assertEqual(receipt["recovery_read_receipt"]["budget_bytes"], 4096)

    def test_acknowledged_input_cannot_be_answered_again(self):
        with self.assertRaisesRegex(DurableContinuationError, "acknowledged input"):
            self._recover(response_input_id="input/already-answered")
        with self.assertRaisesRegex(DurableContinuationError, "response mode"):
            self._recover(response_input_id="input/new")

    def test_first_action_reset_fails_closed(self):
        with self.assertRaisesRegex(DurableContinuationError, "first action"):
            self._recover(proposed_first_action="restate-plan-from-start")

    def test_revision_event_head_and_operation_are_bound_to_trusted_authority(self):
        fault_cases = (
            ("project_revision", 53),
            ("task_revision", 2),
            ("operation_id", "operation/other"),
            ("event_head", {"sequence_no": 90, "event_sha256": "d" * 64}),
        )
        for field, value in fault_cases:
            restored = copy.deepcopy(self._state())
            restored[field] = value
            if field != "event_head":
                restored["state_sha256"] = "0" * 64
            with self.subTest(field=field), self.assertRaises(DurableContinuationError):
                self._recover(restored_state=restored)

    def test_self_consistent_forged_state_is_rejected(self):
        forged = self._state(project_revision=53)
        with self.assertRaisesRegex(DurableContinuationError, "trusted authority"):
            self._recover(state=forged, trusted_state=forged, restored_state=forged)

    def test_phase_fsm_rejects_skips_and_terminal_revival(self):
        state = self._state(
            phase="prepared",
            last_durable_action="operation-created",
            next_action="commit-intent",
            reserved_effects=[
                {
                    "effect_id": "effect/m5-08/test",
                    "replay_policy": "never",
                    "status": "reserved",
                }
            ],
        )
        with self.assertRaisesRegex(DurableContinuationError, "phase transition"):
            advance_durable_continuation(
                state,
                phase="effect-in-flight",
                last_durable_action="effect-started",
                next_action="verify-effect:effect/m5-08/test",
                reserved_effects=[
                    {
                        "effect_id": "effect/m5-08/test",
                        "replay_policy": "never",
                        "status": "started",
                    }
                ],
                response_mode="continue-silently",
            )
        terminal = self._state(
            phase="terminal",
            last_durable_action="operation-terminal",
            next_action=None,
            reserved_effects=[
                {
                    "effect_id": "effect/m5-08/test",
                    "replay_policy": "never",
                    "status": "settled",
                }
            ],
            response_mode="terminal",
        )
        with self.assertRaisesRegex(DurableContinuationError, "terminal"):
            advance_durable_continuation(
                terminal,
                phase="prepared",
                last_durable_action="reset",
                next_action="commit-intent",
                reserved_effects=terminal["reserved_effects"],
                response_mode="continue-silently",
            )

    def test_effect_replay_obeys_per_effect_safe_or_never_policy(self):
        with self.assertRaisesRegex(DurableContinuationError, "never-replay"):
            self._recover(replay_requested=True)
        safe = self._state(
            next_action="replay-effect:effect/m5-08/test",
            reserved_effects=[
                {
                    "effect_id": "effect/m5-08/test",
                    "replay_policy": "safe",
                    "status": "started",
                }
            ],
        )
        receipt = self._recover(state=safe, replay_requested=True)
        self.assertEqual(receipt["execution_gate"], "allow")
        with self.assertRaisesRegex(DurableContinuationError, "not reserved"):
            self._recover(requested_effect_id="effect/unreserved")

    def test_invalid_effect_phase_response_or_authority_fails_closed(self):
        state = self._state()
        fault_cases = []
        changed = copy.deepcopy(state)
        changed["phase"] = "unknown"
        fault_cases.append(changed)
        changed = copy.deepcopy(state)
        changed["reserved_effects"][0]["replay_policy"] = "always"
        fault_cases.append(changed)
        changed = copy.deepcopy(state)
        changed["reserved_effects"][0]["status"] = "settled"
        fault_cases.append(changed)
        changed = copy.deepcopy(state)
        changed["state_write_authority"] = True
        fault_cases.append(changed)
        changed = copy.deepcopy(state)
        changed["provider_native_authority"] = True
        fault_cases.append(changed)
        for index, changed in enumerate(fault_cases):
            with self.subTest(index=index), self.assertRaises(DurableContinuationError):
                validate_durable_continuation(changed)
        with self.assertRaises(DurableContinuationError):
            self._state(reserved_effects=[None])

    def test_recovery_reads_are_strict_unique_and_bounded(self):
        with self.assertRaisesRegex(DurableContinuationError, "budget") as budget_error:
            self._recover(recovery_budget_bytes=2559)
        self.assertEqual(budget_error.exception.code, "read-budget")
        duplicate = self._reads() + [self._reads()[0]]
        with self.assertRaisesRegex(DurableContinuationError, "unique") as duplicate_error:
            self._recover(recovery_reads=duplicate)
        self.assertEqual(duplicate_error.exception.code, "recovery-contract")
        malformed = self._reads()
        malformed[0]["bytes_read"] = -1
        with self.assertRaises(DurableContinuationError):
            self._recover(recovery_reads=malformed)

    def test_canonical_state_is_deterministic_and_rejects_digest_tampering(self):
        first = self._state()
        second = self._state(
            acknowledged_input_ids=list(reversed(first["acknowledged_input_ids"])),
            reserved_effects=list(reversed(first["reserved_effects"])),
        )
        self.assertEqual(
            canonical_durable_continuation_bytes(first),
            canonical_durable_continuation_bytes(second),
        )
        changed = copy.deepcopy(first)
        changed["state_sha256"] = "f" * 64
        with self.assertRaisesRegex(DurableContinuationError, "digest"):
            validate_durable_continuation(changed)


if __name__ == "__main__":
    unittest.main()
