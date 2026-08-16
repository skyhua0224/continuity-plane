"""M8-02 scoped claim and lease capability contracts."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.postgres_state_store import PostgresStateStore
from context_control_plane.shared_state_migration import migrate_typed_state_v5_to_v6
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_store_capabilities_v2 import (
    CAPABILITY_SCHEMA_VERSION,
    SQLiteLocalCoordinatorStateStore,
    StateStoreCapabilityManifestV2,
    StateStoreCapabilityV2Error,
    capability_manifest_v2_from_document,
    capability_manifest_v2_to_document,
    project_state_store_capabilities_v2,
    validate_state_store_adapter_v2,
    validate_state_store_capability_manifest_v2,
)
from tests.test_m8_02_typed_state_v6_migration import _v5_snapshot


def _coordinator_snapshot() -> dict:
    snapshot = migrate_typed_state_v5_to_v6(_v5_snapshot())
    work = next(item for item in snapshot["works"] if item["work_id"] == "work-active")
    work["status"] = "ready"
    work["owner_refs"] = ["actor-proof"]
    snapshot["project"]["active_work_ids"] = []
    snapshot["project"]["primary_work_id"] = None
    snapshot["claims"] = []
    snapshot["effects"] = []
    snapshot["project"]["effect_high_watermark"] = 0
    return snapshot


class M802StateStoreCapabilityV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).parents[1]
        cls.schema_path = (
            cls.root
            / "schemas"
            / "m8-02"
            / "state-store-capabilities-v2alpha1.schema.json"
        )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"

    def test_sqlite_v1_projects_only_proven_local_embedded_capabilities(self) -> None:
        store = SQLiteStateStore(self.database_path)

        manifest = project_state_store_capabilities_v2(store)

        self.assertEqual(manifest.schema_version, CAPABILITY_SCHEMA_VERSION)
        self.assertEqual(manifest.adapter_id, "context.sqlite")
        self.assertEqual(manifest.authority_mode, "local")
        self.assertEqual(manifest.unique_claim_scope, "none")
        self.assertEqual(manifest.lease_clock, "none")
        self.assertFalse(manifest.atomic_lease_transition)
        self.assertFalse(manifest.fencing)
        self.assertFalse(manifest.durable_request_receipts)
        self.assertTrue(manifest.expected_revision)

    def test_local_coordinator_is_opt_in_and_authority_instance_scoped(self) -> None:
        base = SQLiteStateStore(self.database_path)
        coordinator = SQLiteLocalCoordinatorStateStore(base)

        manifest = validate_state_store_adapter_v2(coordinator)

        self.assertIsNot(coordinator, base)
        self.assertEqual(manifest.adapter_id, "context.sqlite-local-coordinator")
        self.assertEqual(manifest.authority_mode, "local")
        self.assertEqual(manifest.unique_claim_scope, "authority-instance")
        self.assertEqual(manifest.lease_clock, "process")
        self.assertTrue(manifest.atomic_lease_transition)
        self.assertTrue(manifest.fencing)
        self.assertTrue(manifest.durable_request_receipts)
        self.assertIn("execute_work_ledger", manifest.operations)
        self.assertEqual(
            project_state_store_capabilities_v2(base).unique_claim_scope,
            "none",
        )

    def test_local_coordinator_delegates_the_v1_state_store_surface(self) -> None:
        coordinator = SQLiteLocalCoordinatorStateStore(
            SQLiteStateStore(self.database_path)
        )

        self.assertEqual(
            coordinator.capability_manifest.adapter_id,
            "context.sqlite-local-coordinator",
        )
        self.assertEqual(
            coordinator.capability_manifest.operations,
            SQLiteStateStore.capability_manifest.operations,
        )
        self.assertEqual(
            SQLiteStateStore.capability_manifest.adapter_id,
            "context.sqlite",
        )
        for operation in (
            "initialize",
            "create_project",
            "read_project",
            "read_events",
            "commit_event",
            "initialize_work_ledger",
            "execute_work_ledger",
            "read_work_ledger",
            "read_work_ledger_receipt",
        ):
            with self.subTest(operation=operation):
                self.assertTrue(callable(getattr(coordinator, operation)))

    def test_local_coordinator_claim_capabilities_are_backed_by_durable_operations(
        self,
    ) -> None:
        base = SQLiteStateStore(self.database_path)
        base.initialize()
        source = _coordinator_snapshot()
        base.create_project(source)
        coordinator = SQLiteLocalCoordinatorStateStore(base)
        work = next(
            item for item in source["works"] if item["work_id"] == "work-active"
        )
        coordinator.initialize_work_ledger(
            project_id=source["project"]["project_id"],
            project_revision=source["project"]["revision"],
            works=source["works"],
            max_ttl_ms=1_000,
        )
        arguments = {
            "work_id": work["work_id"],
            "actor_ref": "actor-proof",
            "expected_project_revision": source["project"]["revision"],
            "observed_at": "2026-08-16T10:00:00+00:00",
            "requested_ttl_ms": 500,
            "claim_id": "claim-proof",
            "scope_owners": work["scope_refs"],
        }
        accepted = coordinator.execute_work_ledger(
            project_id=source["project"]["project_id"],
            operation="acquire_claim",
            request_id="request-proof",
            arguments=arguments,
        )
        reopened = SQLiteLocalCoordinatorStateStore(
            SQLiteStateStore(self.database_path)
        )

        self.assertEqual(
            reopened.execute_work_ledger(
                project_id=source["project"]["project_id"],
                operation="acquire_claim",
                request_id="request-proof",
                arguments=arguments,
            ),
            accepted,
        )
        canonical = reopened.read_project(source["project"]["project_id"])
        self.assertEqual(
            reopened.read_work_ledger(source["project"]["project_id"])[
                "project_revision"
            ],
            source["project"]["revision"] + 1,
        )
        self.assertEqual(
            canonical["project"]["revision"], source["project"]["revision"] + 1
        )
        self.assertEqual(canonical["claims"][0]["claim_id"], "claim-proof")
        events = reopened.read_events(source["project"]["project_id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["revision_after"], canonical["project"]["revision"])
        self.assertEqual(
            reopened.read_work_ledger_receipt(
                source["project"]["project_id"],
                "acquire_claim",
                "request-proof",
            ),
            accepted,
        )

    def test_local_coordinator_refuses_a_ledger_without_canonical_v6_project(
        self,
    ) -> None:
        base = SQLiteStateStore(self.database_path)
        base.initialize()
        coordinator = SQLiteLocalCoordinatorStateStore(base)

        with self.assertRaisesRegex(Exception, "project|typed-state v6"):
            coordinator.initialize_work_ledger(
                project_id="project-missing",
                project_revision=0,
                works=[],
                max_ttl_ms=1_000,
            )

    def test_local_coordinator_fault_rolls_back_state_event_and_receipt(self) -> None:
        def fail_before_commit(point: str) -> None:
            if point == "before_work_ledger_commit":
                raise RuntimeError("injected coordinator fault")

        base = SQLiteStateStore(self.database_path, fault_hook=fail_before_commit)
        base.initialize()
        source = _coordinator_snapshot()
        base.create_project(source)
        coordinator = SQLiteLocalCoordinatorStateStore(base)
        work = next(
            item for item in source["works"] if item["work_id"] == "work-active"
        )
        coordinator.initialize_work_ledger(
            project_id=source["project"]["project_id"],
            project_revision=source["project"]["revision"],
            works=source["works"],
            max_ttl_ms=1_000,
        )

        with self.assertRaisesRegex(RuntimeError, "injected coordinator fault"):
            coordinator.execute_work_ledger(
                project_id=source["project"]["project_id"],
                operation="acquire_claim",
                request_id="request-fault",
                arguments={
                    "work_id": work["work_id"],
                    "actor_ref": "actor-proof",
                    "expected_project_revision": source["project"]["revision"],
                    "observed_at": "2026-08-16T10:00:00+00:00",
                    "requested_ttl_ms": 500,
                    "claim_id": "claim-fault",
                    "scope_owners": work["scope_refs"],
                },
            )

        reopened = SQLiteLocalCoordinatorStateStore(
            SQLiteStateStore(self.database_path)
        )
        self.assertEqual(reopened.read_project(source["project"]["project_id"]), source)
        self.assertEqual(reopened.read_events(source["project"]["project_id"]), [])
        self.assertEqual(
            reopened.read_work_ledger(source["project"]["project_id"])["claims"], []
        )
        self.assertIsNone(
            reopened.read_work_ledger_receipt(
                source["project"]["project_id"],
                "acquire_claim",
                "request-fault",
            )
        )

    def test_current_postgres_projects_without_shared_claim_overclaim(self) -> None:
        store = PostgresStateStore("postgresql://unused")

        manifest = project_state_store_capabilities_v2(store)

        self.assertEqual(manifest.authority_mode, "shared")
        self.assertEqual(manifest.unique_claim_scope, "none")
        self.assertEqual(manifest.lease_clock, "none")
        self.assertFalse(manifest.atomic_lease_transition)
        self.assertFalse(manifest.fencing)
        with self.assertRaisesRegex(
            StateStoreCapabilityV2Error,
            "shared-strong",
        ):
            project_state_store_capabilities_v2(
                store,
                requested_runtime_profile="shared-strong",
            )

    def test_runtime_profile_projection_rejects_unsupported_or_forged_profiles(
        self,
    ) -> None:
        store = SQLiteStateStore(self.database_path)

        with self.assertRaisesRegex(
            StateStoreCapabilityV2Error,
            "local-coordinator",
        ):
            project_state_store_capabilities_v2(
                store,
                requested_runtime_profile="local-coordinator",
            )
        with self.assertRaisesRegex(
            StateStoreCapabilityV2Error,
            "runtime profile",
        ):
            project_state_store_capabilities_v2(
                store,
                requested_runtime_profile="postgres-required",
            )

    def test_validator_rejects_scope_clock_and_proof_overclaims(self) -> None:
        valid = SQLiteLocalCoordinatorStateStore(
            SQLiteStateStore(self.database_path)
        ).capability_manifest_v2
        invalid_cases = (
            (
                replace(valid, unique_claim_scope="shared"),
                "shared.*authority_mode|authority_mode.*shared",
            ),
            (
                replace(valid, lease_clock="backend"),
                "authority-instance.*process|process.*authority-instance",
            ),
            (
                replace(valid, atomic_lease_transition=False),
                "atomic_lease_transition",
            ),
            (
                replace(valid, fencing=False),
                "fencing",
            ),
            (
                replace(valid, expected_revision=False),
                "expected_revision",
            ),
            (
                replace(valid, durable_request_receipts=1),
                "durable_request_receipts.*boolean",
            ),
        )

        for manifest, message in invalid_cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(StateStoreCapabilityV2Error, message),
            ):
                validate_state_store_capability_manifest_v2(manifest)

    def test_shared_claim_requires_backend_clock_and_strong_proofs(self) -> None:
        base = project_state_store_capabilities_v2(
            PostgresStateStore("postgresql://unused")
        )
        valid = replace(
            base,
            unique_claim_scope="shared",
            lease_clock="backend",
            atomic_lease_transition=True,
            fencing=True,
            durable_request_receipts=True,
        )

        validate_state_store_capability_manifest_v2(valid)
        for change, message in (
            ({"lease_clock": "process"}, "shared.*backend|backend.*shared"),
            ({"atomic_lease_transition": False}, "atomic_lease_transition"),
            ({"fencing": False}, "fencing"),
            ({"durable_request_receipts": False}, "durable_request_receipts"),
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(StateStoreCapabilityV2Error, message),
            ):
                validate_state_store_capability_manifest_v2(replace(valid, **change))

    def test_none_scope_cannot_claim_a_lease_clock_or_lease_proofs(self) -> None:
        valid = project_state_store_capabilities_v2(
            SQLiteStateStore(self.database_path)
        )
        for change, message in (
            ({"lease_clock": "process"}, "none.*lease_clock|lease_clock.*none"),
            ({"atomic_lease_transition": True}, "atomic_lease_transition"),
            ({"fencing": True}, "fencing"),
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(StateStoreCapabilityV2Error, message),
            ):
                validate_state_store_capability_manifest_v2(replace(valid, **change))

    def test_malformed_enum_and_operation_values_are_domain_errors(self) -> None:
        valid = project_state_store_capabilities_v2(
            SQLiteStateStore(self.database_path)
        )
        for change, message in (
            ({"authority_mode": []}, "authority_mode"),
            ({"unique_claim_scope": []}, "unique_claim_scope"),
            ({"lease_clock": []}, "lease_clock"),
            ({"artifact_scope": []}, "artifact_scope"),
            ({"operations": ([],)}, "operations"),
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(StateStoreCapabilityV2Error, message),
            ):
                validate_state_store_capability_manifest_v2(replace(valid, **change))

    def test_document_round_trip_is_strict_and_schema_equivalent(self) -> None:
        manifest = SQLiteLocalCoordinatorStateStore(
            SQLiteStateStore(self.database_path)
        ).capability_manifest_v2
        document = capability_manifest_v2_to_document(manifest)

        self.assertEqual(
            capability_manifest_v2_from_document(document),
            manifest,
        )
        self.assertEqual(
            set(document),
            set(StateStoreCapabilityManifestV2.__dataclass_fields__),
        )
        with self.assertRaisesRegex(StateStoreCapabilityV2Error, "fields"):
            capability_manifest_v2_from_document({**document, "unknown": True})

        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(document)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

        for field, value in (
            ("unique_claim_scope", "global"),
            ("lease_clock", "wall"),
            ("durable_request_receipts", 1),
        ):
            changed = copy.deepcopy(document)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validator.validate(changed)


if __name__ == "__main__":
    unittest.main()
