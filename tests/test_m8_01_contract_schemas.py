"""M8-01 strict JSON Schema and registry admission contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.durable_checkpoint_gate import (
    compose_durable_checkpoint_receipt,
)
from context_control_plane.durable_operation import (
    advance_durable_operation,
    compose_durable_operation,
    evaluate_durable_operation_recovery,
)
from context_control_plane.durable_operation_benchmark import (
    validate_durable_operation_benchmark,
)
from context_control_plane.durable_operation_runner import (
    DurableOperationRunnerError,
    validate_effect_adapter_manifest,
)
from context_control_plane.durable_state_authority import (
    compose_durable_state_receipt,
)
from context_control_plane.testing.durable_operation_fixture import (
    FixtureCheckpointGate,
    SQLiteAuthorityFixture,
    SQLiteEffectFixture,
    _prepared_fixture,
    run_fixture,
    run_real_fixture,
)

SCHEMA_WIRES = {
    "durable-operation": "context.durable-operation/v1alpha1",
    "durable-operation-recovery": "context.durable-operation-recovery/v1alpha1",
    "durable-effect-adapter": "context.durable-effect-adapter/v1alpha1",
    "durable-state-receipt": "context.durable-state-receipt/v1alpha1",
    "durable-authority-adapter": "context.durable-authority-adapter/v1alpha1",
    "durable-checkpoint-gate": "context.durable-checkpoint-gate/v1alpha1",
    "durable-checkpoint-adapter": "context.durable-checkpoint-adapter/v1alpha1",
    "durable-operation-crash-fixture": (
        "context.durable-operation-crash-fixture/v1alpha1"
    ),
    "durable-operation-real-crash-fixture": (
        "context.durable-operation-real-crash-fixture/v1alpha1"
    ),
    "durable-operation-benchmark": (
        "context.durable-operation-benchmark/v1alpha1"
    ),
    "deepseek-checkpoint-receipt": (
        "context.deepseek-checkpoint-receipt/v1alpha1"
    ),
}


class M801ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.prepared = _prepared_fixture()
        cls.recovery = evaluate_durable_operation_recovery(cls.prepared)
        cls.state_receipt = compose_durable_state_receipt(
            cls.prepared,
            action="authorize",
            request_id="request-m8-01-schema-authorize",
            request_sha256="1" * 64,
            revision=cls.prepared["authority"]["project_revision"] + 1,
            event_head={"sequence_no": 1268, "event_sha256": "2" * 64},
            result_ref=None,
            registry_digest="3" * 64,
            state_response_sha256="4" * 64,
            reconciled=False,
        )
        cls.checkpoint_receipt = compose_durable_checkpoint_receipt(
            cls.prepared, critical_projection_sha256="5" * 64
        )
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.crash_receipt = run_fixture(
            Path(cls.fixture_directory.name), crash_point=None
        )
        cls.real_fixture_directory = tempfile.TemporaryDirectory()
        cls.real_crash_receipt = run_real_fixture(
            Path(cls.real_fixture_directory.name),
            repository_root=cls.root,
            crash_point=None,
        )
        cls.benchmark_fixture = {
            "schema_version": "context.durable-operation-benchmark/v1alpha1",
            "benchmark_id": "benchmark-m8-01-" + "a" * 24,
            "generated_at": "2026-08-16T12:00:00+08:00",
            "samples": 1,
            "crash_points": [
                "after-external-effect"
            ],
            "provider_oracle_fixture_sha256": "6" * 64,
            "scenario_count": 1,
            "terminal_recoveries": 1,
            "authority_backend": "state-mcp-sqlite",
            "checkpoint_backend": "content-addressed-local",
            "provider_invocations": 0,
            "deduplicated_provider_invocations": 0,
            "effect_adapter_invocations": 1,
            "deduplicated_effect_invocations": 0,
            "applied_effects": 1,
            "duplicate_semantic_effects": 0,
            "authority_intent_commits": 1,
            "authority_state_commits": 1,
            "restore_latency_ms": {"p50": 1.0, "p95": 1.0, "max": 1.0},
            "state_write_authority": False,
            "provider_native_authority": False,
            "receipt_sha256": "7" * 64,
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_directory.cleanup()
        cls.real_fixture_directory.cleanup()

    @classmethod
    def instances(cls) -> dict[str, dict]:
        return {
            "durable-operation": cls.prepared,
            "durable-operation-recovery": cls.recovery,
            "durable-effect-adapter": SQLiteEffectFixture.capability_manifest,
            "durable-state-receipt": cls.state_receipt,
            "durable-authority-adapter": SQLiteAuthorityFixture.capability_manifest,
            "durable-checkpoint-gate": cls.checkpoint_receipt,
            "durable-checkpoint-adapter": FixtureCheckpointGate.capability_manifest,
            "durable-operation-crash-fixture": cls.crash_receipt,
            "durable-operation-real-crash-fixture": cls.real_crash_receipt,
            "durable-operation-benchmark": cls.benchmark_fixture,
        }

    def schema(self, name: str) -> dict:
        path = self.root / "schemas" / "m8-01" / f"{name}.schema.json"
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.fail(f"M8-01 schema is missing: {path}")
        Draft202012Validator.check_schema(schema)
        return schema

    def validate_schema(self, name: str, instance: object) -> None:
        Draft202012Validator(
            self.schema(name), format_checker=FormatChecker()
        ).validate(instance)

    def test_runtime_contracts_and_schema_only_benchmark_match_strict_schemas(
        self,
    ) -> None:
        for name, instance in self.instances().items():
            with self.subTest(schema=name):
                schema = self.schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(
                    set(schema["required"]), set(schema["properties"])
                )
                self.validate_schema(name, instance)

    def test_all_top_level_schemas_reject_unknown_fields(self) -> None:
        for name, instance in self.instances().items():
            with self.subTest(schema=name):
                changed = copy.deepcopy(instance)
                changed["unexpected"] = True
                with self.assertRaises(ValidationError):
                    self.validate_schema(name, changed)

    def test_operation_schema_enforces_runtime_terminal_phase_fields(self) -> None:
        intent = advance_durable_operation(
            self.prepared,
            phase="intent-committed",
            observed_at="2026-08-16T12:00:01+08:00",
            continuation_sha256="1" * 64,
            intent_ref="state-receipt://sha256/" + "2" * 64,
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T12:00:02+08:00",
            continuation_sha256="3" * 64,
            start_ref="attempt://m8-01/schema/1",
        )
        settled = advance_durable_operation(
            started,
            phase="effect-settled",
            observed_at="2026-08-16T12:00:03+08:00",
            continuation_sha256="4" * 64,
            settlement_ref="settlement://m8-01/schema",
            result_ref="artifact://sha256/" + "5" * 64,
        )
        committed = advance_durable_operation(
            settled,
            phase="response-committed",
            observed_at="2026-08-16T12:00:04+08:00",
            continuation_sha256="6" * 64,
            state_commit_ref="state-receipt://sha256/" + "7" * 64,
        )
        terminal = advance_durable_operation(
            committed,
            phase="terminal",
            observed_at="2026-08-16T12:00:05+08:00",
            continuation_sha256="8" * 64,
        )
        self.validate_schema("durable-operation", terminal)

        for field in (
            "intent_ref",
            "start_ref",
            "settlement_ref",
            "result_ref",
            "state_commit_ref",
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(terminal)
                forged[field] = None
                with self.assertRaises(ValidationError):
                    self.validate_schema("durable-operation", forged)

    def test_authority_flags_cannot_be_elevated_by_contract_documents(self) -> None:
        for name, instance in self.instances().items():
            for field in ("state_write_authority", "provider_native_authority"):
                if field not in instance:
                    continue
                with self.subTest(schema=name, field=field):
                    changed = copy.deepcopy(instance)
                    changed[field] = True
                    with self.assertRaises(ValidationError):
                        self.validate_schema(name, changed)

    def test_effect_adapter_capabilities_have_one_canonical_wire_order(self) -> None:
        changed = copy.deepcopy(SQLiteEffectFixture.capability_manifest)
        changed["replay_policies"] = ["safe", "never"]

        with self.assertRaises(ValidationError):
            self.validate_schema("durable-effect-adapter", changed)
        with self.assertRaises(DurableOperationRunnerError):
            validate_effect_adapter_manifest(changed)

    def test_never_effect_recovery_cannot_authorize_automatic_replay(self) -> None:
        effect = copy.deepcopy(self.prepared["effect"])
        effect.update(
            {
                "replay_policy": "never",
                "idempotency_mode": "none",
                "status_lookup": "none",
            }
        )
        prepared = compose_durable_operation(
            operation_id="operation/m8-01/schema-never",
            project_id=self.prepared["project_id"],
            work_id=self.prepared["work_id"],
            claim_id=self.prepared["claim_id"],
            authority=self.prepared["authority"],
            effect=effect,
            checkpoint_ref=self.prepared["checkpoint_ref"],
            continuation_sha256="9" * 64,
            trace_binding=self.prepared["trace_binding"],
            observed_at="2026-08-16T12:00:00+08:00",
        )
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T12:00:01+08:00",
            continuation_sha256="a" * 64,
            intent_ref="state-receipt://sha256/" + "b" * 64,
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T12:00:02+08:00",
            continuation_sha256="c" * 64,
            start_ref="attempt://effect/m8-01/schema-never/1",
        )
        recovery = evaluate_durable_operation_recovery(started)

        self.assertFalse(recovery["automatic_effect_replay"])
        self.assertEqual(recovery["recovery_action"], "manual")
        self.validate_schema("durable-operation-recovery", recovery)

        forged = copy.deepcopy(recovery)
        forged["automatic_effect_replay"] = True
        with self.assertRaises(ValidationError):
            self.validate_schema("durable-operation-recovery", forged)

    def test_registry_admits_each_wire_once_with_current_artifact_hash(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        for wire in SCHEMA_WIRES.values():
            schema_id, _ = wire.rsplit("/", 1)
            entries = [
                item
                for item in registry["schemas"]
                if item["schema_id"] == schema_id
            ]
            with self.subTest(schema_id=schema_id):
                self.assertEqual(len(entries), 1)
                entry = entries[0]
                self.assertEqual(entry["current_semver"], "1.0.0-alpha.1")
                self.assertEqual(entry["current_wire_version"], wire)
                self.assertEqual(entry["supported_wire_versions"], [wire])
                self.assertEqual(entry["compatibility_mode"], "strict-versioned")
                self.assertEqual(entry["status"], "current")
                self.assertEqual(entry["migrations"], [])
                artifact = self.root / entry["artifact_path"]
                self.assertEqual(
                    entry["content_sha256"],
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                )

    def test_committed_benchmark_receipt_covers_every_crash_point(self) -> None:
        path = (
            self.root
            / "experiments/evidence/m8-01-durable-operation-results.json"
        )
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.fail(f"M8-01 benchmark receipt is missing: {path}")

        validate_durable_operation_benchmark(receipt)
        self.validate_schema("durable-operation-benchmark", receipt)
        self.assertGreaterEqual(receipt["samples"], 2)
        self.assertEqual(set(receipt["crash_points"]), {
            "after-prepared",
            "after-intent-commit",
            "after-intent-record",
            "after-effect-start",
            "after-external-effect",
            "after-effect-settlement",
            "after-state-commit",
            "after-response-record",
            "after-terminal",
        })
        self.assertEqual(receipt["duplicate_semantic_effects"], 0)


if __name__ == "__main__":
    unittest.main()
