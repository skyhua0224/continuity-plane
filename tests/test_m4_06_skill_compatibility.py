import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

OBSERVED_AT = "2026-08-11T16:00:00+08:00"


class M406SkillCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.fixture = json.loads(
            (cls.root / "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json")
            .read_text(encoding="utf-8")
        )
        try:
            cls.api = importlib.import_module(
                "context_control_plane.skill_compatibility"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "Skill compatibility module is missing")
        return self.api

    def _case(self):
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        template = self.fixture["manifests"][0]
        manifests = []
        for skill_id, content, rules in (
            ("core.bootstrap", b"bootstrap", ["core.bootstrap.rule"]),
            ("project.active", b"active", ["project.active.rule"]),
            ("unused.metadata", b"unused", ["unused.metadata.rule"]),
        ):
            manifest = copy.deepcopy(template)
            manifest["skill_id"] = skill_id
            manifest["version"] = "1.0.0"
            manifest["content_sha256"] = hashlib.sha256(content).hexdigest()
            manifest["rule_ids"] = rules
            manifest["dependencies"] = []
            manifest["conflicts"] = []
            manifest["expires_at"] = None
            if skill_id != "unused.metadata":
                manifest["applicability"] = [
                    {"kind": "provider", "ref": "provider://claude"},
                    {"kind": "provider", "ref": "provider://codex"},
                ]
                manifest["compatibility"]["provider_contract_refs"] = [
                    "provider://claude/v1",
                    "provider://codex/v1",
                ]
            manifests.append(manifest)
        manifest_set = {
            "schema_version": self.fixture["schema_version"],
            "manifests": manifests,
        }
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )
        return manifest_set, packet

    def _lock(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        return api.create_skill_compatibility_lock(
            manifest_set,
            packet,
            task_id="task/demo",
            operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )

    @staticmethod
    def _evidence_verifier(reference, *, purpose, migration):
        expected = {
            "approval": "artifact://sha256/" + "a" * 64,
            "replay": "artifact://sha256/" + "b" * 64,
            "rollback": "artifact://sha256/" + "c" * 64,
        }
        return (
            reference == expected[purpose]
            and migration["task_id"] == "task/demo"
        )

    def test_lock_canonical_round_trip_is_stable(self):
        api = self._require_api()
        lock = self._lock()
        encoded = api.canonical_skill_compatibility_lock_bytes(lock)
        self.assertEqual(json.loads(encoded), lock)
        self.assertEqual(api.skill_compatibility_lock_digest(lock), hashlib.sha256(encoded).hexdigest())
        reversed_lock = json.loads(json.dumps(lock))
        reversed_lock["selected_rule_ids"] = list(reversed(reversed_lock["selected_rule_ids"]))
        reversed_lock["skills"] = list(reversed(reversed_lock["skills"]))
        self.assertEqual(encoded, api.canonical_skill_compatibility_lock_bytes(reversed_lock))

    def test_unchanged_candidate_is_accepted(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        decision = api.assess_skill_compatibility(
            self._lock(), manifest_set, packet,
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        self.assertEqual(decision["classification"], "unchanged")
        self.assertTrue(api.authorize_skill_delivery(decision))

    def test_unselected_metadata_change_is_compatible(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        candidate = copy.deepcopy(manifest_set)
        candidate["manifests"][2]["provenance_refs"] = [
            "artifact://sha256/" + "d" * 64
        ]
        decision = api.assess_skill_compatibility(
            self._lock(), candidate, packet,
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        self.assertEqual(decision["classification"], "compatible")
        self.assertTrue(api.authorize_skill_delivery(decision))

    def test_selected_identity_or_rule_change_requires_migration(self):
        api = self._require_api()
        original_manifest_set, packet = self._case()
        for change in ("version", "content", "rule"):
            with self.subTest(change=change):
                candidate = copy.deepcopy(original_manifest_set)
                manifest = candidate["manifests"][1]
                if change == "version":
                    manifest["version"] = "1.1.0"
                elif change == "content":
                    manifest["content_sha256"] = "e" * 64
                else:
                    manifest["rule_ids"] = ["project.active.changed"]
                decision = api.assess_skill_compatibility(
                    self._lock(), candidate, packet,
                    provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
                )
                self.assertEqual(decision["classification"], "migration_required")
                self.assertFalse(api.authorize_skill_delivery(decision))

    def test_selected_governance_metadata_change_is_not_compatible(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        for change, expected in (
            ("provenance", "migration_required"),
            ("schema-compatibility", "migration_required"),
            ("quarantine", "rejected"),
        ):
            with self.subTest(change=change):
                candidate = copy.deepcopy(manifest_set)
                selected = candidate["manifests"][1]
                if change == "provenance":
                    selected["provenance_refs"] = [
                        "artifact://sha256/" + "e" * 64
                    ]
                elif change == "schema-compatibility":
                    selected["compatibility"]["schema_refs"].append(
                        "context.typed-state/v1alpha1"
                    )
                else:
                    selected["status"] = "quarantined"
                decision = api.assess_skill_compatibility(
                    self._lock(),
                    candidate,
                    packet,
                    provider_contract_refs=[
                        "provider://codex/v1",
                        "provider://claude/v1",
                    ],
                )
                self.assertEqual(decision["classification"], expected)
                self.assertFalse(api.authorize_skill_delivery(decision))

    def test_lock_rejects_provider_not_supported_by_every_selected_skill(self):
        api = self._require_api()
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set, _ = self._case()
        for manifest in manifest_set["manifests"][:2]:
            manifest["applicability"] = [
                {"kind": "provider", "ref": "provider://codex"}
            ]
            manifest["compatibility"]["provider_contract_refs"] = [
                "provider://codex/v1"
            ]
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )

        with self.assertRaisesRegex(api.SkillCompatibilityError, "provider"):
            api.create_skill_compatibility_lock(
                manifest_set,
                packet,
                task_id="task/demo",
                operation_id="operation://code-change",
                provider_contract_refs=["provider://claude/v1"],
            )

    def test_provider_contract_change_is_fail_closed(self):
        api = self._require_api()
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set, _ = self._case()
        for manifest in manifest_set["manifests"][:2]:
            manifest["compatibility"]["provider_contract_refs"] = [
                "provider://claude/v1",
                "provider://codex/v2",
            ]
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )
        decision = api.assess_skill_compatibility(
            self._lock(), manifest_set, packet,
            provider_contract_refs=["provider://codex/v2", "provider://claude/v1"],
        )
        self.assertEqual(decision["classification"], "migration_required")
        self.assertFalse(api.authorize_skill_delivery(decision))

    def test_compatible_decision_rejects_breaking_reason_code(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        changed = copy.deepcopy(manifest_set)
        changed["manifests"][2]["provenance_refs"] = [
            "artifact://sha256/" + "d" * 64
        ]
        decision = api.assess_skill_compatibility(
            self._lock(),
            changed,
            packet,
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        decision["reason_codes"] = ["provider-contract-changed"]

        with self.assertRaisesRegex(api.SkillCompatibilityError, "reason"):
            api.authorize_skill_delivery(decision)

    def test_corrupt_or_invalid_candidate_is_rejected(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        lock = self._lock()
        for candidate_lock in (
            {**lock, "packet_sha256": "x" * 64},
            {key: value for key, value in lock.items() if key != "selected_rule_ids"},
        ):
            with self.subTest(candidate=candidate_lock), self.assertRaises(ValueError):
                api.validate_skill_compatibility_lock(candidate_lock)
        invalid_packet = copy.deepcopy(packet)
        invalid_packet["selections"][0]["content_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            api.assess_skill_compatibility(
                lock, manifest_set, invalid_packet,
                provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
            )

    def test_explicit_migration_requires_all_evidence(self):
        api = self._require_api()
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set, _ = self._case()
        changed = copy.deepcopy(manifest_set)
        changed["manifests"][1]["version"] = "1.1.0"
        changed_packet = compile_skill_packet(
            changed,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )
        old_lock = self._lock()
        new_lock = api.create_skill_compatibility_lock(
            changed, changed_packet, task_id="task/demo", operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        decision = api.assess_skill_compatibility(
            old_lock, changed, changed_packet,
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        migration = api.create_skill_compatibility_migration(
            old_lock, new_lock, migration_id="migration/demo-1", reason="approved upgrade",
            approval_ref="artifact://sha256/" + "a" * 64,
            replay_proof_ref="artifact://sha256/" + "b" * 64,
            rollback_proof_ref="artifact://sha256/" + "c" * 64,
        )
        self.assertFalse(api.authorize_skill_delivery(decision, migration=migration))
        self.assertFalse(
            api.authorize_skill_delivery(
                decision,
                migration=migration,
                evidence_verifier=lambda *_args, **_kwargs: False,
            )
        )
        self.assertTrue(
            api.authorize_skill_delivery(
                decision,
                migration=migration,
                evidence_verifier=self._evidence_verifier,
            )
        )
        for field in ("approval_ref", "replay_proof_ref", "rollback_proof_ref"):
            kwargs = {
                "old_lock": old_lock, "new_lock": new_lock, "migration_id": "migration/demo-1",
                "reason": "approved upgrade", "approval_ref": "artifact://sha256/" + "a" * 64,
                "replay_proof_ref": "artifact://sha256/" + "b" * 64,
                "rollback_proof_ref": "artifact://sha256/" + "c" * 64,
            }
            kwargs[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                api.create_skill_compatibility_migration(**kwargs)

    def test_migration_is_idempotent_and_rollback_restores_original_lock(self):
        api = self._require_api()
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set, _ = self._case()
        changed = copy.deepcopy(manifest_set)
        changed["manifests"][1]["version"] = "1.1.0"
        changed_packet = compile_skill_packet(
            changed,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )
        old_lock = self._lock()
        new_lock = api.create_skill_compatibility_lock(
            changed, changed_packet, task_id="task/demo", operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        kwargs = {
            "old_lock": old_lock,
            "new_lock": new_lock,
            "migration_id": "migration/demo-2",
            "reason": "approved upgrade",
            "approval_ref": "artifact://sha256/" + "a" * 64,
            "replay_proof_ref": "artifact://sha256/" + "b" * 64,
            "rollback_proof_ref": "artifact://sha256/" + "c" * 64,
        }
        first = api.create_skill_compatibility_migration(**kwargs)
        second = api.create_skill_compatibility_migration(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(api.rollback_skill_compatibility_migration(first), old_lock)

    def test_schema_and_registry_are_current(self):
        api = self._require_api()
        lock = self._lock()
        api.validate_skill_compatibility_lock(lock)
        from context_control_plane.compiled_skill_packet import compile_skill_packet

        manifest_set, packet = self._case()
        decision = api.assess_skill_compatibility(
            lock, manifest_set, packet,
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        changed = copy.deepcopy(manifest_set)
        changed["manifests"][1]["version"] = "1.1.0"
        changed_packet = compile_skill_packet(
            changed,
            selected_skill_ids=["core.bootstrap", "project.active"],
            observed_at=OBSERVED_AT,
        )
        new_lock = api.create_skill_compatibility_lock(
            changed, changed_packet, task_id="task/demo", operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
        )
        migration = api.create_skill_compatibility_migration(
            lock, new_lock, migration_id="migration/schema", reason="schema test",
            approval_ref="artifact://sha256/" + "a" * 64,
            replay_proof_ref="artifact://sha256/" + "b" * 64,
            rollback_proof_ref="artifact://sha256/" + "c" * 64,
        )
        documents = (
            ("skill-compatibility-lock.schema.json", lock),
            ("skill-compatibility-decision.schema.json", decision),
            ("skill-compatibility-migration.schema.json", migration),
        )
        for filename, document in documents:
            with self.subTest(filename=filename):
                schema = json.loads(
                    (self.root / "schemas/m4-06" / filename).read_text(encoding="utf-8")
                )
                self.assertEqual(list(Draft202012Validator(schema).iter_errors(document)), [])
        invalid_decision = copy.deepcopy(decision)
        invalid_decision["classification"] = "migration_required"
        invalid_decision["delivery_allowed_without_migration"] = True
        invalid_decision["reason_codes"] = []
        decision_schema = json.loads(
            (
                self.root
                / "schemas/m4-06/skill-compatibility-decision.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            list(Draft202012Validator(decision_schema).iter_errors(invalid_decision)),
            [],
        )
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        for schema_id, filename in (
            ("context.skill-compatibility-lock", "skill-compatibility-lock.schema.json"),
            ("context.skill-compatibility-decision", "skill-compatibility-decision.schema.json"),
            ("context.skill-compatibility-migration", "skill-compatibility-migration.schema.json"),
        ):
            entry = next(item for item in registry["schemas"] if item["schema_id"] == schema_id)
            self.assertEqual(entry["content_sha256"], hashlib.sha256(
                (self.root / "schemas/m4-06" / filename).read_bytes()
            ).hexdigest())

    def test_fault_matrix_has_zero_unauthorized_delivery(self):
        api = self._require_api()
        manifest_set, packet = self._case()
        lock = self._lock()
        unauthorized = 0
        samples = []
        for index in range(40):
            candidate = copy.deepcopy(manifest_set)
            if index % 4 == 0:
                candidate["manifests"][2]["provenance_refs"] = ["artifact://sha256/" + f"{index + 1:064x}"]
            elif index % 4 == 1:
                candidate["manifests"][1]["version"] = "1.1.0"
            elif index % 4 == 2:
                candidate["manifests"][1]["content_sha256"] = f"{index + 1:064x}"
            else:
                candidate["manifests"][1]["rule_ids"] = [f"changed.rule.{index}"]
            decision = api.assess_skill_compatibility(
                lock, candidate, packet,
                provider_contract_refs=["provider://codex/v1", "provider://claude/v1"],
            )
            allowed = api.authorize_skill_delivery(decision)
            unauthorized += int(decision["classification"] == "migration_required" and allowed)
            samples.append(decision["classification"])
        self.assertEqual(len(samples), 40)
        self.assertEqual(unauthorized, 0)
        self.assertEqual(samples.count("compatible"), 10)
        self.assertEqual(samples.count("migration_required"), 30)


if __name__ == "__main__":
    unittest.main()
