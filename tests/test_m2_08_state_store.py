import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

import context_control_plane.state_store as state_store_contracts
from context_control_plane.postgres_state_store import (
    PostgresStateConflict,
    PostgresStateIntegrityError,
    PostgresStateNotFound,
    PostgresStateStore,
    PostgresStateStoreError,
)
from context_control_plane.state_store import (
    StateStore,
    StateStoreCapabilityError,
    StateStoreConflict,
    StateStoreError,
    StateStoreIntegrityError,
    StateStoreNotFound,
    invoke_state_store,
    validate_state_store_adapter,
)


class M208StateStoreCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]

    def test_adapter_without_capability_manifest_is_rejected_before_use(self):
        class UndeclaredAdapter:
            def __init__(self):
                self.calls = 0

            def initialize(self):
                self.calls += 1

        adapter = UndeclaredAdapter()

        with self.assertRaisesRegex(StateStoreCapabilityError, "capability_manifest"):
            validate_state_store_adapter(adapter)

        self.assertEqual(adapter.calls, 0)

    def test_declared_operation_without_a_callable_is_rejected_before_use(self):
        class IncompleteAdapter:
            capability_manifest = replace(
                PostgresStateStore.capability_manifest,
                adapter_id="context.test-incomplete",
            )

            def __init__(self):
                self.initialize_calls = 0

            def initialize(self):
                self.initialize_calls += 1

        adapter = IncompleteAdapter()

        with self.assertRaisesRegex(StateStoreCapabilityError, "not callable"):
            invoke_state_store(adapter, "read_project", "project-1")

        self.assertEqual(adapter.initialize_calls, 0)

    def test_postgres_manifest_matches_proven_capabilities(self):
        store = PostgresStateStore("postgresql://unused")

        manifest = validate_state_store_adapter(store)

        self.assertEqual(
            manifest.schema_version, "context.state-store-capabilities/v1alpha1"
        )
        self.assertEqual(manifest.adapter_id, "context.postgresql")
        self.assertEqual(manifest.authority_mode, "shared")
        self.assertEqual(
            manifest.operations,
            ("create_project", "read_project", "read_events", "commit_event"),
        )
        self.assertTrue(manifest.shared_authority)
        self.assertFalse(manifest.offline_write)
        self.assertTrue(manifest.multi_writer)
        self.assertTrue(manifest.expected_revision)
        self.assertFalse(manifest.unique_claim)
        self.assertEqual(manifest.lease_clock, "none")
        self.assertEqual(manifest.artifact_scope, "none")
        self.assertFalse(manifest.migration_source)
        self.assertFalse(manifest.migration_target)

    def test_unique_claim_requires_a_backend_lease_clock(self):
        store = PostgresStateStore("postgresql://unused")
        store.capability_manifest = replace(
            store.capability_manifest,
            unique_claim=True,
        )

        with self.assertRaisesRegex(StateStoreCapabilityError, "lease_clock"):
            validate_state_store_adapter(store)

    def test_postgres_exceptions_preserve_generic_and_legacy_catch_boundaries(self):
        self.assertTrue(issubclass(PostgresStateStoreError, StateStoreError))
        self.assertTrue(issubclass(PostgresStateConflict, StateStoreConflict))
        self.assertTrue(issubclass(PostgresStateConflict, PostgresStateStoreError))
        self.assertTrue(issubclass(PostgresStateNotFound, StateStoreNotFound))
        self.assertTrue(issubclass(PostgresStateNotFound, PostgresStateStoreError))
        self.assertTrue(
            issubclass(PostgresStateIntegrityError, StateStoreIntegrityError)
        )
        self.assertTrue(
            issubclass(PostgresStateIntegrityError, PostgresStateStoreError)
        )

    def test_capability_schema_is_strict_versioned_and_registered(self):
        schema_path = (
            self.root / "schemas" / "m2-08" / "state-store-capabilities.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.state-store-capabilities"
        )

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertTrue(schema["properties"]["operations"]["uniqueItems"])
        self.assertEqual(
            entry["current_wire_version"],
            "context.state-store-capabilities/v2alpha1",
        )
        self.assertIn(
            "context.state-store-capabilities/v1alpha1",
            entry["supported_wire_versions"],
        )
        current_schema_path = self.root / entry["artifact_path"]
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(current_schema_path.read_bytes()).hexdigest(),
        )
        Draft202012Validator.check_schema(
            json.loads(current_schema_path.read_text(encoding="utf-8"))
        )

    def test_authoritative_commit_requires_expected_revision(self):
        store = PostgresStateStore("postgresql://unused")
        store.capability_manifest = replace(
            store.capability_manifest,
            expected_revision=False,
        )

        with self.assertRaisesRegex(StateStoreCapabilityError, "expected_revision"):
            validate_state_store_adapter(store)

    def test_projection_cannot_dispatch_an_undeclared_authority_commit(self):
        class ProjectionAdapter:
            capability_manifest = replace(
                PostgresStateStore.capability_manifest,
                adapter_id="context.test-projection",
                authority_mode="projection",
                operations=("read_project", "read_events"),
                shared_authority=False,
                multi_writer=False,
            )

            def __init__(self):
                self.commit_calls = 0

            def initialize(self):
                pass

            def read_project(self, project_id):
                return {"project_id": project_id}

            def read_events(self, project_id):
                return []

            def commit_event(self, **kwargs):
                self.commit_calls += 1

        adapter = ProjectionAdapter()

        with self.assertRaisesRegex(StateStoreCapabilityError, "not declared"):
            invoke_state_store(adapter, "commit_event")

        self.assertEqual(adapter.commit_calls, 0)

    def test_projection_cannot_advertise_authoritative_capabilities(self):
        store = PostgresStateStore("postgresql://unused")
        store.capability_manifest = replace(
            store.capability_manifest,
            authority_mode="projection",
        )

        with self.assertRaisesRegex(StateStoreCapabilityError, "projection"):
            validate_state_store_adapter(store)

    def test_manifest_rejects_values_outside_the_schema_domain(self):
        cases = (
            (
                {"schema_version": "context.state-store-capabilities/v2"},
                "schema_version",
            ),
            ({"adapter_id": 1}, "adapter_id"),
            ({"adapter_version": None}, "adapter_version"),
            ({"authority_mode": "unknown"}, "authority_mode"),
            ({"authority_mode": []}, "authority_mode"),
            ({"operations": ("read_project", "read_project")}, "operations"),
            ({"operations": ("drop_project",)}, "operations"),
            ({"lease_clock": "wall"}, "lease_clock"),
            ({"lease_clock": []}, "lease_clock"),
            ({"artifact_scope": "remote"}, "artifact_scope"),
            ({"artifact_scope": []}, "artifact_scope"),
            (
                {
                    "unique_claim": True,
                    "shared_authority": False,
                    "lease_clock": "backend",
                },
                "shared_authority",
            ),
            ({"authority_mode": "local", "shared_authority": True}, "authority_mode"),
        )

        for changes, expected_error in cases:
            with self.subTest(changes=changes):
                store = PostgresStateStore("postgresql://unused")
                store.capability_manifest = replace(
                    store.capability_manifest,
                    **changes,
                )
                with self.assertRaisesRegex(
                    StateStoreCapabilityError,
                    expected_error,
                ):
                    validate_state_store_adapter(store)

    def test_postgres_implements_the_minimal_state_store_protocol(self):
        self.assertIsInstance(PostgresStateStore("postgresql://unused"), StateStore)

    def test_projection_satisfies_the_base_spi_without_authoritative_methods(self):
        class ProjectionAdapter:
            capability_manifest = replace(
                PostgresStateStore.capability_manifest,
                adapter_id="context.test-projection-protocol",
                authority_mode="projection",
                operations=("read_project", "read_events"),
                shared_authority=False,
                multi_writer=False,
            )

            def initialize(self):
                pass

            def read_project(self, project_id):
                return {"project_id": project_id}

            def read_events(self, project_id):
                return []

        self.assertTrue(
            hasattr(state_store_contracts, "AuthoritativeStateStore"),
            "authoritative and capability-gated base Protocols must be distinct",
        )
        authoritative_protocol = state_store_contracts.AuthoritativeStateStore
        adapter = ProjectionAdapter()

        self.assertIsInstance(adapter, StateStore)
        self.assertNotIsInstance(adapter, authoritative_protocol)
        self.assertEqual(
            invoke_state_store(adapter, "read_events", "project-1"),
            [],
        )

    def test_manifest_descriptor_is_rejected_without_executing_the_getter(self):
        class DescriptorAdapter:
            def __init__(self):
                self.getter_calls = 0

            @property
            def capability_manifest(self):
                self.getter_calls += 1
                return PostgresStateStore.capability_manifest

            def initialize(self):
                pass

        adapter = DescriptorAdapter()

        with self.assertRaises(StateStoreCapabilityError):
            validate_state_store_adapter(adapter)

        self.assertEqual(adapter.getter_calls, 0)

    def test_operation_descriptor_is_rejected_without_executing_the_getter(self):
        class DescriptorAdapter:
            capability_manifest = replace(
                PostgresStateStore.capability_manifest,
                adapter_id="context.test-operation-descriptor",
                authority_mode="projection",
                operations=("read_project", "read_events"),
                shared_authority=False,
                multi_writer=False,
            )

            def __init__(self):
                self.getter_calls = 0

            def initialize(self):
                pass

            @property
            def read_project(self):
                self.getter_calls += 1
                return lambda project_id: {"project_id": project_id}

            def read_events(self, project_id):
                return []

        adapter = DescriptorAdapter()

        with self.assertRaises(StateStoreCapabilityError):
            validate_state_store_adapter(adapter)

        self.assertEqual(adapter.getter_calls, 0)

    def test_initialize_uses_the_guarded_lifecycle_boundary(self):
        class InitializableAdapter:
            capability_manifest = replace(
                PostgresStateStore.capability_manifest,
                adapter_id="context.test-initialize",
                authority_mode="projection",
                operations=("read_project", "read_events"),
                shared_authority=False,
                multi_writer=False,
            )

            def __init__(self):
                self.initialize_calls = 0

            def initialize(self):
                self.initialize_calls += 1

            def read_project(self, project_id):
                return {"project_id": project_id}

            def read_events(self, project_id):
                return []

        adapter = InitializableAdapter()

        state_store_contracts.initialize_state_store(adapter)

        self.assertEqual(adapter.initialize_calls, 1)

    def test_capability_manifest_document_round_trips_through_the_json_schema(self):
        self.assertTrue(
            hasattr(state_store_contracts, "capability_manifest_to_document")
        )
        self.assertTrue(
            hasattr(state_store_contracts, "capability_manifest_from_document")
        )
        to_document = state_store_contracts.capability_manifest_to_document
        from_document = state_store_contracts.capability_manifest_from_document
        manifest = PostgresStateStore.capability_manifest
        schema = json.loads(
            (
                self.root / "schemas" / "m2-08" / "state-store-capabilities.schema.json"
            ).read_text(encoding="utf-8")
        )

        document = to_document(manifest)

        self.assertTrue(Draft202012Validator(schema).is_valid(document))
        self.assertIsInstance(document["operations"], list)
        self.assertEqual(from_document(document), manifest)
        self.assertEqual(to_document(from_document(document)), document)

    def test_capability_document_rejects_non_string_operations_with_contract_error(
        self,
    ):
        manifest = PostgresStateStore.capability_manifest
        base_document = {
            **manifest.__dict__,
            "operations": list(manifest.operations),
        }

        for invalid_operation in ([], {}, None, 1):
            with self.subTest(invalid_operation=invalid_operation):
                document = {
                    **base_document,
                    "operations": [invalid_operation],
                }

                with self.assertRaisesRegex(
                    StateStoreCapabilityError,
                    "operations",
                ):
                    state_store_contracts.capability_manifest_from_document(document)

    def test_json_schema_and_runtime_reject_the_same_cross_field_conflicts(self):
        schema = json.loads(
            (
                self.root / "schemas" / "m2-08" / "state-store-capabilities.schema.json"
            ).read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        manifest = PostgresStateStore.capability_manifest
        base_document = {
            **manifest.__dict__,
            "operations": list(manifest.operations),
        }
        cases = (
            {
                "authority_mode": "projection",
                "shared_authority": False,
                "multi_writer": False,
            },
            {"authority_mode": "local", "shared_authority": True},
            {
                "unique_claim": True,
                "shared_authority": True,
                "lease_clock": "none",
            },
            {"expected_revision": False},
        )

        for changes in cases:
            with self.subTest(changes=changes):
                document = {**base_document, **changes}
                runtime_changes = dict(changes)
                if "operations" in runtime_changes:
                    runtime_changes["operations"] = tuple(runtime_changes["operations"])
                store = PostgresStateStore("postgresql://unused")
                store.capability_manifest = replace(manifest, **runtime_changes)

                self.assertFalse(validator.is_valid(document))
                with self.assertRaises(StateStoreCapabilityError):
                    validate_state_store_adapter(store)


if __name__ == "__main__":
    unittest.main()
