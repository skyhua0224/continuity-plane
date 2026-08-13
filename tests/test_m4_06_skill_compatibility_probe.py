import copy
import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

OBSERVED_AT = "2026-08-11T16:30:00+08:00"


class RecordingAdapter:
    def __init__(self):
        self.calls = []
        self.provider_id = "codex"
        self.adapter_surface_contract = (
            "context.adapter-surface/codex-app-server-skill-input/v1alpha1"
        )

    def compose(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"provider_delivery": "composed"}


class M406SkillCompatibilityProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        try:
            cls.api = importlib.import_module(
                "context_control_plane.skill_compatibility_probe"
            )
            cls.compat = importlib.import_module(
                "context_control_plane.skill_compatibility"
            )
        except ModuleNotFoundError:
            cls.api = None
            cls.compat = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "Skill compatibility probe module is missing")
        self.assertIsNotNone(self.compat, "Skill compatibility module is missing")
        return self.api, self.compat

    def _receipt(self):
        api, _ = self._require_api()
        return api.run_skill_compatibility_probe(
            self.root,
            samples=40,
            observed_at=OBSERVED_AT,
            arguments=["--samples", "40", "--observed-at", OBSERVED_AT],
        )

    def test_blocked_decision_stops_before_provider_compose(self):
        api, compat = self._require_api()
        case = api.build_probe_case(self.root)
        decision = compat.assess_skill_compatibility(
            case["lock"], case["version_changed_manifest_set"], case["original_packet"],
            provider_contract_refs=case["provider_contract_refs"],
        )
        adapter = RecordingAdapter()
        with self.assertRaisesRegex(compat.SkillCompatibilityError, "blocked"):
            compat.compose_provider_skills(
                adapter,
                case["original_packet"],
                "loaded",
                candidate_manifest_set=case["version_changed_manifest_set"],
                compatibility_lock=case["lock"],
                provider_contract_refs=case["provider_contract_refs"],
                compatibility_decision=decision,
            )
        self.assertEqual(adapter.calls, [])

    def test_allowed_decision_reaches_provider_once(self):
        api, compat = self._require_api()
        case = api.build_probe_case(self.root)
        decision = compat.assess_skill_compatibility(
            case["lock"], case["manifest_set"], case["original_packet"],
            provider_contract_refs=case["provider_contract_refs"],
        )
        adapter = RecordingAdapter()
        result = compat.compose_provider_skills(
            adapter,
            case["original_packet"],
            "loaded",
            candidate_manifest_set=case["manifest_set"],
            compatibility_lock=case["lock"],
            provider_contract_refs=case["provider_contract_refs"],
            compatibility_decision=decision,
            layer_plan="plan",
        )
        self.assertEqual(result, {"provider_delivery": "composed"})
        self.assertEqual(len(adapter.calls), 1)

    def test_provider_gate_rejects_decision_for_a_different_packet_or_contract(self):
        api, compat = self._require_api()
        case = api.build_probe_case(self.root)
        decision = compat.assess_skill_compatibility(
            case["lock"],
            case["manifest_set"],
            case["original_packet"],
            provider_contract_refs=case["provider_contract_refs"],
        )
        for packet, contracts in (
            ({"unrelated": "packet"}, case["provider_contract_refs"]),
            (case["original_packet"], ["provider://claude/v1"]),
        ):
            with self.subTest(packet=packet, contracts=contracts):
                adapter = RecordingAdapter()
                with self.assertRaisesRegex(compat.SkillCompatibilityError, "bind"):
                    compat.compose_provider_skills(
                        adapter,
                        packet,
                        "loaded",
                        candidate_manifest_set=case["manifest_set"],
                        compatibility_lock=case["lock"],
                        provider_contract_refs=contracts,
                        compatibility_decision=decision,
                    )
                self.assertEqual(adapter.calls, [])

    def test_provider_gate_rejects_contract_version_not_supported_by_adapter(self):
        api, compat = self._require_api()
        case = api.build_probe_case(self.root)
        provider_refs = ["provider://claude/v1", "provider://codex/v2"]
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set = copy.deepcopy(case["manifest_set"])
        for manifest in manifest_set["manifests"][:2]:
            manifest["compatibility"]["provider_contract_refs"] = list(
                provider_refs
            )
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
        )
        lock = compat.create_skill_compatibility_lock(
            manifest_set,
            packet,
            task_id=case["lock"]["task_id"],
            operation_id=case["lock"]["operation_id"],
            provider_contract_refs=provider_refs,
        )
        decision = compat.assess_skill_compatibility(
            lock,
            manifest_set,
            packet,
            provider_contract_refs=provider_refs,
        )
        adapter = RecordingAdapter()

        with self.assertRaisesRegex(compat.SkillCompatibilityError, "identity"):
            compat.compose_provider_skills(
                adapter,
                packet,
                "loaded",
                candidate_manifest_set=manifest_set,
                compatibility_lock=lock,
                provider_contract_refs=provider_refs,
                compatibility_decision=decision,
            )

        self.assertEqual(adapter.calls, [])

    def test_provider_gate_does_not_follow_live_surface_registry_drift(self):
        api, compat = self._require_api()
        case = api.build_probe_case(self.root)
        decision = compat.assess_skill_compatibility(
            case["lock"],
            case["manifest_set"],
            case["original_packet"],
            provider_contract_refs=case["provider_contract_refs"],
        )
        adapter = RecordingAdapter()
        adapter.adapter_surface_contract = (
            "context.adapter-surface/codex-app-server-skill-input/v2alpha1"
        )
        from context_control_plane.provider_skill_adapter import (
            PROVIDER_SURFACE_CONTRACTS,
        )

        with patch.dict(
            PROVIDER_SURFACE_CONTRACTS,
            {"codex": adapter.adapter_surface_contract},
        ), self.assertRaisesRegex(compat.SkillCompatibilityError, "identity"):
            compat.compose_provider_skills(
                adapter,
                case["original_packet"],
                "loaded",
                candidate_manifest_set=case["manifest_set"],
                compatibility_lock=case["lock"],
                provider_contract_refs=case["provider_contract_refs"],
                compatibility_decision=decision,
            )

        self.assertEqual(adapter.calls, [])

    def test_probe_quantifies_fail_closed_migration_and_rollback(self):
        api, _ = self._require_api()
        receipt = self._receipt()
        api.validate_skill_compatibility_probe_receipt(receipt, root=self.root)
        measurement = receipt["measurement"]
        self.assertEqual(measurement["samples"], 40)
        self.assertEqual(measurement["compatible_samples"], 8)
        self.assertEqual(measurement["breaking_samples"], 32)
        self.assertEqual(measurement["unauthorized_deliveries"], 0)
        self.assertEqual(measurement["blocked_provider_compose_calls"], 0)
        self.assertEqual(measurement["compatible_provider_compose_calls"], 8)
        self.assertEqual(measurement["evidence_missing_migrations_allowed"], 0)
        self.assertEqual(measurement["valid_migrations_allowed"], 4)
        self.assertEqual(measurement["migration_replay_mismatches"], 0)
        self.assertEqual(measurement["rollback_mismatches"], 0)
        self.assertEqual(measurement["wrong_binding_migrations_allowed"], 0)
        self.assertLess(receipt["measurement"]["assessment_p95_ms"], 10.0)
        self.assertTrue(all(receipt["acceptance"].values()))
        self.assertFalse(receipt["authority_boundary"]["provider_process_invoked"])
        self.assertFalse(receipt["authority_boundary"]["state_commit_invoked"])
        self.assertEqual(
            receipt["provenance"]["provider_adapter_implementation_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/provider_skill_adapter.py").read_bytes()
            ).hexdigest(),
        )

    def test_receipt_validator_recomputes_counts_and_provenance(self):
        api, _ = self._require_api()
        receipt = self._receipt()
        mutations = (
            lambda value: value["measurement"].__setitem__("unauthorized_deliveries", 1),
            lambda value: value["measurement"].__setitem__("valid_migrations_allowed", 3),
            lambda value: value["authority_boundary"].__setitem__("provider_process_invoked", True),
            lambda value: value["provenance"].__setitem__("implementation_sha256", "a" * 64),
            lambda value: value["measurement"]["results"][0].__setitem__("decision_sha256", "f" * 64),
            lambda value: value["measurement"]["results"][0].__setitem__("change_kind", "selected-version"),
            lambda value: value["measurement"]["migration_results"][0].__setitem__("migration_sha256", "f" * 64),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(receipt)
                mutation(candidate)
                with self.assertRaises(ValueError):
                    api.validate_skill_compatibility_probe_receipt(candidate, root=self.root)

    def test_receipt_validator_rejects_unreplayable_metadata_and_claims(self):
        api, _ = self._require_api()
        receipt = self._receipt()
        mutations = (
            lambda value: value.__setitem__("observed_at", "not-rfc3339"),
            lambda value: value["environment"].__setitem__("unexpected", "field"),
            lambda value: value["environment"].__setitem__("python_version", ""),
            lambda value: value["generation"].__setitem__("command", "bogus.py"),
            lambda value: value["generation"].__setitem__("arguments", ["--samples", "39"]),
            lambda value: value.__setitem__(
                "limitations", ["Provider processes were invoked."]
            ),
            lambda value: (
                value["measurement"].__setitem__(
                    "latency_samples_ms", [False] * 40
                ),
                value["measurement"].__setitem__("assessment_p50_ms", 0.0),
                value["measurement"].__setitem__("assessment_p95_ms", 0.0),
                value["measurement"].__setitem__("assessment_max_ms", 0.0),
            ),
            lambda value: value["environment"].__setitem__(
                "external_services", False
            ),
            lambda value: value["measurement"]["results"][0].__setitem__(
                "delivery_allowed_without_migration", 1
            ),
            lambda value: value["measurement"]["results"][0].__setitem__(
                "provider_compose_calls", True
            ),
            lambda value: value["measurement"]["migration_results"][0].__setitem__(
                "migration_allowed", 1
            ),
            lambda value: value["measurement"].__setitem__(
                "unauthorized_deliveries", False
            ),
            lambda value: value["acceptance"].__setitem__(
                "forty_sample_gate_passed", 1
            ),
            lambda value: value["authority_boundary"].__setitem__(
                "provider_process_invoked", 0
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(receipt)
                mutation(candidate)
                with self.assertRaises(ValueError):
                    api.validate_skill_compatibility_probe_receipt(
                        candidate, root=self.root
                    )

    def test_runner_writes_versioned_receipt(self):
        api, _ = self._require_api()
        with tempfile.TemporaryDirectory(prefix="m4-06-probe-") as directory:
            output = Path(directory) / "receipt.yaml"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools/run_skill_compatibility_probe.py"),
                    "--samples", "40",
                    "--observed-at", OBSERVED_AT,
                    "--output", str(output),
                ],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = yaml.safe_load(output.read_text(encoding="utf-8"))
            api.validate_skill_compatibility_probe_receipt(receipt, root=self.root)

    def test_versioned_receipt_schema_and_registry_are_current(self):
        api, _ = self._require_api()
        receipt_path = self.root / "experiments/state/m4-06-skill-compatibility-results.yaml"
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        api.validate_skill_compatibility_probe_receipt(receipt, root=self.root)
        schema_path = self.root / "schemas/m4-06/skill-compatibility-probe.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(receipt)), [])
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        entry = next(item for item in registry["schemas"] if item["schema_id"] == "context.skill-compatibility-probe")
        self.assertEqual(entry["content_sha256"], hashlib.sha256(schema_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
