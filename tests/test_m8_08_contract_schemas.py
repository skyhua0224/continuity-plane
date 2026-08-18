"""M8-08 forge collaboration strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.forge_collaboration import (
    build_ref_update_intent,
    project_forge_snapshot,
    project_unpublished_work,
)
from context_control_plane.forge_collaboration_benchmark import (
    benchmark_forge_collaboration,
)


SCHEMAS = {
    "context.forge-work-projection": "forge-work-projection.schema.json",
    "context.forge-ref-update-intent": "forge-ref-update-intent.schema.json",
    "context.forge-unpublished-work": "forge-unpublished-work.schema.json",
    "context.forge-collaboration-benchmark": "forge-collaboration-benchmark.schema.json",
}


def _snapshot() -> dict:
    return {
        "provider": "gitea",
        "instance_id": "forge.example",
        "repository": {"owner": "example", "name": "relay"},
        "source_revision": "gitea-delivery-20",
        "observed_at": "2026-08-16T16:30:00Z",
        "issues": [
            {
                "index": 14,
                "title": "Project visible work",
                "state": "open",
                "assignees": [{"login": "alice"}],
            }
        ],
        "pull_requests": [
            {
                "index": 20,
                "issue_index": 14,
                "state": "open",
                "head": {"ref": "feat/visible-work", "sha": "a" * 40},
                "assignees": [{"login": "alice"}],
                "requested_reviewers": [{"login": "bob"}],
                "reviews": [{"user": {"login": "bob"}, "state": "approved"}],
                "statuses": [{"context": "unit", "state": "success"}],
            }
        ],
        "refs": {"refs/heads/feat/visible-work": "a" * 40},
    }


class M808ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-08"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

    @classmethod
    def samples(cls) -> dict[str, dict]:
        projection = project_forge_snapshot(_snapshot())
        intent = build_ref_update_intent(
            projection,
            branch_ref="refs/heads/feat/visible-work",
            expected_remote_oid="a" * 40,
            desired_oid="b" * 40,
        )
        unpublished = project_unpublished_work(
            {
                "work_id": "local-m8-08",
                "actor_ref": "actor://alice",
                "local_ref": "worktree://alice/m8-08",
                "observed_at": "2026-08-16T16:30:00Z",
            }
        )
        return {
            "context.forge-work-projection": projection,
            "context.forge-ref-update-intent": intent,
            "context.forge-unpublished-work": unpublished,
            "context.forge-collaboration-benchmark": benchmark_forge_collaboration(
                root=cls.root,
                samples=2,
                generated_at="2026-08-17T00:00:00Z",
            ),
        }

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples().items():
            with self.subTest(schema_id=schema_id):
                Draft202012Validator(
                    self.schemas[schema_id], format_checker=FormatChecker()
                ).validate(sample)

    def test_all_top_level_fields_are_required_and_unknown_fields_rejected(self) -> None:
        for schema_id, sample in self.samples().items():
            schema = self.schemas[schema_id]
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id, mutation="extra"), self.assertRaises(
                ValidationError
            ):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(
                    schema_id=schema_id, missing=field
                ), self.assertRaises(ValidationError):
                    validator.validate(missing)

    def test_registry_entries_match_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {
            entry["schema_id"]: entry
            for entry in registry["schemas"]
            if entry["schema_id"] in SCHEMAS
        }
        self.assertEqual(set(entries), set(SCHEMAS))
        for schema_id, filename in SCHEMAS.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertEqual(
                    entries[schema_id]["artifact_path"],
                    path.relative_to(self.root).as_posix(),
                )
                self.assertEqual(
                    entries[schema_id]["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
