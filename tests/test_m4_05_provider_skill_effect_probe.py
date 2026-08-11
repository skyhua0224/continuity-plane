import copy
import hashlib
import importlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator


OBSERVED_AT = "2026-08-11T14:15:00+08:00"


class M405ProviderSkillEffectProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        try:
            cls.api = importlib.import_module(
                "context_control_plane.provider_skill_effect_probe"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "provider Skill effect probe module is missing")
        return self.api

    def _receipt(self, samples=40):
        api = self._require_api()
        arguments = [
            "--samples-per-provider",
            str(samples),
            "--observed-at",
            OBSERVED_AT,
            "--output",
            "experiments/state/m4-05-provider-skill-effect-results.yaml",
        ]
        return api.run_provider_skill_effect_probe(
            self.root,
            samples_per_provider=samples,
            observed_at=OBSERVED_AT,
            arguments=arguments,
        )

    def test_probe_quantifies_two_provider_replay_and_effect_safety(self):
        api = self._require_api()
        receipt = self._receipt()

        api.validate_provider_skill_effect_probe_receipt(receipt, root=self.root)
        measurement = receipt["measurement"]
        self.assertEqual(measurement["samples_per_provider"], 40)
        self.assertEqual(measurement["total_effects"], 80)
        self.assertEqual(measurement["validated_effects"], 80)
        self.assertEqual(
            measurement["measured_operation"],
            "local_provider_delivery_plan_compose_and_validate",
        )
        self.assertEqual(
            receipt["workload"]["distinct_synthetic_compositions"], 40
        )
        self.assertEqual(
            receipt["workload"]["coverage_scope"],
            "byte-distinct-synthetic-compositions",
        )
        self.assertEqual(len(receipt["workload"]["scenario_ids"]), 40)
        self.assertEqual(len(set(receipt["workload"]["scenario_ids"])), 40)
        self.assertEqual(
            measurement["warmup_samples_per_scenario_provider"], 1
        )
        self.assertEqual(measurement["total_compose_calls"], 240)
        self.assertEqual(len(measurement["compose_call_order"]), 240)
        self.assertEqual(
            {
                phase: sum(
                    item["phase"] == phase
                    for item in measurement["compose_call_order"]
                )
                for phase in ("warmup", "measured", "replay")
            },
            {"warmup": 80, "measured": 80, "replay": 80},
        )
        self.assertEqual(measurement["unique_neutral_effect_digests"], 40)
        self.assertEqual(measurement["cross_provider_neutral_mismatches"], 0)
        self.assertEqual(measurement["provider_replay_mismatches"], 0)
        self.assertEqual(measurement["effects_declaring_state_write"], 0)
        self.assertEqual(measurement["composition_body_bytes_per_effect"], 5120)
        self.assertEqual(measurement["composition_body_bytes_total"], 409600)
        self.assertEqual(
            measurement["unique_effect_digests_by_provider"],
            {"codex": 40, "claude": 40},
        )
        self.assertTrue(
            all(
                len(measurement["effects_by_provider"][provider]) == 40
                for provider in ("codex", "claude")
            )
        )
        self.assertTrue(
            all(
                len(measurement["provider_payload_bytes_by_provider"][provider])
                == 40
                for provider in ("codex", "claude")
            )
        )
        self.assertTrue(
            all(value < 10.0 for value in measurement["adapter_p95_ms"].values())
        )
        self.assertTrue(all(receipt["acceptance"].values()))
        self.assertIn(
            "distinct_composition_gate_passed",
            receipt["acceptance"],
        )
        self.assertNotIn(
            "distinct_scenario_gate_passed",
            receipt["acceptance"],
        )
        self.assertFalse(
            receipt["authority_boundary"]["provider_process_invoked"]
        )
        self.assertFalse(
            receipt["authority_boundary"]["provider_input_tokens_measured"]
        )
        self.assertFalse(
            receipt["authority_boundary"]["state_port_calls_measured"]
        )
        self.assertTrue(
            receipt["authority_boundary"]["synthetic_plan_authorizer"]
        )
        self.assertTrue(receipt["authority_boundary"]["frozen_clock"])
        self.assertFalse(
            receipt["authority_boundary"][
                "state_revision_authorization_measured"
            ]
        )

    def test_validator_rejects_mismatch_or_authority_overstatement(self):
        api = self._require_api()
        receipt = self._receipt()

        for mutation in (
            lambda value: value["measurement"].__setitem__(
                "cross_provider_neutral_mismatches", 1
            ),
            lambda value: value["measurement"].__setitem__(
                "provider_replay_mismatches", 1
            ),
            lambda value: value["measurement"].__setitem__(
                "effects_declaring_state_write", 1
            ),
            lambda value: value["authority_boundary"].__setitem__(
                "provider_process_invoked", True
            ),
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(receipt)
                mutation(candidate)
                with self.assertRaises(ValueError):
                    api.validate_provider_skill_effect_probe_receipt(
                        candidate,
                        root=self.root,
                    )

    def test_validator_recomputes_latency_and_rejects_non_finite_values(self):
        api = self._require_api()
        receipt = self._receipt()

        changed_p95 = copy.deepcopy(receipt)
        changed_p95["measurement"]["adapter_p95_ms"]["codex"] += 1
        with self.assertRaisesRegex(ValueError, "latency"):
            api.validate_provider_skill_effect_probe_receipt(
                changed_p95,
                root=self.root,
            )

        non_finite = copy.deepcopy(receipt)
        non_finite["measurement"]["latency_samples_ms"]["claude"][0] = math.inf
        with self.assertRaisesRegex(ValueError, "latency"):
            api.validate_provider_skill_effect_probe_receipt(
                non_finite,
                root=self.root,
            )

    def test_acceptance_requires_exactly_forty_samples_per_provider(self):
        api = self._require_api()
        receipt = self._receipt(samples=1)

        with self.assertRaisesRegex(ValueError, "samples"):
            api.validate_provider_skill_effect_probe_receipt(
                receipt,
                root=self.root,
            )

    def test_runner_writes_a_current_receipt(self):
        api = self._require_api()
        with tempfile.TemporaryDirectory(prefix="m4-05-effect-probe-") as directory:
            output = Path(directory) / "result.yaml"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools" / "run_provider_skill_effect_probe.py"),
                    "--observed-at",
                    OBSERVED_AT,
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = yaml.safe_load(output.read_text(encoding="utf-8"))
            api.validate_provider_skill_effect_probe_receipt(
                receipt,
                root=self.root,
            )

    def test_versioned_receipt_has_current_provenance(self):
        api = self._require_api()
        receipt = yaml.safe_load(
            (
                self.root
                / "experiments/state/m4-05-provider-skill-effect-results.yaml"
            ).read_text(encoding="utf-8")
        )

        api.validate_provider_skill_effect_probe_receipt(receipt, root=self.root)

    def test_probe_receipt_schema_and_registry_are_current(self):
        receipt_path = (
            self.root
            / "experiments/state/m4-05-provider-skill-effect-results.yaml"
        )
        schema_path = (
            self.root / "schemas/m4-05/provider-skill-effect-probe.schema.json"
        )
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(receipt)), [])
        for mutation in (
            lambda value: value["measurement"]["effects_by_provider"]["codex"][
                0
            ].__setitem__("adapter_surface_contract", "bogus"),
            lambda value: value["measurement"]["effects_by_provider"]["codex"][
                0
            ].__setitem__("composition_sections", [{}]),
            lambda value: value["provenance"].__setitem__("unknown_sha256", "a" * 64),
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(receipt)
                mutation(candidate)
                self.assertNotEqual(
                    list(Draft202012Validator(schema).iter_errors(candidate)),
                    [],
                )
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.provider-skill-effect-probe"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        registry_entries = {
            item["schema_id"]: item
            for item in registry["schemas"]
            if item["schema_id"]
            in {
                "context.provider-skill-adapter-effect",
                "context.provider-skill-effect-probe",
            }
        }
        for schema_id, provenance_field in (
            (
                "context.provider-skill-adapter-effect",
                "adapter_effect_registry_entry_sha256",
            ),
            (
                "context.provider-skill-effect-probe",
                "probe_receipt_registry_entry_sha256",
            ),
        ):
            self.assertEqual(
                receipt["provenance"][provenance_field],
                hashlib.sha256(
                    json.dumps(
                        registry_entries[schema_id],
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest(),
            )

    def test_source_change_during_probe_is_rejected(self):
        api = self._require_api()
        expected = api._provenance(self.root)
        changed = dict(expected)
        changed["adapter_sha256"] = "a" * 64
        arguments = [
            "--samples-per-provider",
            "40",
            "--observed-at",
            OBSERVED_AT,
            "--output",
            "experiments/state/m4-05-provider-skill-effect-results.yaml",
        ]

        with patch.object(api, "_provenance", side_effect=[expected, changed]):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                api.run_provider_skill_effect_probe(
                    self.root,
                    samples_per_provider=40,
                    observed_at=OBSERVED_AT,
                    arguments=arguments,
                )


if __name__ == "__main__":
    unittest.main()
