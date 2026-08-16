"""M8-02 typed-state v5/v6 versioned migration boundary."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from context_control_plane.durable_state_migration import migrate_typed_state_v4_to_v5
from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_review import migrate_typed_state_v3_to_v4


def _v5_snapshot() -> dict:
    snapshot = migrate_typed_state_v4_to_v5(
        migrate_typed_state_v3_to_v4(
            build_idea_snapshot(), migrated_at="2026-08-14T09:00:00+08:00"
        )
    )
    snapshot["effects"] = [
        {
            "effect_id": "effect-m8-02-migration",
            "effect_key": "effect-key-m8-02-migration",
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
            "request_sha256": "a" * 64,
        }
    ]
    return snapshot


class M802TypedStateV6MigrationTests(unittest.TestCase):
    @staticmethod
    def _receipt(source: dict, target: dict, *, migration_id: str) -> dict:
        from context_control_plane.shared_state_migration import (
            build_typed_state_v5_to_v6_migration_receipt,
        )

        return build_typed_state_v5_to_v6_migration_receipt(
            source=source,
            target=target,
            migration_id=migration_id,
            source_event_head={"sequence_no": 0, "event_sha256": None},
            registry_digest="c" * 64,
            authorization_ref="authorization://m8-02/state-v6",
            migrated_at="2026-08-16T17:00:00+08:00",
        )

    def test_upgrade_is_lossless_idempotent_and_uses_explicit_defaults(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )

        source = _v5_snapshot()
        original = copy.deepcopy(source)
        first = migrate_typed_state_v5_to_v6(source)
        second = migrate_typed_state_v5_to_v6(copy.deepcopy(source))
        replay = migrate_typed_state_v5_to_v6(first)

        self.assertEqual(source, original)
        self.assertEqual(first["schema_version"], "context.typed-state/v6alpha1")
        self.assertEqual(first, second)
        self.assertEqual(first, replay)
        self.assertEqual(first["claims"][0]["claim_revision"], 1)
        self.assertEqual(first["claims"][0]["lease_epoch"], 1)
        self.assertEqual(
            first["claims"][0]["last_heartbeat_at"],
            first["claims"][0]["claimed_at"],
        )
        self.assertIsNone(first["claims"][0]["closed_at"])
        self.assertIsNone(first["claims"][0]["closed_by_ref"])
        self.assertIsNone(first["claims"][0]["close_reason"])
        self.assertIsNone(first["claims"][0]["reclaimed_from_claim_id"])
        self.assertEqual(
            first["effects"][0]["lease_epoch"],
            first["claims"][0]["lease_epoch"],
        )
        self.assertIsNone(first["effects"][0]["dispatch_receipt_sha256"])
        self.assertIsNone(first["effects"][0]["dispatch_started_at"])
        work = next(item for item in first["works"] if item["work_id"] == "work-active")
        self.assertIsNone(work["work_source_ref"])
        self.assertEqual(work["source_revision"], 0)
        self.assertIsNone(work["work_identity_sha256"])
        self.assertIsNone(work["dedupe_receipt_sha256"])

    def test_default_only_v6_rolls_back_losslessly(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
            rollback_typed_state_v6_to_v5,
        )

        source = _v5_snapshot()
        migrated = migrate_typed_state_v5_to_v6(source)
        self.assertEqual(rollback_typed_state_v6_to_v5(migrated), source)
        self.assertEqual(rollback_typed_state_v6_to_v5(source), source)

    def test_rollback_fails_closed_for_every_populated_v6_field(self):
        from context_control_plane.shared_state_migration import (
            DurableStateV6MigrationError,
            migrate_typed_state_v5_to_v6,
            rollback_typed_state_v6_to_v5,
        )

        migrated = migrate_typed_state_v5_to_v6(_v5_snapshot())
        mutations = (
            ("claims", "claim_revision", 2),
            ("claims", "lease_epoch", 2),
            ("claims", "last_heartbeat_at", "2026-08-16T12:01:00+08:00"),
            ("claims", "closed_at", "2026-08-16T12:01:00+08:00"),
            ("claims", "closed_by_ref", "actor-reclaimer"),
            ("claims", "close_reason", "lease_expired"),
            ("claims", "reclaimed_from_claim_id", "claim-old"),
            ("effects", "lease_epoch", 2),
            ("effects", "dispatch_receipt_sha256", "b" * 64),
            ("effects", "dispatch_started_at", "2026-08-16T12:01:00+08:00"),
            ("works", "work_source_ref", "forge://issue/1"),
            ("works", "source_revision", 1),
            ("works", "work_identity_sha256", "c" * 64),
            ("works", "dedupe_receipt_sha256", "d" * 64),
        )
        for collection, field, value in mutations:
            with self.subTest(collection=collection, field=field):
                changed = copy.deepcopy(migrated)
                changed[collection][0][field] = value
                with self.assertRaisesRegex(DurableStateV6MigrationError, "rollback"):
                    rollback_typed_state_v6_to_v5(changed)

    def test_migration_receipt_binds_snapshots_cursor_registry_and_authorization(self):
        from context_control_plane.shared_state_migration import (
            build_typed_state_v5_to_v6_migration_receipt,
            migrate_typed_state_v5_to_v6,
            validate_typed_state_v5_to_v6_migration_receipt,
        )

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        event_head = {"sequence_no": 40, "event_sha256": "e" * 64}
        receipt = build_typed_state_v5_to_v6_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-m8-02-v5-v6",
            source_event_head=event_head,
            registry_digest="f" * 64,
            authorization_ref="authorization://m8-02/state-v6",
            migrated_at="2026-08-16T12:30:00+08:00",
        )
        validate_typed_state_v5_to_v6_migration_receipt(
            receipt,
            source=source,
            target=target,
            expected_source_event_head=event_head,
            expected_registry_digest="f" * 64,
            expected_authorization_ref="authorization://m8-02/state-v6",
        )
        self.assertEqual(receipt["source_revision"], source["project"]["revision"])
        self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            receipt["source_snapshot_sha256"], receipt["target_snapshot_sha256"]
        )

    def test_receipt_digest_and_authority_drift_are_rejected(self):
        from context_control_plane.shared_state_migration import (
            DurableStateV6MigrationError,
            build_typed_state_v5_to_v6_migration_receipt,
            migrate_typed_state_v5_to_v6,
            validate_typed_state_v5_to_v6_migration_receipt,
        )

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        event_head = {"sequence_no": 40, "event_sha256": "e" * 64}
        receipt = build_typed_state_v5_to_v6_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-m8-02-v5-v6",
            source_event_head=event_head,
            registry_digest="f" * 64,
            authorization_ref="authorization://m8-02/state-v6",
            migrated_at="2026-08-16T12:30:00+08:00",
        )
        common = {
            "source": source,
            "target": target,
            "expected_source_event_head": event_head,
            "expected_registry_digest": "f" * 64,
            "expected_authorization_ref": "authorization://m8-02/state-v6",
        }
        forged = copy.deepcopy(receipt)
        forged["receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(DurableStateV6MigrationError, "digest"):
            validate_typed_state_v5_to_v6_migration_receipt(forged, **common)
        with self.assertRaises(DurableStateV6MigrationError):
            validate_typed_state_v5_to_v6_migration_receipt(
                receipt,
                **{**common, "expected_registry_digest": "1" * 64},
            )

    def test_v6_rejects_unknown_claim_close_reason(self):
        from context_control_plane.shared_state_migration import (
            DurableStateV6MigrationError,
            canonical_shared_state_migration_bytes,
            migrate_typed_state_v5_to_v6,
        )

        migrated = migrate_typed_state_v5_to_v6(_v5_snapshot())
        migrated["claims"][0]["close_reason"] = "free-form-reason"
        with self.assertRaisesRegex(DurableStateV6MigrationError, "close_reason"):
            canonical_shared_state_migration_bytes(migrated)

    def test_v6_is_accepted_by_canonical_validator_and_empty_event_replay(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )
        from context_control_plane.state_events import replay_state_events
        from context_control_plane.typed_state import validate_typed_state

        migrated = migrate_typed_state_v5_to_v6(_v5_snapshot())

        validate_typed_state(migrated)
        self.assertEqual(replay_state_events(migrated, []), migrated)

    def test_sqlite_can_commit_and_replay_a_v6_event_after_migration(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore
        from context_control_plane.state_events import build_state_event

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        receipt = self._receipt(
            source, target, migration_id="migration-m8-02-follow-on"
        )
        changed = copy.deepcopy(target)
        changed["project"]["revision"] += 1
        changed["project"]["updated_at"] = "2026-08-14T08:01:00+08:00"
        changed["works"][0]["title"] = "v6 follow-on transition"
        for claim in changed["claims"]:
            if claim["status"] == "active":
                claim["expected_project_revision"] = changed["project"]["revision"]
        for effect in changed["effects"]:
            if effect["status"] in {"authorized", "started"}:
                effect["expected_project_revision"] = changed["project"]["revision"]
        event = build_state_event(
            event_id="event-m8-02-follow-on",
            event_type="state-transition",
            project_id=source["project"]["project_id"],
            sequence_no=1,
            revision_before=target["project"]["revision"],
            occurred_at=changed["project"]["updated_at"],
            actor_ref="actor-m8-02",
            causation_ref="work:m8-02-follow-on",
            correlation_ref="migration:m8-02",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "works",
                    "object_id": changed["works"][0]["work_id"],
                    "value": changed["works"][0],
                },
                *[
                    {
                        "collection": "claims",
                        "object_id": claim["claim_id"],
                        "value": claim,
                    }
                    for claim in changed["claims"]
                    if claim["status"] == "active"
                ],
                {
                    "collection": "effects",
                    "object_id": changed["effects"][0]["effect_id"],
                    "value": changed["effects"][0],
                },
            ],
            project_after=changed["project"],
            schema_version="context.state-event/v4alpha1",
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
                expected_authorization_ref="authorization://m8-02/state-v6",
                target_snapshot=target,
                migration_receipt=receipt,
            )
            store.commit_event(
                project_id=source["project"]["project_id"],
                expected_revision=target["project"]["revision"],
                event=event,
                expected_snapshot=changed,
            )
            reopened = SQLiteStateStore(Path(directory) / "state.sqlite3")
            self.assertEqual(
                reopened.read_project(source["project"]["project_id"]), changed
            )
            self.assertEqual(
                len(reopened.read_events(source["project"]["project_id"])), 1
            )

    def test_sqlite_persists_v6_boundary_and_receipt_idempotently(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        receipt = self._receipt(source, target, migration_id="migration-m8-02-sqlite")
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            arguments = {
                "project_id": source["project"]["project_id"],
                "expected_revision": source["project"]["revision"],
                "expected_event_head_sha256": None,
                "expected_registry_digest": "c" * 64,
                "expected_authorization_ref": "authorization://m8-02/state-v6",
                "target_snapshot": target,
                "migration_receipt": receipt,
            }

            self.assertEqual(store.migrate_project(**arguments), receipt)
            self.assertEqual(store.migrate_project(**arguments), receipt)
            self.assertEqual(
                store.read_project(source["project"]["project_id"]), target
            )
            self.assertEqual(
                store.read_migration_receipt(
                    source["project"]["project_id"], receipt["migration_id"]
                ),
                receipt,
            )

    def test_sqlite_v6_migration_faults_roll_back_snapshot_and_receipt(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        for stage in (
            "after_migration_receipt_insert",
            "after_migration_snapshot_update",
        ):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                receipt = self._receipt(
                    source, target, migration_id=f"migration-m8-02-fault-{stage}"
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
                        expected_authorization_ref="authorization://m8-02/state-v6",
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

    def test_sqlite_allows_a_new_upgrade_after_an_explicit_rollback(self):
        from context_control_plane.shared_state_migration import (
            build_typed_state_v5_to_v6_migration_receipt,
            migrate_typed_state_v5_to_v6,
            rollback_typed_state_v6_to_v5,
        )
        from context_control_plane.sqlite_state_store import SQLiteStateStore

        source = _v5_snapshot()
        upgraded = migrate_typed_state_v5_to_v6(source)
        rolled_back = rollback_typed_state_v6_to_v5(upgraded)
        project_id = source["project"]["project_id"]
        event_head = {"sequence_no": 0, "event_sha256": None}

        def receipt(source_snapshot, target_snapshot, migration_id, migrated_at):
            return build_typed_state_v5_to_v6_migration_receipt(
                source=source_snapshot,
                target=target_snapshot,
                migration_id=migration_id,
                source_event_head=event_head,
                registry_digest="c" * 64,
                authorization_ref="authorization://m8-02/state-v6",
                migrated_at=migrated_at,
            )

        transitions = (
            (
                upgraded,
                receipt(
                    source,
                    upgraded,
                    "migration-m8-02-upgrade-1",
                    "2026-08-16T17:00:00+08:00",
                ),
            ),
            (
                rolled_back,
                receipt(
                    upgraded,
                    rolled_back,
                    "migration-m8-02-rollback",
                    "2026-08-16T17:01:00+08:00",
                ),
            ),
            (
                upgraded,
                receipt(
                    rolled_back,
                    upgraded,
                    "migration-m8-02-upgrade-2",
                    "2026-08-16T17:02:00+08:00",
                ),
            ),
        )

        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            for target, migration_receipt in transitions:
                store.migrate_project(
                    project_id=project_id,
                    expected_revision=source["project"]["revision"],
                    expected_event_head_sha256=None,
                    expected_registry_digest="c" * 64,
                    expected_authorization_ref="authorization://m8-02/state-v6",
                    target_snapshot=target,
                    migration_receipt=migration_receipt,
                )

            self.assertEqual(store.read_project(project_id), upgraded)
            for _, migration_receipt in transitions:
                self.assertEqual(
                    store.read_migration_receipt(
                        project_id, migration_receipt["migration_id"]
                    ),
                    migration_receipt,
                )

    def test_sqlite_v3_schema_upgrades_to_repeatable_migration_journal(self):
        import sqlite3

        from context_control_plane import sqlite_state_store as sqlite_contracts

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(database, isolation_level=None)
            try:
                connection.executescript(
                    sqlite_contracts._INITIAL_SCHEMA_V1
                    + "\n"
                    + sqlite_contracts._MIGRATION_SCHEMA_V2
                    + "\n"
                    + sqlite_contracts._MIGRATION_SCHEMA_V3
                )
                connection.execute(
                    f"PRAGMA application_id = {sqlite_contracts.SQLITE_APPLICATION_ID}"
                )
                connection.execute("PRAGMA user_version = 3")
            finally:
                connection.close()

            sqlite_contracts.SQLiteStateStore(database).initialize()

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 4
                )
                indexes = connection.execute(
                    "PRAGMA index_list('typed_state_migrations')"
                ).fetchall()
            finally:
                connection.close()
            self.assertFalse(any(row[2] for row in indexes if row[3] == "u"))

    def test_sqlite_v6_migration_checks_registry_and_authorization(self):
        from context_control_plane.shared_state_migration import (
            migrate_typed_state_v5_to_v6,
        )
        from context_control_plane.sqlite_state_store import (
            SQLiteStateIntegrityError,
            SQLiteStateStore,
        )

        source = _v5_snapshot()
        target = migrate_typed_state_v5_to_v6(source)
        receipt = self._receipt(source, target, migration_id="migration-m8-02-auth")
        common = {
            "project_id": source["project"]["project_id"],
            "expected_revision": source["project"]["revision"],
            "expected_event_head_sha256": None,
            "expected_registry_digest": "c" * 64,
            "expected_authorization_ref": "authorization://m8-02/state-v6",
            "target_snapshot": target,
            "migration_receipt": receipt,
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    **{**common, "expected_registry_digest": "d" * 64}
                )
            with self.assertRaises(SQLiteStateIntegrityError):
                store.migrate_project(
                    **{
                        **common,
                        "expected_authorization_ref": "authorization://m8-02/other",
                    }
                )


if __name__ == "__main__":
    unittest.main()
