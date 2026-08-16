"""M8-01 fixed DeepSeek and Pi durability oracle fixtures."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml


class M801ProviderOracleFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).parents[1]
        fixture_path = cls.root / "experiments/fixtures/m8-01-provider-oracles.json"
        cls.payload = fixture_path.read_bytes()
        cls.fixture = json.loads(cls.payload)
        cls.catalog = yaml.safe_load(
            (cls.root / "profiles/reference-catalog.example.yaml").read_text(
                encoding="utf-8"
            )
        )

    def test_fixture_is_content_addressed_and_bound_to_fixed_sources(self) -> None:
        self.assertEqual(
            self.fixture["schema_version"],
            "context.durable-provider-oracle-fixture/v1alpha1",
        )
        body = dict(self.fixture)
        fixture_sha256 = body.pop("fixture_sha256")
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.assertEqual(fixture_sha256, hashlib.sha256(canonical).hexdigest())

        catalog = {item["reference_id"]: item for item in self.catalog["entries"]}
        for oracle in self.fixture["oracles"]:
            with self.subTest(oracle=oracle["oracle_id"]):
                source = catalog[oracle["reference_id"]]
                self.assertEqual(oracle["source_revision"], source["source_revision"])
                self.assertEqual(oracle["source_tree"], source["source_tree"])
                self.assertFalse(oracle["state_write_authority"])
                self.assertFalse(oracle["provider_native_authority"])

    def test_deepseek_unknown_outcome_requires_external_verification(self) -> None:
        oracle = next(
            item
            for item in self.fixture["oracles"]
            if item["oracle_id"] == "deepseek-tool-outcome-unknown"
        )
        self.assertEqual(oracle["observed_events"], ["tool/call"])
        self.assertEqual(oracle["missing_events"], ["tool/result"])
        self.assertEqual(oracle["recovery_signal"], "TOOL_OUTCOME_UNKNOWN")
        self.assertEqual(oracle["required_action"], "verify-external-settlement")
        self.assertFalse(oracle["blind_replay_allowed"])
        self.assertEqual(oracle["implementation_status"], "runnable-fixture")

    def test_pi_effect_sandwich_is_an_oracle_not_a_runnable_dependency(self) -> None:
        oracle = next(
            item
            for item in self.fixture["oracles"]
            if item["oracle_id"] == "pi-effect-sandwich"
        )
        self.assertEqual(
            oracle["program_counter"],
            ["prepared", "intent-committed", "effect-in-flight", "effect-settled"],
        )
        self.assertEqual(oracle["replay_policy"], {"never": "verify-only", "safe": "same-key"})
        self.assertEqual(oracle["implementation_status"], "scaffold")
        self.assertFalse(oracle["runnable_dependency"])


if __name__ == "__main__":
    unittest.main()
