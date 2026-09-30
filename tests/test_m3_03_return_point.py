import copy
import tempfile
import unittest
from pathlib import Path

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.checkpoint import (
    CheckpointStaleError,
    publish_checkpoint,
    restore_checkpoint,
    verify_historical_checkpoint,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore


class M303HistoricalReturnPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml

        fixture_set = yaml.safe_load(
            (Path(__file__).parents[1] / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.base = copy.deepcopy(fixture_set["cases"][0]["document"])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.artifacts = LocalArtifactStore(root / "artifacts")
        self.artifacts.initialize()
        self.store = SQLiteStateStore(root / "state.sqlite3")
        self.store.initialize()
        self.registry_digest = "a" * 64
        self.canonical_plan_sha256 = "b" * 64
        self.snapshot = copy.deepcopy(self.base)
        self.store.create_project(copy.deepcopy(self.snapshot))
        self.checkpoint_ref = publish_checkpoint(
            {
                "snapshot": self.snapshot,
                "revision": self.snapshot["project"]["revision"],
                "event_head": None,
                "registry_digest": self.registry_digest,
                "capabilities": {
                    "schema_version": "context.state-store-capabilities/v1alpha1",
                    "adapter_id": "context.sqlite",
                    "adapter_version": "1.0.0-alpha.1",
                    "authority_mode": "local",
                    "operations": ["create_project", "read_project", "read_events", "commit_event"],
                    "shared_authority": False,
                    "offline_write": True,
                    "unique_claim": False,
                    "multi_writer": True,
                    "lease_clock": "none",
                    "artifact_scope": "none",
                    "expected_revision": True,
                    "migration_source": False,
                    "migration_target": False,
                },
            },
            self.artifacts,
            canonical_plan_sha256=self.canonical_plan_sha256,
        )

    def test_historical_checkpoint_stays_readable_after_authority_advances(self):
        binding = {
            "project_id": self.snapshot["project"]["project_id"],
            "checkpoint_ref": self.checkpoint_ref.to_document(),
            "checkpoint_revision": self.snapshot["project"]["revision"],
            "checkpoint_event_head": None,
            "return_work_id": self.snapshot["project"]["primary_work_id"],
            "return_work_revision": next(
                work["revision"]
                for work in self.snapshot["works"]
                if work["work_id"] == self.snapshot["project"]["primary_work_id"]
            ),
        }

        restored = verify_historical_checkpoint(
            self.checkpoint_ref,
            self.artifacts,
            binding=binding,
            expected_plan_sha256=self.canonical_plan_sha256,
            expected_registry_digest=self.registry_digest,
        )

        self.assertEqual(restored.snapshot, self.snapshot)
        with self.assertRaises(CheckpointStaleError):
            restore_checkpoint(
                self.checkpoint_ref,
                self.artifacts,
                expected_project_id=self.snapshot["project"]["project_id"],
                expected_revision=self.snapshot["project"]["revision"] + 1,
                expected_event_head=None,
                expected_governance_ref=self.snapshot["project"]["governance_ref"],
                expected_plan_sha256=self.canonical_plan_sha256,
                expected_registry_digest=self.registry_digest,
            )

    def test_historical_checkpoint_rejects_unrelated_binding(self):
        binding = {
            "project_id": self.snapshot["project"]["project_id"],
            "checkpoint_ref": self.checkpoint_ref.to_document(),
            "checkpoint_revision": self.snapshot["project"]["revision"],
            "checkpoint_event_head": None,
            "return_work_id": "missing-work",
            "return_work_revision": 1,
        }

        with self.assertRaises(CheckpointStaleError):
            verify_historical_checkpoint(
                self.checkpoint_ref,
                self.artifacts,
                binding=binding,
                expected_plan_sha256=self.canonical_plan_sha256,
                expected_registry_digest=self.registry_digest,
            )


if __name__ == "__main__":
    unittest.main()
