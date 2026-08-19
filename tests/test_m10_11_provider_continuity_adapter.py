"""Provider-neutral live event adapter conformance for M10-11."""

from __future__ import annotations

import copy
import unittest

from context_control_plane.provider_continuity_adapter import (
    ProviderContinuityAdapterError,
    compose_provider_capability_manifest,
    compose_provider_continuity_event,
    derive_provider_continuity_observation,
    validate_provider_continuity_event_chain,
)


class M1011ProviderContinuityAdapterTests(unittest.TestCase):
    def _manifest(self, *, counter_mode: str = "delta") -> dict:
        return compose_provider_capability_manifest(
            provider_contract_id="third-party-provider-z/v7",
            adapter_id="external-adapter-z",
            adapter_sha256="a" * 64,
            usage_counter_mode=counter_mode,
            context_window_source="provider-trace",
            lifecycle_signals=[
                "precompact",
                "postcompact",
                "usage",
                "read",
                "production-action",
            ],
        )

    def _action(self) -> dict:
        return {
            "kind": "tool",
            "target": "continue-active-work",
            "request_sha256": "b" * 64,
        }

    def _events(self, *, counter_mode: str = "delta") -> list[dict]:
        definitions = [
            (
                "usage",
                {
                    "input_tokens": 100 if counter_mode == "delta" else 1000,
                    "cached_input_tokens": 10 if counter_mode == "delta" else 100,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 20 if counter_mode == "delta" else 200,
                    "reasoning_output_tokens": 5 if counter_mode == "delta" else 50,
                },
            ),
            (
                "ingress",
                {
                    "input_id": "question-1",
                    "input_kind": "question",
                    "input_sha256": "c" * 64,
                    "table_requested": False,
                },
            ),
            (
                "response",
                {
                    "response_id": "response-1",
                    "input_id": "question-1",
                    "boundary_id": None,
                    "response_sha256": "d" * 64,
                    "direct_answer": True,
                    "table_present": False,
                    "recovery_narration": False,
                    "assessment_kind": "deterministic-contract",
                    "assessment_ref": "assessment://response-1",
                },
            ),
            (
                "error",
                {
                    "error_code": "host-compact-timeout",
                    "error_sha256": "e" * 64,
                },
            ),
            (
                "precompact",
                {
                    "boundary_id": "boundary-1",
                    "project_revision": 31,
                    "active_work_ref": "work://active",
                    "claim_ref": "claim://active",
                    "expected_first_action": self._action(),
                    "acknowledged_input_ids": ["question-1"],
                    "packet_ref": "artifact://packet/1",
                    "packet_sha256": "f" * 64,
                    "packet_bytes": 6144,
                },
            ),
            (
                "postcompact",
                {
                    "boundary_id": "boundary-1",
                    "project_revision": 31,
                    "active_work_ref": "work://active",
                    "claim_ref": "claim://active",
                    "canary_passed": True,
                    "canary_ref": "canary://boundary-1",
                    "canary_sha256": "1" * 64,
                },
            ),
            (
                "read",
                {
                    "boundary_id": "boundary-1",
                    "source_kind": "execution-packet",
                    "source_ref": "artifact://packet/1",
                    "content_sha256": "f" * 64,
                    "bytes_read": 6144,
                },
            ),
            (
                "production-action",
                {"boundary_id": "boundary-1", "action": self._action()},
            ),
            (
                "usage",
                {
                    "input_tokens": 120 if counter_mode == "delta" else 1120,
                    "cached_input_tokens": 12 if counter_mode == "delta" else 112,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 30 if counter_mode == "delta" else 230,
                    "reasoning_output_tokens": 6 if counter_mode == "delta" else 56,
                },
            ),
        ]
        events = []
        previous = None
        for sequence_no, (event_type, payload) in enumerate(definitions, start=1):
            event = compose_provider_continuity_event(
                previous_event=previous,
                event_id=f"event-{sequence_no}",
                sequence_no=sequence_no,
                event_type=event_type,
                occurred_at=f"2026-08-20T12:00:{sequence_no:02d}+08:00",
                payload=payload,
            )
            events.append(event)
            previous = event
        return events

    def test_explicit_lifecycle_derives_usage_boundary_reads_and_first_action(self) -> None:
        events = self._events()
        observation = derive_provider_continuity_observation(
            manifest=self._manifest(),
            events=events,
            source_ref="opaque://provider-z/session-1",
            source_sha256="2" * 64,
        )

        self.assertEqual(len(observation["compactions"]), 1)
        self.assertEqual(observation["provider_usage"]["input_tokens"], 220)
        self.assertEqual(observation["provider_usage"]["output_tokens"], 50)
        self.assertEqual(
            observation["compactions"][0]["pre"]["expected_first_action_sha256"],
            observation["compactions"][0]["post"]["actual_first_action_sha256"],
        )
        self.assertEqual(
            observation["compactions"][0]["recovery_reads"][0]["bytes_read"],
            6144,
        )
        self.assertEqual(len(observation["responses"]), 1)
        self.assertNotIn("raw_transcript", observation)

    def test_error_code_containing_compact_does_not_count_as_compaction(self) -> None:
        events = self._events()
        events = events[:4]
        validate_provider_continuity_event_chain(events)
        observation = derive_provider_continuity_observation(
            manifest=self._manifest(),
            events=events,
            source_ref="opaque://provider-z/error-only",
            source_sha256="3" * 64,
        )
        self.assertEqual(observation["compactions"], [])

    def test_cumulative_usage_is_differenced_and_counter_reset_is_rejected(self) -> None:
        events = self._events(counter_mode="cumulative")
        observation = derive_provider_continuity_observation(
            manifest=self._manifest(counter_mode="cumulative"),
            events=events,
            source_ref="opaque://provider-z/cumulative",
            source_sha256="4" * 64,
        )
        self.assertEqual(observation["provider_usage"]["input_tokens"], 120)
        self.assertEqual(observation["provider_usage"]["output_tokens"], 30)

        reset = copy.deepcopy(events)
        reset[-1]["payload"]["input_tokens"] = 10
        reset[-1]["event_sha256"] = "0" * 64
        with self.assertRaises(ProviderContinuityAdapterError):
            derive_provider_continuity_observation(
                manifest=self._manifest(counter_mode="cumulative"),
                events=reset,
                source_ref="opaque://provider-z/reset",
                source_sha256="5" * 64,
            )

    def test_event_gap_hash_tamper_missing_canary_and_missing_action_fail_closed(self) -> None:
        events = self._events()
        mutations = []
        gap = copy.deepcopy(events)
        gap[2]["sequence_no"] = 9
        mutations.append(gap)
        tamper = copy.deepcopy(events)
        tamper[2]["payload"]["direct_answer"] = False
        mutations.append(tamper)
        no_canary = copy.deepcopy(events)
        no_canary[5]["payload"]["canary_passed"] = False
        mutations.append(no_canary)
        no_action = events[:-2] + events[-1:]
        mutations.append(no_action)
        for mutation in mutations:
            with self.subTest(), self.assertRaises(ProviderContinuityAdapterError):
                derive_provider_continuity_observation(
                    manifest=self._manifest(),
                    events=mutation,
                    source_ref="opaque://provider-z/fault",
                    source_sha256="6" * 64,
                )

    def test_unknown_provider_contract_needs_no_core_enum_change(self) -> None:
        manifest = self._manifest()
        self.assertEqual(manifest["provider_contract_id"], "third-party-provider-z/v7")
        observation = derive_provider_continuity_observation(
            manifest=manifest,
            events=self._events(),
            source_ref="opaque://provider-z/open-registration",
            source_sha256="7" * 64,
        )
        self.assertEqual(observation["trace"]["adapter_id"], "external-adapter-z")


if __name__ == "__main__":
    unittest.main()
