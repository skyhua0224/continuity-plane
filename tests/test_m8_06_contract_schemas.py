"""M8-06 strict schema and registry admission tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.harness_run import HarnessCoordinator
from context_control_plane.harness_run_benchmark import (
    benchmark_harness_run,
    provider_drift_rejection_reason,
)
from tests.test_m8_06_harness_run import NOW, _run

SCHEMAS = {
    "context.harness-run": "harness-run.schema.json",
    "context.harness-event": "harness-event.schema.json",
    "context.harness-handoff": "harness-handoff.schema.json",
    "context.harness-benchmark": "harness-benchmark.schema.json",
}


class M806ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-06"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

    @classmethod
    def samples(cls) -> dict[str, dict]:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
        )
        run = _run()
        coordinator.register_run(run)
        worker = _run(run_id="run-worker-schema", parent_run_id="run-parent", claim_id="claim-worker-schema")
        coordinator.dispatch_worker("run-parent", worker)
        coordinator.runs["run-parent"]["status"] = "waiting"
        handoff = coordinator.create_handoff(
            "run-parent",
            "run-worker-schema",
            checkpoint_id="checkpoint-m8-06",
            next_action="run verifier",
            expected_task_revision=7,
        )
        benchmark = benchmark_harness_run(root=cls.root, samples=1, generated_at=NOW)
        return {
            "context.harness-run": run,
            "context.harness-event": coordinator.events[0],
            "context.harness-handoff": handoff,
            "context.harness-benchmark": benchmark,
        }

    def test_runtime_documents_match_strict_schemas(self) -> None:
        samples = self.samples()
        for schema_id, sample in samples.items():
            with self.subTest(schema_id=schema_id):
                Draft202012Validator(self.schemas[schema_id], format_checker=FormatChecker()).validate(sample)

    def test_required_and_unknown_fields_are_enforced(self) -> None:
        for schema_id, sample in self.samples().items():
            schema = self.schemas[schema_id]
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id, mutation="extra"), self.assertRaises(ValidationError):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(schema_id=schema_id, missing=field), self.assertRaises(ValidationError):
                    validator.validate(missing)

    def test_registry_entries_match_schema_hashes(self) -> None:
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        entries = {entry["schema_id"]: entry for entry in registry["schemas"] if entry["schema_id"] in SCHEMAS}
        self.assertEqual(set(entries), set(SCHEMAS))
        for schema_id, filename in SCHEMAS.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertEqual(entries[schema_id]["artifact_path"], path.relative_to(self.root).as_posix())
                self.assertEqual(entries[schema_id]["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_benchmark_provider_drift_case_reports_contract_drift(self) -> None:
        self.assertIn("provider contract drift", provider_drift_rejection_reason(0))


if __name__ == "__main__":
    unittest.main()
