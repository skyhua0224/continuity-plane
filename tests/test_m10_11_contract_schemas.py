"""M10-11 strict study, segment, and derived report schema gates."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.live_continuity_probe import validate_live_continuity_study
from tests.test_m10_11_live_continuity_probe import M1011LiveContinuityProbeTests


class M1011ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m10-11"
        cls.schemas = {
            "context.live-continuity-study": "live-continuity-study.schema.json",
            "context.live-continuity-segment": "live-continuity-segment.schema.json",
            "context.live-continuity-report": "live-continuity-report.schema.json",
        }

    def _fixture(self) -> M1011LiveContinuityProbeTests:
        return M1011LiveContinuityProbeTests(
            "test_qualified_comparison_measures_window_token_and_response_improvement"
        )

    def _segments(self) -> list[dict]:
        return self._fixture()._qualified_segments()

    def _study(self) -> dict:
        return self._fixture()._study(self._segments())

    def test_runtime_documents_match_strict_schemas(self) -> None:
        fixture = self._fixture()
        segments = fixture._qualified_segments()
        report = fixture._qualified_segments()
        from context_control_plane.live_continuity_probe import (
            evaluate_live_continuity_comparison,
        )

        derived = evaluate_live_continuity_comparison(
            report,
            study=self._study(),
            report_id="report-m10-11-schema",
            observed_at="2026-08-20T18:00:00+08:00",
        )
        documents = {
            "context.live-continuity-study": self._study(),
            "context.live-continuity-segment": segments[0],
            "context.live-continuity-report": derived,
        }
        for schema_id, filename in self.schemas.items():
            schema = json.loads((self.schema_dir / filename).read_text(encoding="utf-8"))
            with self.subTest(schema_id=schema_id):
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(
                    schema, format_checker=FormatChecker()
                ).validate(documents[schema_id])

    def test_study_is_frozen_before_segments_and_rejects_posthoc_changes(self) -> None:
        study = self._study()
        validate_live_continuity_study(study)
        self.assertEqual(len(study["planned_segments"]), 12)
        self.assertEqual(study["minimum_samples_per_arm"], 3)
        self.assertEqual(study["schedule_kind"], "abba-balanced")

        changed = copy.deepcopy(study)
        changed["planned_segments"].pop()
        changed["study_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "planned segments|pair|digest"):
            validate_live_continuity_study(changed)

    def test_schemas_reject_raw_text_authority_and_unknown_fields(self) -> None:
        segment = self._segments()[0]
        schema = json.loads(
            (self.schema_dir / self.schemas["context.live-continuity-segment"]).read_text(
                encoding="utf-8"
            )
        )
        mutations = []
        raw = copy.deepcopy(segment)
        raw["raw_transcript"] = "private"
        mutations.append(raw)
        authority = copy.deepcopy(segment)
        authority["state_write_authority"] = True
        mutations.append(authority)
        self_assessed = copy.deepcopy(segment)
        self_assessed["responses"][0]["assessment_kind"] = "provider-under-test"
        mutations.append(self_assessed)
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                Draft202012Validator(schema).validate(mutation)

    def test_registry_entries_bind_exact_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        for schema_id, filename in self.schemas.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertIn(schema_id, entries)
                self.assertEqual(
                    entries[schema_id]["artifact_path"], f"schemas/m10-11/{filename}"
                )
                self.assertEqual(
                    entries[schema_id]["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
