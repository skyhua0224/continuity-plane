import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.artifact_store import ArtifactRef, LocalArtifactStore
from context_control_plane.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointInputError,
    CheckpointIntegrityError,
    CheckpointSizeError,
    CheckpointStaleError,
    publish_checkpoint,
    restore_checkpoint,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext, StateMCPService


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class _ReadCountingStore:
    def __init__(self, delegate):
        self.delegate = delegate
        self.read_refs = []

    def read(self, ref):
        self.read_refs.append(ref)
        return self.delegate.read(ref)


class M206CheckpointCanaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixtures = yaml.safe_load(
            (
                cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml"
            ).read_text(encoding="utf-8")
        )
        cls.base_snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixtures["cases"]
                if case["case_id"] == "solo-active-work"
            )
        )

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LocalArtifactStore(
            Path(self.temporary_directory.name) / "artifacts"
        )
        self.store.initialize()
        self.snapshot = self._snapshot_with_terminal_objects()
        self.event_head = {"sequence_no": 7, "event_sha256": "b" * 64}
        self.plan_digest = "c" * 64
        self.registry_digest = "d" * 64
        self.governance_ref = self.snapshot["project"]["governance_ref"]
        self.read_result = {
            "snapshot": copy.deepcopy(self.snapshot),
            "revision": self.snapshot["project"]["revision"],
            "event_head": copy.deepcopy(self.event_head),
            "registry_digest": self.registry_digest,
            "capabilities": {
                "schema_version": "context.state-store-capabilities/v1alpha1",
                "adapter_id": "context.sqlite",
                "adapter_version": "1.0.0-alpha.1",
                "authority_mode": "local",
                "operations": [
                    "create_project",
                    "read_project",
                    "read_events",
                    "commit_event",
                ],
                "shared_authority": False,
                "offline_write": True,
                "unique_claim": False,
                "multi_writer": True,
                "lease_clock": "process",
                "artifact_scope": "local",
                "expected_revision": True,
                "migration_source": True,
                "migration_target": True,
            },
        }

    def _snapshot_with_terminal_objects(self):
        snapshot = copy.deepcopy(self.base_snapshot)
        snapshot["project"]["open_blocker_ids"] = ["blocker-open"]
        snapshot["project"]["effect_high_watermark"] = 2
        snapshot["works"].extend(
            [
                {
                    "work_id": "work-completed",
                    "kind": "work",
                    "title": "Previously completed work",
                    "status": "completed",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": ["actor-owner"],
                    "scope_refs": [],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": ["evidence-solo"],
                    "blocker_ids": [],
                    "revision": 1,
                },
                {
                    "work_id": "work-rejected",
                    "kind": "work",
                    "title": "Rejected work",
                    "status": "rejected",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": [],
                    "scope_refs": [],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": ["blocker-open"],
                    "revision": 1,
                },
            ]
        )
        snapshot["decisions"].extend(
            [
                {
                    "decision_id": "decision-rejected",
                    "work_id": "work-rejected",
                    "status": "rejected",
                    "statement": "Do not reactivate rejected work.",
                    "decided_at": "2026-08-09T08:55:00+08:00",
                    "supersedes_decision_id": None,
                    "evidence_ids": [],
                },
                {
                    "decision_id": "decision-reverted",
                    "work_id": "work-completed",
                    "status": "reverted",
                    "statement": "Preserve the reverted decision as terminal history.",
                    "decided_at": "2026-08-09T08:56:00+08:00",
                    "supersedes_decision_id": None,
                    "evidence_ids": [],
                },
            ]
        )
        snapshot["blockers"].append(
            {
                "blocker_id": "blocker-open",
                "status": "open",
                "reason": "Rejected work remains blocked from reactivation.",
                "blocked_work_ids": ["work-rejected"],
                "evidence_ids": ["evidence-solo"],
                "opened_at": "2026-08-09T08:57:00+08:00",
                "resolved_at": None,
                "supersedes_blocker_id": None,
            }
        )
        snapshot["effects"].append(
            {
                "effect_id": "effect-succeeded",
                "effect_key": "effect-key-succeeded",
                "work_id": "work-solo",
                "claim_id": "claim-solo",
                "status": "succeeded",
                "operation": "publish-checkpoint",
                "scope_ref": {"scope_kind": "file", "scope_ref": "src/core.py"},
                "expected_project_revision": 7,
                "sequence_no": 2,
                "evidence_ids": ["evidence-solo"],
                "result_ref": "artifact://fixture/checkpoint-result",
                "requested_at": "2026-08-09T09:11:00+08:00",
                "completed_at": "2026-08-09T09:12:00+08:00",
            }
        )
        return snapshot

    def _publish(self):
        return publish_checkpoint(
            self.read_result,
            self.store,
            canonical_plan_sha256=self.plan_digest,
        )

    def _restore(self, checkpoint_ref, **overrides):
        arguments = {
            "expected_project_id": self.snapshot["project"]["project_id"],
            "expected_revision": self.snapshot["project"]["revision"],
            "expected_event_head": copy.deepcopy(self.event_head),
            "expected_governance_ref": self.governance_ref,
            "expected_plan_sha256": self.plan_digest,
            "expected_registry_digest": self.registry_digest,
        }
        arguments.update(overrides)
        return restore_checkpoint(checkpoint_ref, self.store, **arguments)

    def _replace_manifest(self, checkpoint_ref, transform):
        document = json.loads(self.store.read(checkpoint_ref).decode("utf-8"))
        transform(document)
        return self.store.put_bytes(
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def test_checkpoint_schema_is_strict_registered_and_hashed(self):
        schema_path = self.root / "schemas" / "m2-06" / "checkpoint-manifest.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.checkpoint-manifest"
        )

        self.assertEqual(CHECKPOINT_SCHEMA_VERSION, "context.checkpoint-manifest/v1alpha1")
        self.assertEqual(
            entry["artifact_path"],
            "schemas/m2-06/checkpoint-manifest.schema.json",
        )
        self.assertEqual(
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            entry["content_sha256"],
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

        checkpoint_ref = self._publish()
        manifest = json.loads(self.store.read(checkpoint_ref).decode("utf-8"))
        validator = Draft202012Validator(schema)
        validator.validate(manifest)
        invalid = copy.deepcopy(manifest)
        invalid["unexpected"] = True
        with self.assertRaises(ValidationError):
            validator.validate(invalid)

    def test_publish_and_restore_round_trip_all_critical_fields_deterministically(self):
        first_ref = self._publish()
        second_ref = self._publish()
        restored = self._restore(first_ref)

        self.assertEqual(first_ref, second_ref)
        self.assertEqual(restored.checkpoint_ref, first_ref)
        self.assertEqual(restored.snapshot, self.snapshot)
        self.assertEqual(restored.manifest["project_id"], "project-solo")
        self.assertEqual(restored.manifest["revision"], 7)
        self.assertEqual(restored.manifest["governance_ref"], self.snapshot["project"]["governance_ref"])
        self.assertEqual(restored.manifest["canonical_plan_sha256"], self.plan_digest)
        self.assertEqual(restored.manifest["registry_digest"], self.registry_digest)
        self.assertEqual(restored.manifest["event_head"], self.event_head)
        self.assertEqual(restored.manifest["active_work_ids"], ["work-solo"])
        self.assertEqual(restored.manifest["primary_work_id"], "work-solo")
        self.assertEqual(
            restored.manifest["terminal_work_ids"],
            ["work-completed", "work-rejected"],
        )
        self.assertEqual(restored.manifest["accepted_decision_ids"], ["decision-solo"])
        self.assertEqual(
            restored.manifest["terminal_decision_ids"],
            ["decision-rejected", "decision-reverted"],
        )
        self.assertEqual(restored.manifest["active_constraint_ids"], ["constraint-solo"])
        self.assertEqual(restored.manifest["open_blocker_ids"], ["blocker-open"])
        self.assertEqual(restored.manifest["active_claim_ids"], ["claim-solo"])
        self.assertEqual(restored.manifest["in_flight_effect_ids"], ["effect-solo"])
        self.assertEqual(restored.manifest["terminal_effect_ids"], ["effect-succeeded"])
        self.assertEqual(restored.manifest["effect_high_watermark"], 2)
        self.assertEqual(
            restored.manifest["captured_state_updated_at"],
            self.snapshot["project"]["updated_at"],
        )
        self.assertRegex(restored.manifest["critical_projection_sha256"], r"^[0-9a-f]{64}$")

    def test_publication_and_restore_are_defensive_against_input_mutation(self):
        checkpoint_ref = self._publish()
        self.read_result["snapshot"]["project"]["primary_work_id"] = None
        first = self._restore(checkpoint_ref)
        first.snapshot["project"]["primary_work_id"] = None
        first.manifest["primary_work_id"] = None
        second = self._restore(checkpoint_ref)

        self.assertEqual(second.snapshot["project"]["primary_work_id"], "work-solo")
        self.assertEqual(second.manifest["primary_work_id"], "work-solo")

    def test_publish_rejects_invalid_or_torn_state_mcp_read_results(self):
        cases = []
        wrong_revision = copy.deepcopy(self.read_result)
        wrong_revision["revision"] += 1
        cases.append(("revision", wrong_revision))
        bad_registry = copy.deepcopy(self.read_result)
        bad_registry["registry_digest"] = "not-a-digest"
        cases.append(("registry", bad_registry))
        bad_head = copy.deepcopy(self.read_result)
        bad_head["event_head"]["sequence_no"] = 0
        cases.append(("event head", bad_head))
        bad_head_hash = copy.deepcopy(self.read_result)
        bad_head_hash["event_head"]["event_sha256"] = "not-a-digest"
        cases.append(("event head hash", bad_head_hash))
        bad_snapshot = copy.deepcopy(self.read_result)
        bad_snapshot["snapshot"]["project"]["primary_work_id"] = None
        cases.append(("typed state", bad_snapshot))
        bad_capabilities = copy.deepcopy(self.read_result)
        bad_capabilities["capabilities"] = {}
        cases.append(("capabilities", bad_capabilities))

        for label, read_result in cases:
            with self.subTest(label=label):
                before = sorted(
                    path.relative_to(self.store.root)
                    for path in self.store.root.rglob("*")
                    if path.is_file()
                )
                with self.assertRaises(CheckpointInputError):
                    publish_checkpoint(
                        read_result,
                        self.store,
                        canonical_plan_sha256=self.plan_digest,
                    )
                after = sorted(
                    path.relative_to(self.store.root)
                    for path in self.store.root.rglob("*")
                    if path.is_file()
                )
                self.assertEqual(after, before)

        with self.assertRaises(CheckpointInputError):
            publish_checkpoint(
                self.read_result,
                self.store,
                canonical_plan_sha256="not-a-digest",
            )

    def test_missing_or_tampered_manifest_and_snapshot_are_rejected(self):
        checkpoint_ref = self._publish()
        manifest_payload = self.store.read(checkpoint_ref)
        manifest = json.loads(manifest_payload.decode("utf-8"))
        snapshot_ref = ArtifactRef.from_document(manifest["snapshot_ref"])
        snapshot_payload = self.store.read(snapshot_ref)

        self.store.object_path(checkpoint_ref).unlink()
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(checkpoint_ref)
        self.assertEqual(self.store.put_bytes(manifest_payload), checkpoint_ref)

        self.store.object_path(checkpoint_ref).write_bytes(b"{" + b"x" * (checkpoint_ref.size_bytes - 1))
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(checkpoint_ref)
        self.store.object_path(checkpoint_ref).unlink()
        self.assertEqual(self.store.put_bytes(manifest_payload), checkpoint_ref)

        self.store.object_path(snapshot_ref).unlink()
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(checkpoint_ref)
        self.assertEqual(self.store.put_bytes(snapshot_payload), snapshot_ref)

        self.store.object_path(snapshot_ref).write_bytes(b"[" + b"x" * (snapshot_ref.size_bytes - 1))
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(checkpoint_ref)

    def test_unknown_version_fields_malformed_state_and_projection_drift_fail_closed(self):
        checkpoint_ref = self._publish()
        cases = [
            ("schema_version", lambda document: document.__setitem__("schema_version", "v0")),
            ("unknown field", lambda document: document.__setitem__("unknown", True)),
            ("projection drift", lambda document: document.__setitem__("primary_work_id", None)),
        ]
        for label, transform in cases:
            with self.subTest(label=label):
                candidate = self._replace_manifest(checkpoint_ref, transform)
                with self.assertRaises(CheckpointIntegrityError):
                    self._restore(candidate)

        malformed_manifest = self.store.put_bytes(b"not-json")
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(malformed_manifest)

        manifest = json.loads(self.store.read(checkpoint_ref).decode("utf-8"))
        malformed_snapshot_ref = self.store.put_bytes(b"not-json")
        manifest["snapshot_ref"] = malformed_snapshot_ref.to_document()
        candidate = self.store.put_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        with self.assertRaises(CheckpointIntegrityError):
            self._restore(candidate)

    def test_restore_rejects_each_stale_expected_authority_value(self):
        checkpoint_ref = self._publish()
        cases = {
            "project": {"expected_project_id": "other-project"},
            "governance": {"expected_governance_ref": "artifact://governance/forged"},
            "revision": {"expected_revision": 8},
            "event head": {
                "expected_event_head": {"sequence_no": 8, "event_sha256": "e" * 64}
            },
            "missing event head": {"expected_event_head": None},
            "plan": {"expected_plan_sha256": "e" * 64},
            "registry": {"expected_registry_digest": "f" * 64},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(CheckpointStaleError):
                    self._restore(checkpoint_ref, **overrides)

        no_head_result = copy.deepcopy(self.read_result)
        no_head_result["event_head"] = None
        no_head_ref = publish_checkpoint(
            no_head_result,
            self.store,
            canonical_plan_sha256=self.plan_digest,
        )
        with self.assertRaises(CheckpointStaleError):
            self._restore(no_head_ref)

    def test_manifest_and_snapshot_size_limits_run_before_unbounded_reads(self):
        checkpoint_ref = self._publish()
        counting_store = _ReadCountingStore(self.store)
        with self.assertRaises(CheckpointSizeError):
            restore_checkpoint(
                checkpoint_ref,
                counting_store,
                expected_project_id=self.snapshot["project"]["project_id"],
                expected_revision=self.snapshot["project"]["revision"],
                expected_event_head=self.event_head,
                expected_governance_ref=self.governance_ref,
                expected_plan_sha256=self.plan_digest,
                expected_registry_digest=self.registry_digest,
                max_manifest_bytes=1,
            )
        self.assertEqual(counting_store.read_refs, [])

        counting_store = _ReadCountingStore(self.store)
        with self.assertRaises(CheckpointSizeError):
            restore_checkpoint(
                checkpoint_ref,
                counting_store,
                expected_project_id=self.snapshot["project"]["project_id"],
                expected_revision=self.snapshot["project"]["revision"],
                expected_event_head=self.event_head,
                expected_governance_ref=self.governance_ref,
                expected_plan_sha256=self.plan_digest,
                expected_registry_digest=self.registry_digest,
                max_snapshot_bytes=1,
            )
        self.assertEqual(counting_store.read_refs, [checkpoint_ref])

    def test_sqlite_state_mcp_to_local_checkpoint_restore_requires_no_service(self):
        database_path = Path(self.temporary_directory.name) / "state.sqlite"
        state_store = SQLiteStateStore(database_path)
        state_store.initialize()
        state_store.create_project(copy.deepcopy(self.base_snapshot))
        service = StateMCPService(
            state_store,
            authorizer=_AllowAuthorizer(),
            registry_digest=self.registry_digest,
            clock=lambda: "2026-08-10T05:45:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        response = service.call_tool(
            "context.state.read",
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-checkpoint-read",
                "project_id": self.base_snapshot["project"]["project_id"],
            },
            context=RequestContext("actor-owner", "authorization-local"),
        )
        self.assertTrue(response["ok"])

        checkpoint_ref = publish_checkpoint(
            response["result"],
            self.store,
            canonical_plan_sha256=self.plan_digest,
        )
        restored = restore_checkpoint(
            checkpoint_ref,
            self.store,
            expected_project_id=self.base_snapshot["project"]["project_id"],
            expected_revision=self.base_snapshot["project"]["revision"],
            expected_event_head=None,
            expected_governance_ref=self.base_snapshot["project"]["governance_ref"],
            expected_plan_sha256=self.plan_digest,
            expected_registry_digest=self.registry_digest,
        )

        self.assertEqual(restored.snapshot, self.base_snapshot)
        self.assertEqual(restored.manifest["event_head"], None)
        self.assertTrue(database_path.is_file())
        self.assertEqual(os.environ.get("CONTEXT_CONTROL_PLANE_POSTGRES_DSN"), None)


if __name__ == "__main__":
    unittest.main()
