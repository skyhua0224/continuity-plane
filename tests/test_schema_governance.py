import hashlib
import json
import unittest
from pathlib import Path

import yaml

from context_control_plane.schema_governance import (
    SchemaGovernanceError,
    registry_digest,
    resolve_wire_version,
    validate_registry,
    validate_version_transition,
)


class SchemaGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.registry_path = cls.root / "schemas" / "registry.yaml"
        cls.registry = yaml.safe_load(cls.registry_path.read_text(encoding="utf-8"))

    def test_registry_entries_are_unique_and_hash_current_artifacts(self):
        validate_registry(self.registry, root=self.root)
        ids = [entry["schema_id"] for entry in self.registry["schemas"]]

        self.assertEqual(len(ids), len(set(ids)))
        for entry in self.registry["schemas"]:
            artifact = self.root / entry["artifact_path"]
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                entry["content_sha256"],
            )

    def test_registry_digest_is_stable_for_key_order(self):
        reordered = json.loads(json.dumps(self.registry))
        reordered["schemas"] = list(reversed(reordered["schemas"]))

        self.assertEqual(registry_digest(self.registry), registry_digest(reordered))

    def test_current_and_legacy_wire_versions_resolve(self):
        for entry in self.registry["schemas"]:
            resolved = resolve_wire_version(
                self.registry,
                entry["schema_id"],
                entry["current_wire_version"],
            )
            self.assertEqual(resolved["status"], "current")

    def test_unknown_wire_version_is_quarantined(self):
        with self.assertRaises(SchemaGovernanceError) as failure:
            resolve_wire_version(self.registry, "context.source-registry", "v0")

        self.assertIn("quarantine", str(failure.exception))

    def test_backward_compatible_change_requires_minor_or_major_bump(self):
        with self.assertRaises(SchemaGovernanceError):
            validate_version_transition(
                "1.0.0-alpha.1",
                "1.0.1-alpha.1",
                change_kind="backward-compatible",
                replay_passed=True,
            )

        validate_version_transition(
            "1.0.0-alpha.1",
            "1.1.0-alpha.1",
            change_kind="backward-compatible",
            replay_passed=True,
        )

    def test_breaking_change_requires_migration_rollback_and_replay(self):
        requirements = {
            "change_kind": "breaking",
            "replay_passed": True,
            "migration_present": True,
            "rollback_present": True,
        }
        validate_version_transition("1.0.0-alpha.1", "2.0.0-alpha.1", **requirements)

        requirements["rollback_present"] = False
        with self.assertRaises(SchemaGovernanceError):
            validate_version_transition("1.0.0-alpha.1", "2.0.0-alpha.1", **requirements)

    def test_registry_requires_replay_for_migration(self):
        entry = next(
            item for item in self.registry["schemas"] if item["schema_id"] == "context.provenance"
        )
        broken = json.loads(json.dumps(self.registry))
        broken_entry = next(
            item for item in broken["schemas"] if item["schema_id"] == entry["schema_id"]
        )
        broken_entry["migrations"] = [{"from": "m1-02.v0", "to": entry["current_wire_version"]}]

        with self.assertRaises(SchemaGovernanceError):
            validate_registry(broken, root=self.root)


if __name__ == "__main__":
    unittest.main()
