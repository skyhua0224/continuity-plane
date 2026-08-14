"""M8-04 provider-neutral context trace contract tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.context_trace import (
    ContextTraceError,
    GENESIS_EVENT_SHA256,
    LocalContextTraceEmitter,
    append_context_trace_event,
    canonical_context_trace_event_bytes,
    export_context_trace_to_otel,
    validate_context_trace_chain,
    validate_context_trace_event,
)


EVENT_NAMES = (
    "context.compaction.precompact",
    "context.compaction.postcompact",
    "context.routing.input",
    "context.skill.selection",
    "context.plan.revision",
    "context.multi_agent.dispatch",
    "context.multi_agent.handoff",
    "context.delivery.accepted",
)


class M804ContextTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = {
            "project_id": "project/context-control-plane",
            "state_revision": 54,
            "active_work_id": "M5-07",
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "run_id": "run/m5-07/unit",
            "operation_id": "operation/m5-07/emit",
            "correlation_id": "correlation/m5-campaign",
        }
        self.source = {
            "kind": "local_hook",
            "provider": "codex",
            "adapter": "provider-neutral/v1",
            "source_ref": "hook://m8-04/unit",
        }

    def _append(self, chain: list[dict], event_name: str, index: int = 1) -> dict:
        return append_context_trace_event(
            chain,
            event_name=event_name,
            binding=self.binding,
            source=self.source,
            evidence_refs=[f"run://m8-04/evidence/{index}"],
            observed_at=f"2026-08-15T03:00:{index:02d}Z",
            attributes={"index": index, "result": "observed"},
            event_id=f"event/m8-04/{index}",
        )

    def test_all_required_observations_bind_trace_state_run_and_evidence(self):
        chain: list[dict] = []
        for index, event_name in enumerate(EVENT_NAMES, start=1):
            event = self._append(chain, event_name, index)
            validate_context_trace_event(event)
            self.assertEqual(event["event_name"], event_name)
            self.assertEqual(event["project_id"], self.binding["project_id"])
            self.assertEqual(event["state_revision"], 54)
            self.assertEqual(event["active_work_id"], "M5-07")
            self.assertEqual(event["trace_id"], self.binding["trace_id"])
            self.assertEqual(event["span_id"], self.binding["span_id"])
            self.assertEqual(event["run_id"], self.binding["run_id"])
            self.assertEqual(event["operation_id"], self.binding["operation_id"])
            self.assertEqual(event["correlation_id"], self.binding["correlation_id"])
            self.assertEqual(event["source"], self.source)
            self.assertTrue(event["evidence_refs"])
            self.assertFalse(event["authority"])
        validate_context_trace_chain(chain)
        self.assertEqual(chain[0]["previous_event_sha256"], GENESIS_EVENT_SHA256)
        self.assertEqual(chain[-1]["sequence"], len(EVENT_NAMES))

    def test_append_is_deterministic_and_preserves_existing_events(self):
        first_chain: list[dict] = []
        first = self._append(first_chain, EVENT_NAMES[0])
        original = copy.deepcopy(first)
        self._append(first_chain, EVENT_NAMES[1], 2)
        self.assertEqual(first_chain[0], original)
        self.assertEqual(first_chain[1]["previous_event_sha256"], first["event_sha256"])

        second_chain: list[dict] = []
        replay = self._append(second_chain, EVENT_NAMES[0])
        self.assertEqual(
            canonical_context_trace_event_bytes(first),
            canonical_context_trace_event_bytes(replay),
        )

    def test_chain_rejects_tampering_gaps_reordering_and_wrong_previous_hash(self):
        chain: list[dict] = []
        self._append(chain, EVENT_NAMES[0], 1)
        self._append(chain, EVENT_NAMES[1], 2)

        mutations = []
        changed = copy.deepcopy(chain)
        changed[0]["attributes"]["result"] = "forged"
        mutations.append(changed)
        changed = copy.deepcopy(chain)
        changed[1]["sequence"] = 3
        mutations.append(changed)
        changed = copy.deepcopy(chain)
        changed[1]["previous_event_sha256"] = GENESIS_EVENT_SHA256
        mutations.append(changed)
        mutations.append(list(reversed(copy.deepcopy(chain))))

        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ContextTraceError):
                validate_context_trace_chain(changed)

    def test_event_validation_is_strict_and_never_grants_authority(self):
        chain: list[dict] = []
        event = self._append(chain, EVENT_NAMES[0])
        cases = (
            ("event_name", "provider.compaction"),
            ("authority", True),
            ("evidence_refs", []),
            ("observed_at", "2026-08-15T03:00:01"),
            ("trace_id", "not-an-otel-trace-id"),
            ("span_id", "0" * 16),
            ("state_revision", -1),
        )
        for field, value in cases:
            changed = copy.deepcopy(event)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ContextTraceError):
                validate_context_trace_event(changed)

        changed = copy.deepcopy(event)
        changed["unexpected"] = True
        with self.assertRaises(ContextTraceError):
            validate_context_trace_event(changed)

    def test_local_emitter_persists_and_resumes_a_valid_jsonl_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trace" / "events.jsonl"
            emitter = LocalContextTraceEmitter(
                binding=self.binding,
                source=self.source,
                output_path=output,
            )
            emitter.emit(
                EVENT_NAMES[0],
                evidence_refs=["run://m8-04/evidence/1"],
                observed_at="2026-08-15T03:00:01Z",
                attributes={"phase": "before"},
                event_id="event/m8-04/1",
            )
            resumed = LocalContextTraceEmitter(
                binding=self.binding,
                source=self.source,
                output_path=output,
            )
            resumed.emit(
                EVENT_NAMES[1],
                evidence_refs=["run://m8-04/evidence/2"],
                observed_at="2026-08-15T03:00:02Z",
                attributes={"phase": "after"},
                event_id="event/m8-04/2",
            )

            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(tuple(records), resumed.events)
            validate_context_trace_chain(records)
            self.assertEqual(records[1]["previous_event_sha256"], records[0]["event_sha256"])

    def test_local_component_source_is_normalized_to_complete_provenance(self):
        emitter = LocalContextTraceEmitter(
            binding=self.binding,
            source={"kind": "local-emitter", "component": "context.dogfood"},
        )
        event = emitter.emit(
            "context.dogfood.delivery",
            evidence_refs=["run://m8-04/evidence/1"],
            observed_at="2026-08-15T03:00:01Z",
            event_id="event/m8-04/local-component",
        )
        self.assertEqual(
            event["source"],
            {
                "kind": "local-emitter",
                "provider": "local",
                "adapter": "context.dogfood",
                "source_ref": "component://context.dogfood",
            },
        )

    def test_local_emitter_rejects_corrupt_or_differently_bound_existing_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "events.jsonl"
            emitter = LocalContextTraceEmitter(
                binding=self.binding, source=self.source, output_path=output
            )
            emitter.emit(
                EVENT_NAMES[0],
                evidence_refs=["run://m8-04/evidence/1"],
                observed_at="2026-08-15T03:00:01Z",
                event_id="event/m8-04/1",
            )
            other_binding = dict(self.binding, state_revision=55)
            with self.assertRaisesRegex(ContextTraceError, "binding"):
                LocalContextTraceEmitter(
                    binding=other_binding, source=self.source, output_path=output
                )
            output.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaises(ContextTraceError):
                LocalContextTraceEmitter(
                    binding=self.binding, source=self.source, output_path=output
                )

    def test_otel_is_unavailable_without_exporter_and_success_requires_evidence_receipt(self):
        chain: list[dict] = []
        self._append(chain, EVENT_NAMES[0])
        unavailable = export_context_trace_to_otel(chain)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertFalse(unavailable["configured"])
        self.assertFalse(unavailable["attempted"])
        self.assertEqual(unavailable["exported_events"], 0)
        self.assertIsNone(unavailable["exporter_id"])
        self.assertIsNone(unavailable["evidence_ref"])
        self.assertFalse(unavailable["authority"])

        with self.assertRaisesRegex(ContextTraceError, "receipt"):
            export_context_trace_to_otel(chain, exporter=lambda events: True)

        exported = export_context_trace_to_otel(
            chain,
            exporter=lambda events: {
                "status": "exported",
                "exported_events": len(events),
                "exporter_id": "otel/sdk-test",
                "evidence_ref": "otel://collector/export/1",
            },
        )
        self.assertEqual(exported["status"], "exported")
        self.assertTrue(exported["configured"])
        self.assertTrue(exported["attempted"])
        self.assertEqual(exported["exported_events"], 1)
        self.assertEqual(exported["exporter_id"], "otel/sdk-test")
        self.assertEqual(exported["evidence_ref"], "otel://collector/export/1")
        self.assertFalse(exported["authority"])

    def test_otel_export_failure_is_explicit_and_never_reported_as_success(self):
        chain: list[dict] = []
        self._append(chain, EVENT_NAMES[0])

        def failing_exporter(events):
            raise RuntimeError("collector unavailable")

        receipt = export_context_trace_to_otel(chain, exporter=failing_exporter)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["configured"])
        self.assertTrue(receipt["attempted"])
        self.assertEqual(receipt["exported_events"], 0)
        self.assertIn("collector unavailable", receipt["unavailable_reason"])
        self.assertFalse(receipt["authority"])

    def test_otel_cannot_report_success_for_an_empty_trace_chain(self):
        with self.assertRaisesRegex(ContextTraceError, "empty"):
            export_context_trace_to_otel(
                [],
                exporter=lambda events: {
                    "status": "exported",
                    "exported_events": 0,
                    "exporter_id": "otel/sdk-test",
                    "evidence_ref": "otel://collector/export/empty",
                },
            )


if __name__ == "__main__":
    unittest.main()
