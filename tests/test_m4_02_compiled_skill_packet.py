import hashlib
import importlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
import yaml


MANIFEST_FIXTURE = "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
SCHEMA_RELATIVE_PATH = "schemas/m4-02/compiled-skill-packet.schema.json"
PACKET_FIXTURE = "experiments/skills/m4-02-compiled-skill-packet-v1alpha1.json"
PACKET_WIRE_VERSION = "context.compiled-skill-packet/v1alpha1"
COMPILER_VERSION = "context.skill-packet-compiler/v1alpha1"


class M402CompiledSkillPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.manifest_set = json.loads(
            (cls.root / MANIFEST_FIXTURE).read_text(encoding="utf-8")
        )
        cls.schema_path = cls.root / SCHEMA_RELATIVE_PATH
        cls.packet_fixture_path = cls.root / PACKET_FIXTURE
        try:
            cls.api = importlib.import_module(
                "context_control_plane.compiled_skill_packet"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "compiled_skill_packet module is missing")
        return self.api

    def test_compile_expands_dependencies_into_stable_rule_bindings(self):
        api = self._require_api()

        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )

        self.assertEqual(packet["schema_version"], PACKET_WIRE_VERSION)
        self.assertEqual(packet["compiler_version"], COMPILER_VERSION)
        self.assertEqual(
            packet["manifest_set_sha256"],
            hashlib.sha256(
                api.skill_manifest_set.canonical_skill_manifest_set_bytes(
                    self.manifest_set,
                    observed_at="2026-08-10T08:00:00+08:00",
                )
            ).hexdigest(),
        )
        self.assertEqual(
            [selection["skill_id"] for selection in packet["selections"]],
            ["core.recovery", "external.audit", "project.testing"],
        )
        self.assertEqual(
            [binding["rule_id"] for binding in packet["rule_bindings"]],
            [
                "rule.core.recovery.canary",
                "rule.core.recovery.checkpoint",
                "rule.external.audit.provenance",
                "rule.external.audit.scope",
                "rule.project.testing.red-green",
                "rule.project.testing.verify",
            ],
        )
        self.assertEqual(
            packet["rule_bindings"][-1],
            {
                "rule_id": "rule.project.testing.verify",
                "skill_id": "project.testing",
                "skill_version": "1.0.0",
                "skill_content_sha256": (
                    "c9ffd46e9e9efc085a78b1e485917a1380b6ef9081787ad0fc112c2ecaa7ddcd"
                ),
            },
        )

    def test_packet_rejects_dynamic_state_at_every_contract_level(self):
        api = self._require_api()
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )

        api.validate_compiled_skill_packet(packet)

        for field, value in (
            ("active_task_id", "work.m4-02"),
            ("owner", "agent.example"),
            ("claim_id", "claim.example"),
            ("revision", 29),
            ("checkpoint_id", "checkpoint.example"),
            ("current_evidence", ["artifact://sha256/" + "d" * 64]),
            ("effects", ["effect.example"]),
        ):
            with self.subTest(field=field):
                candidate = json.loads(json.dumps(packet))
                candidate[field] = value
                with self.assertRaisesRegex(
                    api.CompiledSkillPacketError,
                    "unknown fields",
                ):
                    api.validate_compiled_skill_packet(candidate)

        candidate = json.loads(json.dumps(packet))
        candidate["selections"][0]["owner"] = "agent.example"
        with self.assertRaisesRegex(api.CompiledSkillPacketError, "unknown fields"):
            api.validate_compiled_skill_packet(candidate)

        candidate = json.loads(json.dumps(packet))
        candidate["rule_bindings"][0]["checkpoint_id"] = "checkpoint.example"
        with self.assertRaisesRegex(api.CompiledSkillPacketError, "unknown fields"):
            api.validate_compiled_skill_packet(candidate)

    def test_packet_rejects_binding_that_does_not_match_selected_skill_version(self):
        api = self._require_api()
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        candidate = json.loads(json.dumps(packet))
        candidate["rule_bindings"][-1]["skill_version"] = "9.9.9"

        with self.assertRaisesRegex(
            api.CompiledSkillPacketError,
            "must match its selected Skill",
        ):
            api.validate_compiled_skill_packet(candidate)

    def test_packet_rejects_a_selection_with_an_unbound_rule(self):
        api = self._require_api()
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        candidate = json.loads(json.dumps(packet))
        candidate["rule_bindings"].pop()

        with self.assertRaisesRegex(
            api.CompiledSkillPacketError,
            "must bind every selected rule",
        ):
            api.validate_compiled_skill_packet(candidate)

    def test_packet_rejects_rule_id_declared_by_multiple_selected_skills(self):
        api = self._require_api()
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        candidate = json.loads(json.dumps(packet))
        candidate["selections"][0]["rule_ids"].append(
            "rule.project.testing.verify"
        )

        with self.assertRaisesRegex(
            api.CompiledSkillPacketError,
            "globally unique",
        ):
            api.validate_compiled_skill_packet(candidate)

    def test_compiled_packet_has_order_independent_canonical_bytes_and_digest(self):
        api = self._require_api()
        first = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["external.audit", "project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        second = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing", "external.audit"],
            observed_at="2026-08-10T08:00:00+08:00",
        )

        first_bytes = api.canonical_compiled_skill_packet_bytes(first)
        self.assertEqual(first_bytes, api.canonical_compiled_skill_packet_bytes(second))
        self.assertEqual(first, json.loads(first_bytes))
        self.assertEqual(
            api.compiled_skill_packet_digest(first),
            hashlib.sha256(first_bytes).hexdigest(),
        )

    def test_strict_json_schema_accepts_packet_and_rejects_dynamic_state(self):
        api = self._require_api()
        self.assertTrue(self.schema_path.is_file())
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"],
            "https://context-control-plane.invalid/schemas/m4-02/compiled-skill-packet.schema.json",
        )
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )

        self.assertEqual(list(Draft202012Validator(schema).iter_errors(packet)), [])
        candidate = json.loads(json.dumps(packet))
        candidate["active_task_id"] = "work.m4-02"
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(candidate)),
            [],
        )

    def test_packet_verification_rejects_a_changed_source_manifest_set(self):
        api = self._require_api()
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        api.verify_compiled_skill_packet(
            packet,
            self.manifest_set,
            observed_at="2026-08-10T08:00:00+08:00",
        )

        changed_manifest_set = json.loads(json.dumps(self.manifest_set))
        changed_manifest_set["manifests"][0]["content_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            api.CompiledSkillPacketError,
            "manifest_set_sha256 does not match",
        ):
            api.verify_compiled_skill_packet(
                packet,
                changed_manifest_set,
                observed_at="2026-08-10T08:00:00+08:00",
            )

    def test_packet_verification_rejects_a_quarantined_source_manifest(self):
        api = self._require_api()
        from context_control_plane.skill_manifest_set import (
            canonical_skill_manifest_set_bytes,
        )

        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        quarantined = json.loads(json.dumps(self.manifest_set))
        quarantined["manifests"][2]["status"] = "quarantined"
        candidate = json.loads(json.dumps(packet))
        candidate["manifest_set_sha256"] = hashlib.sha256(
            canonical_skill_manifest_set_bytes(
                quarantined,
                observed_at="2026-08-10T08:00:00+08:00",
            )
        ).hexdigest()

        with self.assertRaisesRegex(
            api.CompiledSkillPacketError,
            "source Skill status",
        ):
            api.verify_compiled_skill_packet(
                candidate,
                quarantined,
                observed_at="2026-08-10T08:00:00+08:00",
            )

    def test_packet_uses_the_manifest_set_stable_id_grammar(self):
        api = self._require_api()
        packet = {
            "schema_version": PACKET_WIRE_VERSION,
            "compiler_version": COMPILER_VERSION,
            "manifest_set_sha256": "a" * 64,
            "selections": [
                {
                    "skill_id": "foo/bar",
                    "version": "1.0.0-0",
                    "content_sha256": "b" * 64,
                    "rule_ids": ["rule/foo"],
                }
            ],
            "rule_bindings": [
                {
                    "rule_id": "rule/foo",
                    "skill_id": "foo/bar",
                    "skill_version": "1.0.0-0",
                    "skill_content_sha256": "b" * 64,
                }
            ],
        }

        api.validate_compiled_skill_packet(packet)

    def test_compiler_accepts_an_active_manifest_as_selectable(self):
        api = self._require_api()
        active = json.loads(json.dumps(self.manifest_set))
        active["manifests"][0]["status"] = "active"

        packet = api.compile_skill_packet(
            active,
            selected_skill_ids=["core.recovery"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        api.verify_compiled_skill_packet(
            packet,
            active,
            observed_at="2026-08-10T08:00:00+08:00",
        )

    def test_schema_and_runtime_reject_strict_semver_and_duplicate_top_level_items(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )

        malformed_version = json.loads(json.dumps(packet))
        malformed_version["selections"][0]["version"] = "1.0.0-01"
        malformed_version["rule_bindings"][-1]["skill_version"] = "1.0.0-01"
        with self.assertRaises(api.CompiledSkillPacketError):
            api.validate_compiled_skill_packet(malformed_version)
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(malformed_version)),
            [],
        )

        duplicate = json.loads(json.dumps(packet))
        duplicate["selections"].append(json.loads(json.dumps(packet["selections"][0])))
        duplicate["rule_bindings"].append(
            json.loads(json.dumps(packet["rule_bindings"][0]))
        )
        with self.assertRaises(api.CompiledSkillPacketError):
            api.validate_compiled_skill_packet(duplicate)
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(duplicate)),
            [],
        )

    def test_schema_and_runtime_reject_overlong_semver(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["project.testing"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        candidate = json.loads(json.dumps(packet))
        overlong = "1.0.0-" + "a" * 5000
        for selection in candidate["selections"]:
            selection["version"] = overlong
        for binding in candidate["rule_bindings"]:
            binding["skill_version"] = overlong

        with self.assertRaises(api.CompiledSkillPacketError):
            api.validate_compiled_skill_packet(candidate)
        self.assertNotEqual(
            list(Draft202012Validator(schema).iter_errors(candidate)),
            [],
        )

    def test_schema_and_runtime_reject_trailing_line_terminators(self):
        api = self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["core.recovery"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        candidates = []

        changed = json.loads(json.dumps(packet))
        changed["manifest_set_sha256"] += "\n"
        candidates.append(changed)

        changed = json.loads(json.dumps(packet))
        changed["selections"][0]["skill_id"] += "\n"
        changed["rule_bindings"][0]["skill_id"] += "\n"
        candidates.append(changed)

        changed = json.loads(json.dumps(packet))
        changed["selections"][0]["version"] += "\n"
        for binding in changed["rule_bindings"]:
            binding["skill_version"] += "\n"
        candidates.append(changed)

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(api.CompiledSkillPacketError):
                    api.validate_compiled_skill_packet(candidate)
                self.assertNotEqual(
                    list(Draft202012Validator(schema).iter_errors(candidate)),
                    [],
                )

    def test_versioned_fixture_is_canonical_and_bound_to_manifest_set(self):
        api = self._require_api()
        self.assertTrue(self.packet_fixture_path.is_file())
        fixture_bytes = self.packet_fixture_path.read_bytes()
        packet = json.loads(fixture_bytes)

        api.verify_compiled_skill_packet(
            packet,
            self.manifest_set,
            observed_at="2026-08-10T08:00:00+08:00",
        )
        self.assertEqual(
            fixture_bytes,
            api.canonical_compiled_skill_packet_bytes(packet) + b"\n",
        )

    def test_selected_packet_reduces_static_manifest_input_by_at_least_sixty_percent(self):
        api = self._require_api()
        from context_control_plane.skill_manifest_set import (
            canonical_skill_manifest_set_bytes,
        )

        source_bytes = canonical_skill_manifest_set_bytes(
            self.manifest_set,
            observed_at="2026-08-10T08:00:00+08:00",
        )
        packet = api.compile_skill_packet(
            self.manifest_set,
            selected_skill_ids=["core.recovery"],
            observed_at="2026-08-10T08:00:00+08:00",
        )
        packet_bytes = api.canonical_compiled_skill_packet_bytes(packet)
        reduction_percent = round(
            (1 - len(packet_bytes) / len(source_bytes)) * 100,
            4,
        )

        self.assertEqual(len(source_bytes), 2967)
        self.assertEqual(len(packet_bytes), 811)
        self.assertEqual(reduction_percent, 72.666)
        self.assertGreaterEqual(reduction_percent, 60.0)

    def test_packet_schema_is_registered_with_its_current_artifact_hash(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.compiled-skill-packet"
        )

        self.assertEqual(entry["current_wire_version"], PACKET_WIRE_VERSION)
        self.assertEqual(entry["artifact_path"], SCHEMA_RELATIVE_PATH)
        self.assertEqual(entry["compatibility_mode"], "strict-versioned")
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )

if __name__ == "__main__":
    unittest.main()
