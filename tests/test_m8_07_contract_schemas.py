"""M8-07 strict schema and registry admission tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.project_adaptation import ProjectAdaptationLoop
from context_control_plane.project_adaptation_benchmark import benchmark_project_adaptation
from tests.test_m8_07_project_adaptation import NOW, _changes, _metrics


SCHEMAS = {
    "context.project-adaptation-observation": "project-adaptation-observation.schema.json",
    "context.project-adaptation-proposal": "project-adaptation-proposal.schema.json",
    "context.project-adaptation-replay": "project-adaptation-replay.schema.json",
    "context.project-adaptation-transition": "project-adaptation-transition.schema.json",
    "context.project-adaptation-benchmark": "project-adaptation-benchmark.schema.json",
}


class M807ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-07"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

    @classmethod
    def samples(cls) -> dict[str, dict]:
        loop = ProjectAdaptationLoop(
            project_id="project-m8-07",
            profile_id="profile-m8-07",
            clock=lambda: NOW,
        )
        observation = loop.observe(
            run_ref="run://verified-1",
            state_revision=7,
            verified=True,
            metrics=_metrics(),
            correction_refs=["correction://compact-output"],
            failure_fixture_refs=["fixture://repeat-read"],
        )
        proposal = loop.propose(
            observation_refs=[observation["observation_id"]],
            version="1.0.0",
            scope="project",
            applicability=[{"kind": "project", "ref": "project://project-m8-07"}],
            changes=_changes(),
            metrics_before=_metrics(),
            metrics_after=_metrics(repeated_read_bytes=50),
        )
        replay = loop.shadow(
            proposal["adaptation_id"],
            fixture_ref="fixture://repeat-read",
            provider_id="codex",
            budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
            attempts=3,
        )
        loop.approve(proposal["adaptation_id"], approval_ref="approval://m8-07/1", approval_round=1)
        transition = loop.activate(proposal["adaptation_id"])
        benchmark = benchmark_project_adaptation(root=cls.root, samples=2, generated_at=NOW)
        return {
            "context.project-adaptation-observation": observation,
            "context.project-adaptation-proposal": loop.proposals[proposal["adaptation_id"]],
            "context.project-adaptation-replay": replay,
            "context.project-adaptation-transition": transition,
            "context.project-adaptation-benchmark": benchmark,
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

    def test_presentation_preferences_reject_unknown_keys(self) -> None:
        sample = self.samples()["context.project-adaptation-proposal"]
        schema = self.schemas["context.project-adaptation-proposal"]
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        mutated = copy.deepcopy(sample)
        mutated["changes"]["presentation_preferences"]["unsafe_override"] = "true"
        with self.assertRaises(ValidationError):
            validator.validate(mutated)

    def test_registry_entries_match_schema_hashes(self) -> None:
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        entries = {entry["schema_id"]: entry for entry in registry["schemas"] if entry["schema_id"] in SCHEMAS}
        self.assertEqual(set(entries), set(SCHEMAS))
        for schema_id, filename in SCHEMAS.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertEqual(entries[schema_id]["artifact_path"], path.relative_to(self.root).as_posix())
                self.assertEqual(entries[schema_id]["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
