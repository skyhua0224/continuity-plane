"""M9-03 Decision and Evidence strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from context_control_plane.decision_evidence_benchmark import (
    benchmark_decision_evidence_projection,
)
from context_control_plane.decision_evidence_projection import (
    build_decision_evidence_projection,
)
from tests.test_m9_03_decision_evidence_projection import (
    _bundle_digest,
    _external_projection,
    _snapshot,
)

SCHEMAS = {
    "context.decision-evidence-projection": (
        "decision-evidence-projection.schema.json"
    ),
    "context.decision-evidence-provenance-bundle": (
        "decision-evidence-provenance-bundle.schema.json"
    ),
    "context.decision-evidence-benchmark": (
        "decision-evidence-benchmark.schema.json"
    ),
}


class M903ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m9-03"
        cls.schemas = {
            schema_id: json.loads(
                (cls.schema_dir / filename).read_text(encoding="utf-8")
            )
            for schema_id, filename in SCHEMAS.items()
        }
        external_schemas = [
            json.loads(
                (cls.root / relative).read_text(encoding="utf-8")
            )
            for relative in (
                "schemas/m7-01/assertion-provenance.schema.json",
                "schemas/m7-02/claim-evidence-gate.schema.json",
            )
        ]
        for schema in [*cls.schemas.values(), *external_schemas]:
            Draft202012Validator.check_schema(schema)
        cls.registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in [*cls.schemas.values(), *external_schemas]
        )

        snapshot = _snapshot()
        from context_control_plane.external_state_provider import (
            HMACExternalStateProjectionSigner,
        )

        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-03-schema",
            secret=b"m9-03-schema-test-key",
        )
        source = _external_projection(
            snapshot, cls.signer, request_id="request-m9-03-schema"
        )
        bundle = {
            "schema_version": "context.decision-evidence-provenance-bundle/v1alpha1",
            "project_id": snapshot["project"]["project_id"],
            "state_revision": snapshot["project"]["revision"],
            "state_sha256": source["state_sha256"],
            "source_projection_sha256": source["projection_sha256"],
            "assertion_records": [],
            "claims": [],
            "claim_verdicts": [],
            "bindings": [],
            "bundle_sha256": "",
        }
        bundle["bundle_sha256"] = _bundle_digest(bundle)
        cls.samples = {
            "context.decision-evidence-provenance-bundle": bundle,
            "context.decision-evidence-projection": (
                build_decision_evidence_projection(
                    source,
                    signer=cls.signer,
                    observed_at="2026-08-17T20:00:00+08:00",
                    provenance_bundle=bundle,
                )
            ),
            "context.decision-evidence-benchmark": (
                benchmark_decision_evidence_projection(
                    root=cls.root,
                    iterations=2,
                    generated_at="2026-08-17T22:00:00+08:00",
                )
            ),
        }

    def validator(self, schema_id: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[schema_id],
            registry=self.registry,
            format_checker=FormatChecker(),
        )

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples.items():
            with self.subTest(schema_id=schema_id):
                self.validator(schema_id).validate(sample)

    def test_top_level_and_projection_nested_contracts_are_strict(self) -> None:
        for schema_id, sample in self.samples.items():
            schema = self.schemas[schema_id]
            validator = self.validator(schema_id)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id), self.assertRaises(
                ValidationError
            ):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(schema_id=schema_id, field=field), self.assertRaises(
                    ValidationError
                ):
                    validator.validate(missing)

        projection = self.samples["context.decision-evidence-projection"]
        nested = (
            projection["capabilities"],
            projection["decision_timeline"][0],
            projection["constraint_matrix"],
            projection["evidence_matrix"][0],
            projection["evidence_matrix"][0]["referencing_objects"][0],
            projection["health"],
            projection["authority"],
        )
        validator = self.validator("context.decision-evidence-projection")
        for index, item in enumerate(nested):
            forged = copy.deepcopy(projection)
            if index == 0:
                target = forged["capabilities"]
            elif index == 1:
                target = forged["decision_timeline"][0]
            elif index == 2:
                forged["constraint_matrix"] = {"unexpected": True}
                with self.assertRaises(ValidationError):
                    validator.validate(forged)
                continue
            elif index == 3:
                target = forged["evidence_matrix"][0]
            elif index == 4:
                target = forged["evidence_matrix"][0]["referencing_objects"][0]
            elif index == 5:
                target = forged["health"]
            else:
                target = forged["authority"]
            target["unexpected"] = True
            with self.assertRaises(ValidationError):
                validator.validate(forged)

    def test_authority_and_failed_capability_types_are_exact(self) -> None:
        projection = self.samples["context.decision-evidence-projection"]
        validator = self.validator("context.decision-evidence-projection")
        for field, value in (
            ("state_write_authority", True),
            ("completion_authority", True),
            ("approval_authority", True),
            ("provider_authority", False),
            ("external_effect_authority", 1),
        ):
            forged = copy.deepcopy(projection)
            forged["authority"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validator.validate(forged)

        forged = copy.deepcopy(projection)
        forged["capabilities"]["typed_evidence"] = 1
        with self.assertRaises(ValidationError):
            validator.validate(forged)

        benchmark = copy.deepcopy(
            self.samples["context.decision-evidence-benchmark"]
        )
        benchmark["gate"] = {
            "status": "failed",
            "failed_gates": ["same-revision"],
        }
        with self.assertRaises(ValidationError):
            self.validator("context.decision-evidence-benchmark").validate(benchmark)

    def test_registry_entries_bind_exact_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        for schema_id, filename in SCHEMAS.items():
            entry = entries[schema_id]
            path = self.schema_dir / filename
            wire_version = self.schemas[schema_id]["properties"]["schema_version"][
                "const"
            ]
            self.assertEqual(entry["current_wire_version"], wire_version)
            self.assertEqual(entry["supported_wire_versions"], [wire_version])
            self.assertEqual(entry["artifact_path"], f"schemas/m9-03/{filename}")
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(entry["status"], "current")
            self.assertEqual(entry["compatibility_mode"], "strict-versioned")


if __name__ == "__main__":
    unittest.main()
