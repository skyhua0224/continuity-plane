"""M9-06 Obsidian vault strict schema and registry tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.external_state_provider import (
    HMACExternalStateProjectionSigner,
)
from context_control_plane.obsidian_vault import build_obsidian_vault
from context_control_plane.obsidian_vault_benchmark import benchmark_obsidian_vault

SCHEMA_ID = "context.obsidian-vault"
SCHEMA_FILE = "obsidian-vault.schema.json"
BENCHMARK_SCHEMA_ID = "context.obsidian-vault-benchmark"
BENCHMARK_SCHEMA_FILE = "obsidian-vault-benchmark.schema.json"


def _canonical_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _signed_projection(
    signer: HMACExternalStateProjectionSigner,
    *,
    schema_version: str,
    authority: dict,
    extra: dict,
) -> dict:
    projection = {
        "schema_version": schema_version,
        "project_id": "project-m9-06-schema",
        "state_revision": 9,
        "state_sha256": "a" * 64,
        "source_projection_sha256": "b" * 64,
        "authority": authority,
        **extra,
    }
    projection["projection_sha256"] = _canonical_digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def _sample_vault() -> dict:
    signer = HMACExternalStateProjectionSigner(
        key_id="key-m9-06-schema",
        secret=b"m9-06-obsidian-vault-schema-test-key",
    )
    graph = _signed_projection(
        signer,
        schema_version="context.project-graph-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "controlled_action_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "graph": {"nodes": [], "edges": [], "root_work_ids": []},
            "active_work_set": [],
            "work_ledger": {},
            "health": {},
        },
    )
    decisions = _signed_projection(
        signer,
        schema_version="context.decision-evidence-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "completion_authority": False,
            "approval_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "decision_timeline": [],
            "constraint_matrix": [],
            "evidence_matrix": [],
            "health": {},
        },
    )
    health = _signed_projection(
        signer,
        schema_version="context.context-health-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "completion_authority": False,
            "approval_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "source_projection_sha256": graph["source_projection_sha256"],
            "decision_evidence_projection_sha256": decisions["projection_sha256"],
            "context_health": {},
            "reference_health": {},
            "harness_health": {},
            "replay_health": {},
            "drilldowns": [],
            "overall_status": "passed",
        },
    )
    return build_obsidian_vault(
        project_graph=graph,
        decision_evidence=decisions,
        context_health=health,
        signer=signer,
        generated_at="2026-08-17T23:59:00Z",
    )


class M906ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_path = cls.root / "schemas/m9-06" / SCHEMA_FILE
        cls.benchmark_schema_path = cls.root / "schemas/m9-06" / BENCHMARK_SCHEMA_FILE

    def test_schema_exists_and_is_strict(self) -> None:
        self.assertTrue(self.schema_path.is_file())
        if not self.schema_path.is_file():
            return
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_runtime_vault_matches_schema_and_rejects_unmanaged_fields(self) -> None:
        self.assertTrue(self.schema_path.is_file())
        if not self.schema_path.is_file():
            return
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        vault = _sample_vault()
        validator.validate(vault)

        with self.assertRaises(ValidationError):
            validator.validate({**vault, "unexpected": True})
        for field in vault:
            missing = dict(vault)
            del missing[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validator.validate(missing)
        forged = json.loads(json.dumps(vault))
        forged["files"][0]["unexpected"] = True
        with self.assertRaises(ValidationError):
            validator.validate(forged)

    def test_registry_entry_binds_exact_schema_hash(self) -> None:
        self.assertTrue(self.schema_path.is_file())
        if not self.schema_path.is_file():
            return
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        self.assertIn(SCHEMA_ID, entries)
        if SCHEMA_ID not in entries:
            return
        entry = entries[SCHEMA_ID]
        wire_version = schema["properties"]["schema_version"]["const"]
        self.assertEqual(entry["current_wire_version"], wire_version)
        self.assertEqual(entry["supported_wire_versions"], [wire_version])
        self.assertEqual(entry["artifact_path"], f"schemas/m9-06/{SCHEMA_FILE}")
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(entry["status"], "current")
        self.assertEqual(entry["compatibility_mode"], "strict-versioned")

    def test_benchmark_schema_exists_is_strict_and_matches_runtime_receipt(
        self,
    ) -> None:
        self.assertTrue(self.benchmark_schema_path.is_file())
        if not self.benchmark_schema_path.is_file():
            return
        schema = json.loads(self.benchmark_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        receipt = benchmark_obsidian_vault(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T23:59:00+08:00",
        )
        validator.validate(receipt)
        with self.assertRaises(ValidationError):
            validator.validate({**receipt, "unexpected": True})

    def test_registry_binds_exact_benchmark_schema_hash(self) -> None:
        self.assertTrue(self.benchmark_schema_path.is_file())
        if not self.benchmark_schema_path.is_file():
            return
        schema = json.loads(self.benchmark_schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        self.assertIn(BENCHMARK_SCHEMA_ID, entries)
        if BENCHMARK_SCHEMA_ID not in entries:
            return
        entry = entries[BENCHMARK_SCHEMA_ID]
        wire_version = schema["properties"]["schema_version"]["const"]
        self.assertEqual(entry["current_wire_version"], wire_version)
        self.assertEqual(entry["supported_wire_versions"], [wire_version])
        self.assertEqual(
            entry["artifact_path"], f"schemas/m9-06/{BENCHMARK_SCHEMA_FILE}"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.benchmark_schema_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(entry["status"], "current")
        self.assertEqual(entry["compatibility_mode"], "strict-versioned")


if __name__ == "__main__":
    unittest.main()
