"""M8-04 context trace replay and coverage benchmark tests."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane.context_trace import (
    export_context_trace_to_otel,
    validate_context_trace_chain,
)
from context_control_plane.context_trace_benchmark import (
    REQUIRED_EVENT_NAMES,
    benchmark_context_trace,
    benchmark_context_trace_fixture,
    validate_context_trace_benchmark,
)


class M804ContextTraceBenchmarkTests(unittest.TestCase):
    def test_committed_receipt_and_strict_schemas_are_current(self):
        root = Path(__file__).parents[1]
        schemas = {
            name: json.loads((root / relative).read_text(encoding="utf-8"))
            for name, relative in {
                "event": "schemas/m8-04/context-trace-event.schema.json",
                "otel": "schemas/m8-04/otel-export-receipt.schema.json",
                "benchmark": "schemas/m8-04/context-trace-benchmark.schema.json",
            }.items()
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)

        fixture = benchmark_context_trace_fixture()
        event_validator = Draft202012Validator(
            schemas["event"], format_checker=FormatChecker()
        )
        for event in fixture["events"]:
            event_validator.validate(event)
        forged_event = copy.deepcopy(fixture["events"][0])
        forged_event["authority"] = True
        self.assertFalse(event_validator.is_valid(forged_event))
        extra_event = copy.deepcopy(fixture["events"][0])
        extra_event["unexpected"] = True
        self.assertFalse(event_validator.is_valid(extra_event))

        unavailable = export_context_trace_to_otel(fixture["events"])
        exported = export_context_trace_to_otel(
            fixture["events"],
            exporter=lambda events: {
                "status": "exported",
                "exported_events": len(events),
                "exporter_id": "otel/schema-probe",
                "evidence_ref": "otel://schema-probe/export/1",
            },
        )
        otel_validator = Draft202012Validator(
            schemas["otel"], format_checker=FormatChecker()
        )
        otel_validator.validate(unavailable)
        otel_validator.validate(exported)
        forged_export = copy.deepcopy(unavailable)
        forged_export["status"] = "exported"
        self.assertFalse(otel_validator.is_valid(forged_export))

        receipt = json.loads(
            (root / "experiments/observability/m8-04-context-trace-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_context_trace_benchmark(receipt)
        Draft202012Validator(
            schemas["benchmark"], format_checker=FormatChecker()
        ).validate(receipt)
        self.assertEqual(receipt["samples"], 1000)
        current = benchmark_context_trace(samples=1)
        self.assertEqual(receipt["fixture_sha256"], current["fixture_sha256"])
        self.assertEqual(receipt["implementation_sha256"], current["implementation_sha256"])

    def test_fixture_covers_required_observations_with_complete_binding(self):
        fixture = benchmark_context_trace_fixture()
        events = fixture["events"]
        validate_context_trace_chain(events)
        self.assertEqual({event["event_name"] for event in events}, set(REQUIRED_EVENT_NAMES))
        for event in events:
            self.assertEqual(event["project_id"], "project/context-control-plane")
            self.assertEqual(event["state_revision"], 54)
            self.assertEqual(event["active_work_id"], "M5-07")
            self.assertTrue(event["source"]["source_ref"])
            self.assertTrue(event["evidence_refs"])
            self.assertFalse(event["authority"])

    def test_replay_benchmark_has_full_coverage_and_no_false_otel_claim(self):
        receipt = benchmark_context_trace(samples=64, generated_at="2026-08-15T04:00:00Z")
        validate_context_trace_benchmark(receipt)
        self.assertEqual(receipt["samples"], 64)
        self.assertEqual(receipt["successful_samples"], 64)
        self.assertEqual(receipt["total_events"], 64 * len(REQUIRED_EVENT_NAMES))
        self.assertEqual(receipt["required_event_coverage_millionths"], 1_000_000)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["hash_chain_failures"], 0)
        self.assertEqual(receipt["binding_failures"], 0)
        self.assertEqual(receipt["source_evidence_failures"], 0)
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["otel_unavailable_samples"], 64)
        self.assertEqual(receipt["false_otel_success"], 0)
        self.assertEqual(receipt["external_services"], 0)
        self.assertGreaterEqual(receipt["p95_ms"], 0)

    def test_benchmark_is_strict_and_rejects_false_acceptance(self):
        receipt = benchmark_context_trace(samples=4)
        for field, value in (
            ("successful_samples", 3),
            ("total_events", 31),
            ("required_event_coverage_millionths", 999_999),
            ("replay_mismatch", 1),
            ("hash_chain_failures", 1),
            ("binding_failures", 1),
            ("source_evidence_failures", 1),
            ("authority_violations", 1),
            ("otel_unavailable_samples", 3),
            ("false_otel_success", 1),
            ("external_services", 1),
        ):
            changed = copy.deepcopy(receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_context_trace_benchmark(changed)

        changed = copy.deepcopy(receipt)
        changed["unexpected"] = True
        with self.assertRaises(ValueError):
            validate_context_trace_benchmark(changed)

    def test_sample_count_must_be_positive(self):
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark_context_trace(samples=value)


if __name__ == "__main__":
    unittest.main()
