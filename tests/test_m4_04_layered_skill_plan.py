import copy
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator
import yaml


LOAD_TIME = "2026-08-11T08:00:00+08:00"


class AllowListedPlanAuthorizer:
    def __init__(self, plan_sha256):
        self.plan_sha256 = plan_sha256

    def authorize(self, *, plan_sha256, packet_sha256, manifest_set_sha256):
        if plan_sha256 == self.plan_sha256:
            return "authorization.m4-04.test"
        return None


class M404LayeredSkillPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.manifest_fixture = json.loads(
            (
                cls.root
                / "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
            ).read_text(encoding="utf-8")
        )

    def _case(self):
        from context_control_plane.compiled_skill_packet import compile_skill_packet
        from context_control_plane.layered_skill_plan import (
            compile_layered_skill_load_plan,
        )
        from context_control_plane.skill_drift_quarantine import assess_skill_drift

        specs = (
            ("core.bootstrap", "S0", b"b" * 1024),
            ("project.active", "S2", b"a" * 4096),
        )
        template = self.manifest_fixture["manifests"][0]
        manifests = []
        assets = {}
        bindings = {}
        for skill_id, layer, content in specs:
            manifest = copy.deepcopy(template)
            manifest["skill_id"] = skill_id
            manifest["version"] = "1.0.0"
            manifest["content_sha256"] = hashlib.sha256(content).hexdigest()
            manifest["rule_ids"] = [f"{skill_id}.rule"]
            manifest["dependencies"] = []
            manifest["conflicts"] = []
            manifest["expires_at"] = None
            manifests.append(manifest)
            assets[skill_id] = content
            bindings[skill_id] = layer
        manifest_set = {
            "schema_version": self.manifest_fixture["schema_version"],
            "manifests": manifests,
        }
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=[item[0] for item in specs],
            observed_at=LOAD_TIME,
        )
        assessment = assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: assets[skill_id],
            observed_at=LOAD_TIME,
        )
        plan = compile_layered_skill_load_plan(
            packet,
            layer_bindings=bindings,
            requested_layers=["S0", "S2"],
            observed_at=LOAD_TIME,
        )
        return packet, assessment, assets, plan

    def test_loader_requires_packet_bound_plan_and_authorization(self):
        from context_control_plane.layered_skill_loader import load_layered_skills
        from context_control_plane.layered_skill_plan import (
            layered_skill_load_plan_digest,
        )

        packet, assessment, assets, plan = self._case()
        result = load_layered_skills(
            packet,
            assessment,
            assets,
            layer_plan=plan,
            plan_authorizer=AllowListedPlanAuthorizer(
                layered_skill_load_plan_digest(plan)
            ),
            trusted_clock=lambda: datetime.fromisoformat(LOAD_TIME),
        )

        self.assertEqual(set(result.assets), {"core.bootstrap", "project.active"})
        self.assertEqual(
            result.receipt["layer_plan_sha256"],
            layered_skill_load_plan_digest(plan),
        )

    def test_relabelled_plan_is_rejected_by_original_authorization(self):
        from context_control_plane.layered_skill_loader import (
            LayeredSkillLoadInputError,
            load_layered_skills,
        )
        from context_control_plane.layered_skill_plan import (
            layered_skill_load_plan_digest,
        )

        packet, assessment, assets, plan = self._case()
        trusted_digest = layered_skill_load_plan_digest(plan)
        changed = copy.deepcopy(plan)
        changed["bindings"][1]["layer"] = "S3"

        with self.assertRaisesRegex(LayeredSkillLoadInputError, "not authorized"):
            load_layered_skills(
                packet,
                assessment,
                assets,
                layer_plan=changed,
                plan_authorizer=AllowListedPlanAuthorizer(trusted_digest),
                trusted_clock=lambda: datetime.fromisoformat(LOAD_TIME),
            )

    def test_plan_schema_and_registry_are_current(self):
        from context_control_plane.layered_skill_plan import (
            validate_layered_skill_load_plan,
        )

        _, _, _, plan = self._case()
        schema_path = self.root / "schemas/m4-04/layered-skill-load-plan.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_layered_skill_load_plan(plan)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(plan)), [])

        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.layered-skill-load-plan"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )

    def test_runtime_and_schema_reject_oversized_or_invalid_timestamps(self):
        from context_control_plane.layered_skill_plan import (
            LayeredSkillLoadPlanError,
            validate_layered_skill_load_plan,
        )

        _, _, _, plan = self._case()
        schema = json.loads(
            (
                self.root / "schemas/m4-04/layered-skill-load-plan.schema.json"
            ).read_text(encoding="utf-8")
        )
        for invalid_time in (
            "2026-08-11T08:00:00." + "1" * 100 + "+08:00",
            "2026-08-11T24:00:00+08:00",
            "2026-08-11T08:00:00+01:60",
            "2026-08-11T08:00:00+24:00",
        ):
            with self.subTest(invalid_time=invalid_time):
                candidate = copy.deepcopy(plan)
                candidate["observed_at"] = invalid_time
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(candidate))
                )
                with self.assertRaisesRegex(LayeredSkillLoadPlanError, "invalid"):
                    validate_layered_skill_load_plan(candidate)


if __name__ == "__main__":
    unittest.main()
