"""M5-06 Idea-aware context return packet behavior."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_return_packet import (
    IdeaReturnPacketError,
    build_return_context_migration_receipt,
    canonical_idea_return_packet_bytes,
    compose_idea_return_packet,
    validate_idea_return_packet,
)
from context_control_plane.idea_review import (
    migrate_typed_state_v3_to_v4,
    upsert_idea_observation,
)
from context_control_plane.typed_state import validate_typed_state


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _checkpoint_ref(digest="d" * 64):
    return {
        "schema_version": "context.artifact-ref/v1alpha1",
        "digest_algorithm": "sha-256",
        "digest": digest,
        "size_bytes": 2048,
        "artifact_uri": f"artifact://sha256/{digest}",
    }


def _canary_receipt(*, checkpoint_digest="d" * 64, active_work_id="work-active"):
    receipt = {
        "schema_version": "context.postcompact-canary/v1alpha1",
        "canary_version": "context.postcompact-canary-evaluator/v1alpha1",
        "canary_id": "pending",
        "provider_id": "pi",
        "project_id": "project-idea-benchmark",
        "project_revision": 9,
        "expected_packet_sha256": "a" * 64,
        "restored_packet_sha256": "a" * 64,
        "delta_id": "delta-m5-06-fixture",
        "delta_sha256": "b" * 64,
        "event_head": None,
        "active_work_id": active_work_id,
        "task_revision": 1,
        "effect_high_watermark": 0,
        "decision_ids": [],
        "constraint_ids": [],
        "host_metadata_sha256": "c" * 64,
        "authority_binding_sha256": "e" * 64,
        "checkpoint_sha256": checkpoint_digest,
        "expected_packet_artifact_sha256": "f" * 64,
        "status": "pass",
        "execution_gate": "allow",
        "state_write_authority": False,
        "provider_native_authority": False,
        "observed_at": "2026-08-15T03:00:00+08:00",
        "canary_sha256": "pending",
    }
    # M5-03 derives the ID from a body where canary_sha256 is absent.
    body = copy.deepcopy(receipt)
    body.pop("canary_sha256")
    first = hashlib.sha256(_canonical(body)).hexdigest()
    receipt["canary_id"] = f"canary-{first[:32]}"
    body = copy.deepcopy(receipt)
    body.pop("canary_sha256")
    receipt["canary_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return receipt


class M506IdeaReturnPacketTests(unittest.TestCase):
    def setUp(self):
        snapshot = migrate_typed_state_v3_to_v4(
            build_idea_snapshot(), migrated_at="2026-08-15T03:05:00+08:00"
        )
        snapshot = upsert_idea_observation(
            snapshot,
            idea_id="idea-return",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_abcdefghijklmnopqrstuvwxyz",
            summary="This complete Idea body must never enter the return packet.",
            scope_ref="work://work-active",
            urgency="next",
            review_at=None,
            occurrence_id="occurrence-return",
            observed_at="2026-08-15T03:06:00+08:00",
        )
        snapshot = upsert_idea_observation(
            snapshot,
            idea_id="idea-unrelated",
            parent_work_id="work-target",
            return_work_id="work-target",
            source_ref="rng_bcdefghijklmnopqrstuvwxyza",
            summary="Unrelated Idea body.",
            scope_ref="work://work-target",
            urgency="later",
            review_at=None,
            occurrence_id="occurrence-unrelated",
            observed_at="2026-08-15T03:07:00+08:00",
        )
        snapshot["project"]["revision"] = 11
        snapshot["project"]["updated_at"] = "2026-08-15T03:10:00+08:00"
        snapshot["claims"][0]["expected_project_revision"] = 11
        snapshot["claims"][0]["lease_expires_at"] = "2026-08-15T04:00:00+08:00"
        next(
            item for item in snapshot["works"] if item["work_id"] == "work-active"
        )["revision"] = 2
        validate_typed_state(snapshot)
        checkpoint_ref = _checkpoint_ref()
        self.snapshot = snapshot
        self.return_frame = {
            "checkpoint_ref": checkpoint_ref,
            "old_work_id": "work-active",
            "old_work_revision": 1,
            "proposal_sha256": "9" * 64,
            "old_project_revision": 9,
            "return_work_id": "work-active",
            "return_work_revision": 1,
            "current_work_id": "work-target",
        }
        self.binding = {
            "schema_version": "context.idea-return-checkpoint-binding/v1alpha1",
            "project_id": "project-idea-benchmark",
            "project_revision": 9,
            "return_work_id": "work-active",
            "return_work_revision": 1,
            "canonical_plan_sha256": "1" * 64,
            "registry_digest": "2" * 64,
            "checkpoint_ref": checkpoint_ref,
        }
        self.canary = _canary_receipt()

    def compose(self, **updates):
        arguments = {
            "snapshot": self.snapshot,
            "return_frame": self.return_frame,
            "checkpoint_binding": self.binding,
            "postcompact_canary": self.canary,
            "canonical_plan_sha256": "1" * 64,
            "registry_digest": "2" * 64,
            "next_action": "resume the original Work from its current authority revision",
            "observed_at": "2026-08-15T03:11:00+08:00",
            "trusted_migration_receipt": None,
            "migration_receipt_verifier": None,
        }
        arguments.update(updates)
        return compose_idea_return_packet(**arguments)

    def test_return_packet_uses_current_authority_and_contains_only_idea_refs(self):
        packet = self.compose()
        validate_idea_return_packet(packet)
        self.assertEqual(packet["project_revision"], 11)
        self.assertEqual(packet["active_leaf"]["work_id"], "work-active")
        self.assertEqual(packet["active_leaf"]["revision"], 2)
        self.assertEqual(
            packet["return_point"],
            {
                "work_id": "work-active",
                "historical_work_revision": 1,
                "current_work_revision": 2,
            },
        )
        self.assertEqual(packet["historical_checkpoint"]["authority_role"], "recovery-evidence-only")
        self.assertEqual(
            packet["idea_refs"],
            [{
                "idea_id": "idea-return",
                "status": "candidate",
                "return_work_id": "work-active",
                "authority": "candidate-only",
            }],
        )
        payload = canonical_idea_return_packet_bytes(packet)
        self.assertNotIn(b"This complete Idea body", payload)
        self.assertNotIn(b"rng_abcdefghijklmnopqrstuvwxyz", payload)
        self.assertFalse(packet["candidate_execution_authority"])
        self.assertFalse(packet["candidate_state_write_authority"])
        self.assertFalse(packet["state_write_authority"])
        self.assertFalse(packet["external_effect_authority"])
        self.assertEqual(payload, canonical_idea_return_packet_bytes(self.compose()))

    def test_v5_snapshot_preserves_the_v4_return_packet_contract(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
        )

        packet = self.compose(
            snapshot=migrate_typed_state_v4_to_v5(self.snapshot)
        )

        validate_idea_return_packet(packet)
        self.assertEqual(packet["project_revision"], 11)
        self.assertEqual(packet["active_leaf"]["work_id"], "work-active")

    def test_historical_checkpoint_cannot_replace_current_active_authority(self):
        changed = copy.deepcopy(self.snapshot)
        active = next(item for item in changed["works"] if item["work_id"] == "work-active")
        target = next(item for item in changed["works"] if item["work_id"] == "work-target")
        active["status"] = "ready"
        target["status"] = "active"
        changed["project"]["active_work_ids"] = ["work-target"]
        changed["project"]["primary_work_id"] = "work-target"
        changed["claims"][0]["work_id"] = "work-target"
        changed["claims"][0]["scope_owners"] = copy.deepcopy(target["scope_refs"])
        validate_typed_state(changed)
        with self.assertRaisesRegex(IdeaReturnPacketError, "current authority"):
            self.compose(snapshot=changed)

    def test_self_consistent_canary_for_another_work_is_rejected(self):
        forged = _canary_receipt(active_work_id="work-target")
        with self.assertRaisesRegex(IdeaReturnPacketError, "canary.*return"):
            self.compose(postcompact_canary=forged)

    def test_plan_or_registry_drift_requires_an_explicit_trusted_migration(self):
        for field, current_value in (
            ("canonical_plan_sha256", "3" * 64),
            ("registry_digest", "4" * 64),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(IdeaReturnPacketError, "migration"):
                    self.compose(**{field: current_value})

                migration = build_return_context_migration_receipt(
                    migration_id=f"migration-{field}",
                    project_id="project-idea-benchmark",
                    return_work_id="work-active",
                    from_project_revision=9,
                    to_project_revision=11,
                    from_canonical_plan_sha256="1" * 64,
                    to_canonical_plan_sha256=(
                        current_value if field == "canonical_plan_sha256" else "1" * 64
                    ),
                    from_registry_digest="2" * 64,
                    to_registry_digest=(current_value if field == "registry_digest" else "2" * 64),
                    authority_event_ref="event://state-mcp/m5-06-migration",
                    authorization_ref="authorization://m5-06-migration",
                )
                packet = self.compose(
                    **{field: current_value},
                    trusted_migration_receipt=migration,
                    migration_receipt_verifier=lambda receipt: (
                        receipt["authority_event_ref"]
                        == "event://state-mcp/m5-06-migration"
                    ),
                )
                self.assertEqual(packet["governance_migration_ref"]["migration_id"], f"migration-{field}")

    def test_migration_receipt_must_bind_the_current_authority_revision(self):
        migration = build_return_context_migration_receipt(
            migration_id="migration-stale",
            project_id="project-idea-benchmark",
            return_work_id="work-active",
            from_project_revision=9,
            to_project_revision=10,
            from_canonical_plan_sha256="1" * 64,
            to_canonical_plan_sha256="3" * 64,
            from_registry_digest="2" * 64,
            to_registry_digest="2" * 64,
            authority_event_ref="event://state-mcp/stale-migration",
            authorization_ref="authorization://stale-migration",
        )
        with self.assertRaisesRegex(IdeaReturnPacketError, "migration"):
            self.compose(canonical_plan_sha256="3" * 64, trusted_migration_receipt=migration)

    def test_self_signed_migration_receipt_cannot_bypass_authority_verification(self):
        migration = build_return_context_migration_receipt(
            migration_id="migration-unverified",
            project_id="project-idea-benchmark",
            return_work_id="work-active",
            from_project_revision=9,
            to_project_revision=11,
            from_canonical_plan_sha256="1" * 64,
            to_canonical_plan_sha256="3" * 64,
            from_registry_digest="2" * 64,
            to_registry_digest="2" * 64,
            authority_event_ref="event://untrusted/self-issued",
            authorization_ref="authorization://untrusted/self-issued",
        )
        for verifier in (None, lambda receipt: False):
            with self.subTest(verifier=verifier), self.assertRaisesRegex(
                IdeaReturnPacketError, "authority verification"
            ):
                self.compose(
                    canonical_plan_sha256="3" * 64,
                    trusted_migration_receipt=migration,
                    migration_receipt_verifier=verifier,
                )

    def test_packet_validator_rejects_idea_body_injection_and_authority_escalation(self):
        packet = self.compose()
        injected = copy.deepcopy(packet)
        injected["idea_refs"][0]["summary"] = "forbidden body"
        with self.assertRaises(IdeaReturnPacketError):
            validate_idea_return_packet(injected)
        escalated = copy.deepcopy(packet)
        escalated["candidate_execution_authority"] = True
        with self.assertRaisesRegex(IdeaReturnPacketError, "authority"):
            validate_idea_return_packet(escalated)


if __name__ == "__main__":
    unittest.main()
