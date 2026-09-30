"""M8-01 deterministic typed-state v4/v5 migration boundary."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_review import migrate_typed_state_v3_to_v4


def _v4_snapshot_with_effects() -> dict:
    snapshot = migrate_typed_state_v3_to_v4(
        build_idea_snapshot(), migrated_at="2026-08-14T09:00:00+08:00"
    )
    snapshot["effects"] = [
        {
            "effect_id": "effect-m8-01-migration",
            "effect_key": "effect-key-m8-01-migration",
            "work_id": "work-active",
            "claim_id": "claim-active",
            "status": "authorized",
            "operation": "write-artifact",
            "scope_ref": {
                "scope_kind": "capability",
                "scope_ref": "idea/active",
            },
            "expected_project_revision": snapshot["project"]["revision"],
            "sequence_no": 1,
            "evidence_ids": [],
            "result_ref": None,
            "requested_at": "2026-08-16T12:00:00+08:00",
            "completed_at": None,
            "attempt_id": None,
        }
    ]
    return snapshot


class M801TypedStateV5MigrationTests(unittest.TestCase):
    @staticmethod
    def _receipt(source: dict, target: dict, *, migration_id: str) -> dict:
        from context_control_plane.durable_state_migration import (
            build_durable_state_migration_receipt,
        )

        return build_durable_state_migration_receipt(
            source=source,
            target=target,
            migration_id=migration_id,
            source_event_head={"sequence_no": 0, "event_sha256": None},
            registry_digest="c" * 64,
            authorization_ref="authorization://m8-01/state-v5",
            migrated_at="2026-08-16T12:30:00+08:00",
        )

    def test_upgrade_adds_null_request_digest_deterministically_and_is_idempotent(self):
        from context_control_plane.durable_state_migration import (
            canonical_typed_state_migration_bytes,
            migrate_typed_state_v4_to_v5,
        )

        source = _v4_snapshot_with_effects()
        original = copy.deepcopy(source)
        first = migrate_typed_state_v4_to_v5(source)
        second = migrate_typed_state_v4_to_v5(copy.deepcopy(source))
        replay = migrate_typed_state_v4_to_v5(first)

        self.assertEqual(source, original)
        self.assertEqual(first["schema_version"], "context.typed-state/v5alpha1")
        self.assertTrue(all(effect["request_sha256"] is None for effect in first["effects"]))
        self.assertEqual(first, second)
        self.assertEqual(replay, first)
        self.assertEqual(
            canonical_typed_state_migration_bytes(first),
            canonical_typed_state_migration_bytes(second),
        )

    def test_null_only_v5_rolls_back_losslessly_and_v4_rollback_is_idempotent(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
            rollback_typed_state_v5_to_v4,
        )

        source = _v4_snapshot_with_effects()
        migrated = migrate_typed_state_v4_to_v5(source)

        self.assertEqual(rollback_typed_state_v5_to_v4(migrated), source)
        self.assertEqual(rollback_typed_state_v5_to_v4(source), source)

    def test_rollback_fails_closed_after_any_effect_digest_is_populated(self):
        from context_control_plane.durable_state_migration import (
            DurableStateMigrationError,
            migrate_typed_state_v4_to_v5,
            rollback_typed_state_v5_to_v4,
        )

        migrated = migrate_typed_state_v4_to_v5(_v4_snapshot_with_effects())
        migrated["effects"][0]["request_sha256"] = "a" * 64

        with self.assertRaisesRegex(DurableStateMigrationError, "rollback"):
            rollback_typed_state_v5_to_v4(migrated)

    def test_v5_preserves_the_v4_idea_capture_gate(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
        )
        from context_control_plane.idea_continuity import (
            evaluate_idea_capture_gate,
        )

        snapshot = migrate_typed_state_v4_to_v5(_v4_snapshot_with_effects())
        project = snapshot["project"]
        verdict = evaluate_idea_capture_gate(
            snapshot,
            actor_ref="actor-owner",
            expected_revision=project["revision"],
            parent_work_id=project["primary_work_id"],
            return_work_id=project["primary_work_id"],
            action="capture-and-continue",
            switch_target_work_id=None,
            expiry=None,
            observed_at=project["updated_at"],
        )

        self.assertEqual(verdict["decision"], "allow")

    def test_receipt_binds_both_snapshots_state_cursor_registry_and_authorization(self):
        from context_control_plane.durable_state_migration import (
            build_durable_state_migration_receipt,
            migrate_typed_state_v4_to_v5,
            validate_durable_state_migration_receipt,
        )

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        event_head = {"sequence_no": 27, "event_sha256": "b" * 64}
        receipt = build_durable_state_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-m8-01-v4-v5",
            source_event_head=event_head,
            registry_digest="c" * 64,
            authorization_ref="authorization://m8-01/state-v5",
            migrated_at="2026-08-16T12:30:00+08:00",
        )

        validate_durable_state_migration_receipt(
            receipt,
            source=source,
            target=target,
            expected_source_event_head=event_head,
            expected_registry_digest="c" * 64,
            expected_authorization_ref="authorization://m8-01/state-v5",
        )
        self.assertEqual(receipt["source_revision"], source["project"]["revision"])
        self.assertEqual(receipt["source_event_head"], event_head)
        self.assertNotEqual(
            receipt["source_snapshot_sha256"], receipt["target_snapshot_sha256"]
        )
        self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_receipt_rejects_snapshot_cursor_registry_and_authorization_drift(self):
        from context_control_plane.durable_state_migration import (
            DurableStateMigrationError,
            build_durable_state_migration_receipt,
            migrate_typed_state_v4_to_v5,
            validate_durable_state_migration_receipt,
        )

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        event_head = {"sequence_no": 27, "event_sha256": "b" * 64}
        receipt = build_durable_state_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-m8-01-v4-v5",
            source_event_head=event_head,
            registry_digest="c" * 64,
            authorization_ref="authorization://m8-01/state-v5",
            migrated_at="2026-08-16T12:30:00+08:00",
        )

        cases = (
            {
                "source": {**source, "project": {**source["project"], "revision": 10}},
            },
            {
                "target": {
                    **target,
                    "effects": [
                        {**target["effects"][0], "request_sha256": "d" * 64}
                    ],
                },
            },
            {"expected_source_event_head": {"sequence_no": 28, "event_sha256": "e" * 64}},
            {"expected_registry_digest": "f" * 64},
            {"expected_authorization_ref": "authorization://m8-01/other"},
        )
        defaults = {
            "source": source,
            "target": target,
            "expected_source_event_head": event_head,
            "expected_registry_digest": "c" * 64,
            "expected_authorization_ref": "authorization://m8-01/state-v5",
        }
        for changed in cases:
            with self.subTest(changed=tuple(changed)), self.assertRaises(
                DurableStateMigrationError
            ):
                validate_durable_state_migration_receipt(
                    receipt, **{**defaults, **changed}
                )

        forged = copy.deepcopy(receipt)
        forged["receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(DurableStateMigrationError, "digest"):
            validate_durable_state_migration_receipt(receipt=forged, **defaults)

    def test_sqlite_persists_v5_boundary_and_receipt_idempotently(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        receipt = self._receipt(source, target, migration_id="migration-m8-01-sqlite")
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            arguments = {
                "project_id": source["project"]["project_id"],
                "expected_revision": source["project"]["revision"],
                "expected_event_head_sha256": None,
                "expected_registry_digest": "c" * 64,
                "expected_authorization_ref": "authorization://m8-01/state-v5",
                "target_snapshot": target,
                "migration_receipt": receipt,
            }

            self.assertEqual(store.migrate_project(**arguments), receipt)
            self.assertEqual(store.migrate_project(**arguments), receipt)
            self.assertEqual(store.read_project(source["project"]["project_id"]), target)
            self.assertEqual(
                store.read_migration_receipt(
                    source["project"]["project_id"], receipt["migration_id"]
                ),
                receipt,
            )

    def test_sqlite_v5_migration_faults_roll_back_snapshot_and_receipt(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        for stage in (
            "after_migration_receipt_insert",
            "after_migration_snapshot_update",
        ):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                receipt = self._receipt(
                    source, target, migration_id=f"migration-m8-01-fault-{stage}"
                )
                store = SQLiteStateStore(
                    Path(directory) / "state.sqlite3",
                    fault_hook=lambda current, expected=stage: (
                        (_ for _ in ()).throw(RuntimeError("migration fault"))
                        if current == expected
                        else None
                    ),
                )
                store.initialize()
                store.create_project(source)
                with self.assertRaisesRegex(RuntimeError, "migration fault"):
                    store.migrate_project(
                        project_id=source["project"]["project_id"],
                        expected_revision=source["project"]["revision"],
                        expected_event_head_sha256=None,
                        expected_registry_digest="c" * 64,
                        expected_authorization_ref="authorization://m8-01/state-v5",
                        target_snapshot=target,
                        migration_receipt=receipt,
                    )
                self.assertEqual(
                    store.read_project(source["project"]["project_id"]), source
                )
                self.assertIsNone(
                    store.read_migration_receipt(
                        source["project"]["project_id"], receipt["migration_id"]
                    )
                )

    def test_sqlite_v5_migration_checks_revision_event_head_registry_and_authorization(self):
        from context_control_plane.durable_state_migration import (
            build_durable_state_migration_receipt,
            migrate_typed_state_v4_to_v5,
        )
        from context_control_plane.sqlite_state_store import (
            SQLiteStateConflict,
            SQLiteStateIntegrityError,
            SQLiteStateStore,
        )

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        receipt = self._receipt(source, target, migration_id="migration-m8-01-cas")
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            common = {
                "project_id": source["project"]["project_id"],
                "expected_revision": source["project"]["revision"],
                "expected_event_head_sha256": None,
                "expected_registry_digest": "c" * 64,
                "expected_authorization_ref": "authorization://m8-01/state-v5",
                "target_snapshot": target,
                "migration_receipt": receipt,
            }
            with self.assertRaises(SQLiteStateConflict):
                store.migrate_project(**{**common, "expected_revision": 10})
            with self.assertRaises(SQLiteStateConflict):
                store.migrate_project(
                    **{**common, "expected_event_head_sha256": "d" * 64}
                )
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    **{**common, "expected_registry_digest": "e" * 64}
                )
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    **{
                        **common,
                        "expected_authorization_ref": "authorization://m8-01/other",
                    }
                )

            wrong_head_receipt = build_durable_state_migration_receipt(
                source=source,
                target=target,
                migration_id="migration-m8-01-wrong-head",
                source_event_head={"sequence_no": 1, "event_sha256": "f" * 64},
                registry_digest="c" * 64,
                authorization_ref="authorization://m8-01/state-v5",
                migrated_at="2026-08-16T12:30:00+08:00",
            )
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    **{**common, "migration_receipt": wrong_head_receipt}
                )

    def test_sqlite_rejects_rollback_after_effect_digest_is_populated(self):
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
            rollback_typed_state_v5_to_v4,
        )
        from context_control_plane.sqlite_state_store import (
            SQLiteStateIntegrityError,
            SQLiteStateStore,
        )

        legacy = _v4_snapshot_with_effects()
        null_v5 = migrate_typed_state_v4_to_v5(legacy)
        receipt = self._receipt(null_v5, legacy, migration_id="migration-m8-01-rollback")
        populated_v5 = copy.deepcopy(null_v5)
        populated_v5["effects"][0]["request_sha256"] = "a" * 64
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(populated_v5)
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    project_id=populated_v5["project"]["project_id"],
                    expected_revision=populated_v5["project"]["revision"],
                    expected_event_head_sha256=None,
                    expected_registry_digest="c" * 64,
                    expected_authorization_ref="authorization://m8-01/state-v5",
                    target_snapshot=legacy,
                    migration_receipt=receipt,
                )
            self.assertEqual(
                store.read_project(populated_v5["project"]["project_id"]), populated_v5
            )
            with self.assertRaisesRegex(Exception, "rollback"):
                rollback_typed_state_v5_to_v4(populated_v5)

    def test_sqlite_rejects_rollback_after_upgrade_event_head_advances(self):
        from context_control_plane.durable_state_migration import (
            build_durable_state_migration_receipt,
            migrate_typed_state_v4_to_v5,
            rollback_typed_state_v5_to_v4,
        )
        from context_control_plane.sqlite_state_store import (
            SQLiteStateIntegrityError,
            SQLiteStateStore,
        )
        from context_control_plane.state_events import build_state_event

        source = _v4_snapshot_with_effects()
        target = migrate_typed_state_v4_to_v5(source)
        upgrade_receipt = self._receipt(
            source, target, migration_id="migration-m8-01-upgrade-before-rollback"
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            store.migrate_project(
                project_id=source["project"]["project_id"],
                expected_revision=source["project"]["revision"],
                expected_event_head_sha256=None,
                expected_registry_digest="c" * 64,
                expected_authorization_ref="authorization://m8-01/state-v5",
                target_snapshot=target,
                migration_receipt=upgrade_receipt,
            )

            changed = copy.deepcopy(target)
            changed["project"]["revision"] += 1
            changed["project"]["updated_at"] = "2026-08-14T08:00:00+08:00"
            for claim in changed["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = changed["project"]["revision"]
            for effect in changed["effects"]:
                if effect["status"] in {"authorized", "executing"}:
                    effect["expected_project_revision"] = changed["project"]["revision"]
            changed["works"][0]["title"] = "advanced after v5 upgrade"
            changed["works"][0]["revision"] += 1
            event = build_state_event(
                event_id="event-m8-01-after-upgrade",
                event_type="state-transition",
                project_id=source["project"]["project_id"],
                sequence_no=1,
                revision_before=target["project"]["revision"],
                occurred_at="2026-08-14T08:00:00+08:00",
                actor_ref="actor-owner",
                causation_ref="work:m8-01",
                correlation_ref="migration:m8-01",
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "works",
                        "object_id": changed["works"][0]["work_id"],
                        "value": changed["works"][0],
                    },
                    {
                        "collection": "claims",
                        "object_id": changed["claims"][0]["claim_id"],
                        "value": changed["claims"][0],
                    },
                    {
                        "collection": "effects",
                        "object_id": changed["effects"][0]["effect_id"],
                        "value": changed["effects"][0],
                    },
                ],
                project_after=changed["project"],
                schema_version="context.state-event/v4alpha1",
            )
            store.commit_event(
                project_id=source["project"]["project_id"],
                expected_revision=target["project"]["revision"],
                event=event,
                expected_snapshot=changed,
            )
            rollback_target = rollback_typed_state_v5_to_v4(changed)
            rollback_receipt = build_durable_state_migration_receipt(
                source=changed,
                target=rollback_target,
                migration_id="migration-m8-01-rollback-after-event",
                source_event_head={
                    "sequence_no": 1,
                    "event_sha256": event["event_sha256"],
                },
                registry_digest="c" * 64,
                authorization_ref="authorization://m8-01/state-v5",
                migrated_at="2026-08-14T08:01:00+08:00",
            )

            with self.assertRaisesRegex(
                SQLiteStateIntegrityError, "advanced beyond the upgrade boundary"
            ):
                store.migrate_project(
                    project_id=source["project"]["project_id"],
                    expected_revision=changed["project"]["revision"],
                    expected_event_head_sha256=event["event_sha256"],
                    expected_registry_digest="c" * 64,
                    expected_authorization_ref="authorization://m8-01/state-v5",
                    target_snapshot=rollback_target,
                    migration_receipt=rollback_receipt,
                )


if __name__ == "__main__":
    unittest.main()
