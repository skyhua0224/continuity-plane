import hashlib
import importlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import yaml


MANIFEST_FIXTURE = "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
PACKET_FIXTURE = "experiments/skills/m4-02-compiled-skill-packet-v1alpha1.json"
SCHEMA_RELATIVE_PATH = "schemas/m4-03/skill-drift-assessment.schema.json"
ASSESSMENT_FIXTURE = "experiments/skills/m4-03-skill-drift-assessment-v1alpha1.json"


class M403SkillDriftQuarantineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.manifest_set = json.loads(
            (cls.root / MANIFEST_FIXTURE).read_text(encoding="utf-8")
        )
        cls.packet = json.loads(
            (cls.root / PACKET_FIXTURE).read_text(encoding="utf-8")
        )
        cls.schema_path = cls.root / SCHEMA_RELATIVE_PATH
        cls.assessment_fixture_path = cls.root / ASSESSMENT_FIXTURE
        try:
            cls.api = importlib.import_module(
                "context_control_plane.skill_drift_quarantine"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "skill_drift_quarantine module is missing")
        return self.api

    def _single_skill_case(self, content: bytes, *, expires_at=None):
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set = json.loads(json.dumps(self.manifest_set))
        manifest_set["manifests"] = [manifest_set["manifests"][0]]
        manifest_set["manifests"][0]["content_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        manifest_set["manifests"][0]["expires_at"] = expires_at
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.recovery"],
            observed_at="2026-08-11T08:00:00+08:00",
        )
        return manifest_set, packet

    def test_missing_selected_skill_path_is_quarantined(self):
        api = self._require_api()

        assessment = api.assess_skill_drift(
            self.packet,
            self.manifest_set,
            asset_resolver=lambda skill_id: None,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertEqual(
            assessment["skills"],
            [
                {
                    "skill_id": "core.recovery",
                    "status": "quarantined",
                    "reason_codes": ["missing-path"],
                    "observed_content_sha256": None,
                }
            ],
        )

    def test_changed_skill_content_digest_is_quarantined(self):
        api = self._require_api()
        assessment = api.assess_skill_drift(
            self.packet,
            self.manifest_set,
            asset_resolver=lambda skill_id: b"changed after packet compilation",
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertEqual(
            assessment["skills"][0]["reason_codes"],
            ["content-digest-mismatch"],
        )

    def test_current_manifest_version_drift_is_quarantined(self):
        api = self._require_api()
        content = b"locked Skill content"
        manifest_set, packet = self._single_skill_case(content)
        current_manifest_set = json.loads(json.dumps(manifest_set))
        current_manifest_set["manifests"][0]["version"] = "1.1.0"

        assessment = api.assess_skill_drift(
            packet,
            current_manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-version-mismatch",
            assessment["skills"][0]["reason_codes"],
        )

    def test_current_manifest_content_digest_drift_is_quarantined(self):
        api = self._require_api()
        content = b"manifest digest Skill content"
        manifest_set, packet = self._single_skill_case(content)
        current_manifest_set = json.loads(json.dumps(manifest_set))
        current_manifest_set["manifests"][0]["content_sha256"] = "f" * 64

        assessment = api.assess_skill_drift(
            packet,
            current_manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-digest-mismatch",
            assessment["skills"][0]["reason_codes"],
        )

    def test_manifest_is_quarantined_at_the_exact_expiry_boundary(self):
        api = self._require_api()
        content = b"expiring Skill content"
        manifest_set, packet = self._single_skill_case(
            content,
            expires_at="2026-08-11T09:00:00+08:00",
        )

        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T09:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-expired",
            assessment["skills"][0]["reason_codes"],
        )

    def test_matching_asset_and_manifest_are_verified(self):
        api = self._require_api()
        content = b"verified Skill content"
        manifest_set, packet = self._single_skill_case(content)

        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "allow")
        self.assertEqual(assessment["skills"][0]["status"], "verified")
        self.assertEqual(assessment["skills"][0]["reason_codes"], [])
        self.assertEqual(
            assessment["skills"][0]["observed_content_sha256"],
            hashlib.sha256(content).hexdigest(),
        )

    def test_unselected_manifest_change_quarantines_the_locked_packet(self):
        api = self._require_api()
        content = b"locked Skill content"
        manifest_set, packet = self._single_skill_case(content)
        current_manifest_set = json.loads(json.dumps(manifest_set))
        current_manifest_set["manifests"].append(
            json.loads(json.dumps(self.manifest_set["manifests"][1]))
        )

        assessment = api.assess_skill_drift(
            packet,
            current_manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-set-digest-mismatch",
            [finding["code"] for finding in assessment["findings"]],
        )

    def test_unselectable_source_status_is_quarantined(self):
        api = self._require_api()
        content = b"status-changed Skill content"
        manifest_set, packet = self._single_skill_case(content)
        current_manifest_set = json.loads(json.dumps(manifest_set))
        current_manifest_set["manifests"][0]["status"] = "quarantined"

        assessment = api.assess_skill_drift(
            packet,
            current_manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "source-status-unselectable",
            assessment["skills"][0]["reason_codes"],
        )

    def test_provider_asset_resolver_is_hashed_by_the_core(self):
        api = self._require_api()
        content = b"resolver content"
        manifest_set, packet = self._single_skill_case(content)

        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "allow")
        self.assertEqual(assessment["skills"][0]["status"], "verified")

    def test_assessment_receipt_is_canonical_and_replayable(self):
        api = self._require_api()
        content = b"canonical Skill content"
        manifest_set, packet = self._single_skill_case(content)
        resolver = lambda skill_id: content

        first = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=resolver,
            observed_at="2026-08-11T08:00:00+08:00",
        )
        second = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=resolver,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        receipt_bytes = api.canonical_skill_drift_assessment_bytes(first)
        self.assertEqual(receipt_bytes, api.canonical_skill_drift_assessment_bytes(second))
        self.assertEqual(
            api.skill_drift_assessment_digest(first),
            hashlib.sha256(receipt_bytes).hexdigest(),
        )

    def test_strict_json_schema_accepts_receipt_and_rejects_dynamic_state(self):
        api = self._require_api()
        self.assertTrue(self.schema_path.is_file())
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        content = b"schema-validated Skill content"
        manifest_set, packet = self._single_skill_case(content)
        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(list(Draft202012Validator(schema).iter_errors(assessment)), [])
        changed = json.loads(json.dumps(assessment))
        changed["active_task_id"] = "work.m4-03"
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(changed)),
            [],
        )

    def test_assessment_schema_is_registered_with_current_artifact_hash(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.skill-drift-assessment"
        )
        self.assertEqual(
            entry["current_wire_version"],
            "context.skill-drift-assessment/v1alpha1",
        )
        self.assertEqual(entry["artifact_path"], SCHEMA_RELATIVE_PATH)
        self.assertEqual(entry["compatibility_mode"], "strict-versioned")
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )

    def test_versioned_assessment_fixture_is_canonical(self):
        api = self._require_api()
        self.assertTrue(self.assessment_fixture_path.is_file())
        fixture_bytes = self.assessment_fixture_path.read_bytes()
        assessment = json.loads(fixture_bytes)
        api.validate_skill_drift_assessment(assessment)
        self.assertEqual(
            fixture_bytes,
            api.canonical_skill_drift_assessment_bytes(assessment) + b"\n",
        )

    def test_invalid_current_manifest_contract_is_quarantined(self):
        api = self._require_api()
        content = b"invalid manifest Skill content"
        manifest_set, packet = self._single_skill_case(content)
        current_manifest_set = json.loads(json.dumps(manifest_set))
        current_manifest_set["manifests"][0].pop("provenance_refs")

        assessment = api.assess_skill_drift(
            packet,
            current_manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-contract-invalid",
            [finding["code"] for finding in assessment["findings"]],
        )

    def test_schema_and_runtime_reject_untrusted_naive_observed_time(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        fixture = json.loads(self.assessment_fixture_path.read_text(encoding="utf-8"))
        fixture["observed_at"] = "2026-08-11T08:00:00"

        with self.assertRaises(ValueError):
            api.validate_skill_drift_assessment(fixture)
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(fixture)),
            [],
        )

    def test_schema_and_runtime_reject_an_inconsistent_allow_gate(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        fixture = json.loads(self.assessment_fixture_path.read_text(encoding="utf-8"))
        fixture["gate"] = "allow"

        with self.assertRaises(ValueError):
            api.validate_skill_drift_assessment(fixture)
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(fixture)),
            [],
        )

    def test_schema_and_runtime_reject_line_terminators_in_receipt_fields(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        base = json.loads(self.assessment_fixture_path.read_text(encoding="utf-8"))
        candidates = []

        packet_digest = json.loads(json.dumps(base))
        packet_digest["packet_sha256"] += "\n"
        candidates.append(("packet_sha256", packet_digest))

        skill_id = json.loads(json.dumps(base))
        skill_id["skills"][0]["skill_id"] += "\n"
        candidates.append(("skill_id", skill_id))

        reason_code = json.loads(json.dumps(base))
        reason_code["skills"][0]["reason_codes"][0] += "\n"
        candidates.append(("reason_code", reason_code))

        finding_detail = json.loads(json.dumps(base))
        finding_detail["findings"] = [
            {"scope": "asset", "code": "invalid", "detail": "detail\n"}
        ]
        candidates.append(("finding_detail", finding_detail))

        for field, candidate in candidates:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    api.validate_skill_drift_assessment(candidate)
                self.assertNotEqual(
                    list(Draft202012Validator(schema).iter_errors(candidate)),
                    [],
                )

    def test_schema_format_checker_and_runtime_reject_invalid_calendar_time(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        base = json.loads(self.assessment_fixture_path.read_text(encoding="utf-8"))
        invalid_times = (
            "2026-02-30T08:00:00+08:00",
            "2025-02-29T08:00:00+08:00",
            "0000-01-01T00:00:00Z",
        )

        for observed_at in invalid_times:
            with self.subTest(observed_at=observed_at):
                fixture = json.loads(json.dumps(base))
                fixture["observed_at"] = observed_at
                with self.assertRaises(ValueError):
                    api.validate_skill_drift_assessment(fixture)
                self.assertNotEqual(
                    list(
                        Draft202012Validator(
                            schema,
                            format_checker=FormatChecker(),
                        ).iter_errors(fixture)
                    ),
                    [],
                )

        valid_leap_day = json.loads(json.dumps(base))
        valid_leap_day["observed_at"] = "2024-02-29T08:00:00+08:00"
        api.validate_skill_drift_assessment(valid_leap_day)
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(valid_leap_day)),
            [],
        )

    def test_duplicate_skill_ids_are_runtime_semantic_admission_errors(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        candidate = json.loads(self.assessment_fixture_path.read_text(encoding="utf-8"))
        candidate["skills"].append(
            {
                "skill_id": candidate["skills"][0]["skill_id"],
                "status": "verified",
                "reason_codes": [],
                "observed_content_sha256": "a" * 64,
            }
        )

        with self.assertRaisesRegex(ValueError, "unique"):
            api.validate_skill_drift_assessment(candidate)
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(candidate)),
            [],
        )

    def test_malformed_compiled_packet_fails_before_asset_resolution(self):
        api = self._require_api()
        broken = json.loads(json.dumps(self.packet))
        broken.pop("rule_bindings")
        resolver_calls = []

        with self.assertRaisesRegex(api.SkillDriftInputError, "compiled packet"):
            api.assess_skill_drift(
                broken,
                self.manifest_set,
                asset_resolver=lambda skill_id: resolver_calls.append(skill_id),
                observed_at="2026-08-11T08:00:00+08:00",
            )

        self.assertEqual(resolver_calls, [])

    def test_resolver_cannot_mutate_the_packet_identity_used_for_assessment(self):
        api = self._require_api()
        from context_control_plane.compiled_skill_packet import (
            compiled_skill_packet_digest,
        )

        content = b"locked packet content"
        changed_content = b"resolver replacement content"
        manifest_set, packet = self._single_skill_case(content)
        locked_packet_digest = compiled_skill_packet_digest(packet)

        def mutating_resolver(skill_id):
            packet["selections"][0]["content_sha256"] = hashlib.sha256(
                changed_content
            ).hexdigest()
            return changed_content

        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=mutating_resolver,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["packet_sha256"], locked_packet_digest)
        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "content-digest-mismatch",
            assessment["skills"][0]["reason_codes"],
        )

    def test_allow_receipt_requires_manifest_and_observed_content_digests(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        content = b"allow receipt evidence"
        manifest_set, packet = self._single_skill_case(content)
        assessment = api.assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: content,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        missing_manifest_digest = json.loads(json.dumps(assessment))
        missing_manifest_digest["manifest_set_sha256"] = None
        missing_observed_digest = json.loads(json.dumps(assessment))
        missing_observed_digest["skills"][0]["observed_content_sha256"] = None

        for candidate in (missing_manifest_digest, missing_observed_digest):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    api.validate_skill_drift_assessment(candidate)
                self.assertNotEqual(
                    list(Draft202012Validator(schema).iter_errors(candidate)),
                    [],
                )

    def test_invalid_rfc3339_times_are_rejected_before_asset_resolution(self):
        api = self._require_api()
        resolver_calls = []
        invalid_times = (
            "2026-02-30T08:00:00+08:00",
            "2025-02-29T08:00:00+08:00",
            "0000-01-01T00:00:00Z",
            "2026-08-11T24:00:00Z",
            "2026-08-11T08:00:00+01:60",
            "2026-08-11T08:00:00+00:99",
            "2026-08-11T08:00:00-01:60",
            "2026-08-11T08:00:00+22:99",
        )

        for observed_at in invalid_times:
            with self.subTest(observed_at=observed_at):
                with self.assertRaisesRegex(api.SkillDriftInputError, "observed_at"):
                    api.assess_skill_drift(
                        self.packet,
                        self.manifest_set,
                        asset_resolver=lambda skill_id: resolver_calls.append(skill_id),
                        observed_at=observed_at,
                    )

        self.assertEqual(resolver_calls, [])

    def test_manifest_field_failures_are_denied_without_raw_key_errors(self):
        api = self._require_api()
        missing_manifests = json.loads(json.dumps(self.manifest_set))
        missing_manifests.pop("manifests")
        with self.assertRaisesRegex(api.SkillDriftInputError, "manifest set"):
            api.assess_skill_drift(
                self.packet,
                missing_manifests,
                asset_resolver=lambda skill_id: b"unexpected",
                observed_at="2026-08-11T08:00:00+08:00",
            )

        missing_version = json.loads(json.dumps(self.manifest_set))
        missing_version["manifests"][0].pop("version")
        assessment = api.assess_skill_drift(
            self.packet,
            missing_version,
            asset_resolver=lambda skill_id: b"unexpected",
            observed_at="2026-08-11T08:00:00+08:00",
        )
        self.assertEqual(assessment["gate"], "quarantine")

        invalid_expiry = json.loads(json.dumps(self.manifest_set))
        invalid_expiry["manifests"][0]["expires_at"] = "2026-02-30T08:00:00+08:00"
        assessment = api.assess_skill_drift(
            self.packet,
            invalid_expiry,
            asset_resolver=lambda skill_id: b"unexpected",
            observed_at="2026-08-11T08:00:00+08:00",
        )
        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "manifest-contract-invalid",
            [finding["code"] for finding in assessment["findings"]],
        )

    def test_unexpected_asset_resolver_failure_becomes_quarantine(self):
        api = self._require_api()

        def broken_resolver(skill_id):
            raise RuntimeError("adapter implementation detail")

        assessment = api.assess_skill_drift(
            self.packet,
            self.manifest_set,
            asset_resolver=broken_resolver,
            observed_at="2026-08-11T08:00:00+08:00",
        )

        self.assertEqual(assessment["gate"], "quarantine")
        self.assertIn(
            "asset-unreadable-or-unsafe",
            assessment["skills"][0]["reason_codes"],
        )

    def test_core_rejects_direct_paths_before_any_file_read(self):
        api = self._require_api()

        with self.assertRaisesRegex(api.SkillDriftInputError, "asset_resolver"):
            api.assess_skill_drift(
                self.packet,
                self.manifest_set,
                skill_paths={"core.recovery": "/untrusted/SKILL.md"},
                observed_at="2026-08-11T08:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
