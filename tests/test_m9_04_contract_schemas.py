"""M9-04 Context Health strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.context_health_benchmark import (
    benchmark_context_health_projection,
)
from context_control_plane.context_health_projection import (
    build_context_health_projection,
)
from tests.test_m9_04_context_health_projection import NOW, M904Fixture

SCHEMA_ID = "context.context-health-projection"
SCHEMA_FILE = "context-health-projection.schema.json"
BENCHMARK_SCHEMA_ID = "context.context-health-benchmark"
BENCHMARK_SCHEMA_FILE = "context-health-benchmark.schema.json"
METRIC_EVIDENCE_SCHEMA_ID = "context.metric-evidence"
METRIC_EVIDENCE_SCHEMA_FILE = "metric-evidence.schema.json"


class M904ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_path = cls.root / "schemas/m9-04" / SCHEMA_FILE
        cls.schema = json.loads(cls.schema_path.read_text(encoding="utf-8"))
        cls.benchmark_schema_path = (
            cls.root / "schemas/m9-04" / BENCHMARK_SCHEMA_FILE
        )
        cls.benchmark_schema = json.loads(
            cls.benchmark_schema_path.read_text(encoding="utf-8")
        )
        cls.metric_evidence_schema_path = (
            cls.root / "schemas/m9-04" / METRIC_EVIDENCE_SCHEMA_FILE
        )
        cls.metric_evidence_schema = json.loads(
            cls.metric_evidence_schema_path.read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(cls.schema)
        Draft202012Validator.check_schema(cls.benchmark_schema)
        Draft202012Validator.check_schema(cls.metric_evidence_schema)
        cls.validator = Draft202012Validator(
            cls.schema, format_checker=FormatChecker()
        )
        cls.benchmark_validator = Draft202012Validator(
            cls.benchmark_schema, format_checker=FormatChecker()
        )
        cls.metric_evidence_validator = Draft202012Validator(
            cls.metric_evidence_schema, format_checker=FormatChecker()
        )
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        store = LocalArtifactStore(Path(temporary.name) / "artifacts")
        store.initialize()
        fixture = M904Fixture(cls.root, store)
        cls.metric_evidence_sample = json.loads(
            fixture.artifacts[
                fixture.accounting_records[0]["receipt"]["routes"][0]["metrics"][
                    "retrieval_read_bytes"
                ]["evidence_ref"]
            ]
        )
        cls.sample = build_context_health_projection(
            source_projection=fixture.source_projection,
            decision_evidence_projection=fixture.decision_projection,
            provenance_bundle=fixture.provenance_bundle,
            trace_events=fixture.trace_events,
            compaction_records=fixture.compaction_records,
            accounting_records=fixture.accounting_records,
            reference_records=fixture.reference_records,
            harness_runs=fixture.harness_runs,
            harness_events=fixture.harness_events,
            recovery_records=fixture.recovery_records,
            artifact_store=fixture.artifact_store,
            provider_id="provider-docmost-health",
            observed_at=NOW,
            signer=fixture.signer,
            evidence_resolver=fixture.evidence_resolver,
            artifact_resolver=fixture.artifact_resolver,
            trusted_time_verifier=fixture.trusted_time_verifier,
        )
        cls.benchmark_sample = benchmark_context_health_projection(
            root=cls.root,
            iterations=2,
            generated_at="2026-08-17T23:00:00+08:00",
        )

    def test_runtime_projection_matches_strict_schema(self) -> None:
        self.validator.validate(self.sample)
        self.benchmark_validator.validate(self.benchmark_sample)
        self.metric_evidence_validator.validate(self.metric_evidence_sample)

    def test_schema_accepts_provider_neutral_context_event_names(self) -> None:
        projection = copy.deepcopy(self.sample)
        projection["context_health"]["event_summaries"][0]["event_name"] = (
            "context.custom.valid"
        )

        self.validator.validate(projection)

    def test_top_level_and_nested_contracts_reject_extra_fields(self) -> None:
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(set(self.schema["required"]), set(self.schema["properties"]))
        targets = (
            ("source_bindings", None),
            ("context_health", None),
            ("context_health", "event_summaries"),
            ("context_health", "compaction_summaries"),
            ("context_health", "accounting_summaries"),
            ("context_health", "slo_results"),
            ("reference_health", None),
            ("reference_health", "records"),
            ("harness_health", None),
            ("harness_health", "runs"),
            ("replay_health", None),
            ("replay_health", "recoveries"),
            ("drilldowns", None),
            ("authority", None),
            ("signature", None),
        )
        for parent, child in targets:
            forged = copy.deepcopy(self.sample)
            if parent == "drilldowns":
                target = forged[parent][0]
            elif child is None:
                target = forged[parent]
            else:
                target = forged[parent][child][0]
            target["unexpected"] = True
            with self.subTest(parent=parent, child=child), self.assertRaises(
                ValidationError
            ):
                self.validator.validate(forged)

        forged_benchmark = copy.deepcopy(self.benchmark_sample)
        forged_benchmark["results"]["unexpected"] = True
        with self.assertRaises(ValidationError):
            self.benchmark_validator.validate(forged_benchmark)

        forged_evidence = copy.deepcopy(self.metric_evidence_sample)
        forged_evidence["unexpected"] = True
        with self.assertRaises(ValidationError):
            self.metric_evidence_validator.validate(forged_evidence)

    def test_authority_and_unavailable_measurements_are_exact(self) -> None:
        for field, value in (
            ("state_write_authority", True),
            ("completion_authority", True),
            ("approval_authority", True),
            ("provider_authority", False),
            ("external_effect_authority", 1),
        ):
            forged = copy.deepcopy(self.sample)
            forged["authority"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.validator.validate(forged)

        forged = copy.deepcopy(self.sample)
        forged["context_health"]["context_window_status"] = "measured"
        with self.assertRaises(ValidationError):
            self.validator.validate(forged)

    def test_registry_entry_binds_exact_schema_hash(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        for schema_id, filename, path, schema in (
            (SCHEMA_ID, SCHEMA_FILE, self.schema_path, self.schema),
            (
                BENCHMARK_SCHEMA_ID,
                BENCHMARK_SCHEMA_FILE,
                self.benchmark_schema_path,
                self.benchmark_schema,
            ),
            (
                METRIC_EVIDENCE_SCHEMA_ID,
                METRIC_EVIDENCE_SCHEMA_FILE,
                self.metric_evidence_schema_path,
                self.metric_evidence_schema,
            ),
        ):
            entry = entries[schema_id]
            wire_version = schema["properties"]["schema_version"]["const"]
            self.assertEqual(entry["current_wire_version"], wire_version)
            self.assertEqual(entry["supported_wire_versions"], [wire_version])
            self.assertEqual(entry["artifact_path"], f"schemas/m9-04/{filename}")
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
