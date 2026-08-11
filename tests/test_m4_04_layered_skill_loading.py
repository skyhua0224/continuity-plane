import copy
import hashlib
import importlib
import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator
import yaml


MANIFEST_FIXTURE = "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
SCHEMA_RELATIVE_PATH = "schemas/m4-04/layered-skill-load.schema.json"
LOAD_FIXTURE = "experiments/skills/m4-04-layered-skill-load-v1alpha1.json"
LOAD_TIME = "2026-08-11T08:00:00+08:00"


def fixed_clock():
    return datetime.fromisoformat(LOAD_TIME)


class AllowListedPlanAuthorizer:
    def __init__(self, plan_sha256, authorization_ref="authorization.m4-04.test"):
        self.plan_sha256 = plan_sha256
        self.authorization_ref = authorization_ref

    def authorize(self, *, plan_sha256, packet_sha256, manifest_set_sha256):
        if plan_sha256 == self.plan_sha256:
            return self.authorization_ref
        return None


class M404LayeredSkillLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.manifest_fixture = json.loads(
            (cls.root / MANIFEST_FIXTURE).read_text(encoding="utf-8")
        )
        cls.schema_path = cls.root / SCHEMA_RELATIVE_PATH
        cls.load_fixture_path = cls.root / LOAD_FIXTURE
        try:
            cls.api = importlib.import_module(
                "context_control_plane.layered_skill_loader"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "layered_skill_loader module is missing")
        return self.api

    def _case(self, *, requested_layers=("S0", "S2"), layer_bindings=None):
        from context_control_plane.compiled_skill_packet import compile_skill_packet
        from context_control_plane.layered_skill_plan import (
            compile_layered_skill_load_plan,
        )
        from context_control_plane.skill_drift_quarantine import assess_skill_drift

        layer_specs = (
            ("core.bootstrap", "S0", b"b" * 1024),
            ("core.manifest", "S1", b"m" * 4096),
            ("project.active", "S2", b"a" * 4096),
            ("external.reference", "S3", b"r" * 16384),
        )
        template = self.manifest_fixture["manifests"][0]
        manifests = []
        assets = {}
        default_bindings = {}
        for skill_id, layer, content in layer_specs:
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
            default_bindings[skill_id] = layer
        manifest_set = {
            "schema_version": self.manifest_fixture["schema_version"],
            "manifests": manifests,
        }
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=[item[0] for item in layer_specs],
            observed_at=LOAD_TIME,
        )
        assessment = assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=lambda skill_id: assets[skill_id],
            observed_at=LOAD_TIME,
        )
        self.assertEqual(assessment["gate"], "allow")
        bindings = layer_bindings or default_bindings
        plan = compile_layered_skill_load_plan(
            packet,
            layer_bindings=bindings,
            requested_layers=list(requested_layers),
            observed_at=LOAD_TIME,
        )
        return packet, assessment, assets, default_bindings, plan

    def _load(
        self,
        api,
        packet,
        assessment,
        assets,
        plan,
        *,
        clock=fixed_clock,
        authorizer=None,
    ):
        from context_control_plane.layered_skill_plan import (
            layered_skill_load_plan_digest,
        )

        authorizer = authorizer or AllowListedPlanAuthorizer(
            layered_skill_load_plan_digest(plan)
        )
        return api.load_layered_skills(
            packet,
            assessment,
            assets,
            layer_plan=plan,
            plan_authorizer=authorizer,
            trusted_clock=clock,
        )

    def test_recovery_load_selects_s0_and_s2_and_reports_bytes_reduction(self):
        api = self._require_api()
        packet, assessment, assets, bindings, plan = self._case()

        result = self._load(api, packet, assessment, assets, plan)

        self.assertEqual(set(result.assets), {"core.bootstrap", "project.active"})
        self.assertEqual(result.receipt["available_content_bytes"], 25600)
        self.assertEqual(result.receipt["loaded_content_bytes"], 5120)
        self.assertEqual(result.receipt["omitted_content_bytes"], 20480)
        self.assertEqual(result.receipt["content_reduction_basis_points"], 8000)
        self.assertEqual(
            [item["layer"] for item in result.receipt["layers"]],
            ["S0", "S1", "S2", "S3"],
        )
        self.assertEqual(
            [item["layer"] for item in result.receipt["skills"]],
            sorted(bindings.values()),
        )
        self.assertEqual(
            result.receipt["layer_plan_authorization_ref"],
            "authorization.m4-04.test",
        )

    def test_all_layers_can_be_loaded_without_content_loss(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case(
            requested_layers=("S0", "S1", "S2", "S3")
        )

        result = self._load(api, packet, assessment, assets, plan)

        self.assertEqual(set(result.assets), set(assets))
        self.assertEqual(result.receipt["loaded_content_bytes"], 25600)
        self.assertEqual(result.receipt["content_reduction_basis_points"], 0)

    def test_result_does_not_expose_mutable_verified_state(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        result = self._load(api, packet, assessment, assets, plan)

        changed_receipt = result.receipt
        changed_receipt["loaded_content_bytes"] = 0
        changed_receipt["skills"][0]["loaded"] = False
        self.assertEqual(result.receipt["loaded_content_bytes"], 5120)
        self.assertTrue(result.receipt["skills"][0]["loaded"])
        with self.assertRaises(TypeError):
            result.assets["core.bootstrap"] = b"changed"
        self.assertFalse(hasattr(result, "_receipt"))
        self.assertIsInstance(result._receipt_bytes, bytes)
        with self.assertRaises(FrozenInstanceError):
            result._receipt_bytes = b"changed"

    def test_quarantine_receipt_is_rejected_before_asset_access(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        quarantined = copy.deepcopy(assessment)
        quarantined["gate"] = "quarantine"
        quarantined["findings"] = [
            {"scope": "asset", "code": "missing-path", "detail": "unavailable"}
        ]
        quarantined["skills"][0]["status"] = "quarantined"
        quarantined["skills"][0]["reason_codes"] = ["missing-path"]
        quarantined["skills"][0]["observed_content_sha256"] = None
        accesses = []

        class AccessTrackingMapping(dict):
            def __getitem__(self, key):
                accesses.append(key)
                return super().__getitem__(key)

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "allow"):
            self._load(
                api,
                packet,
                quarantined,
                AccessTrackingMapping(assets),
                plan,
            )
        self.assertEqual(accesses, [])

    def test_stale_allow_assessment_cannot_be_replayed_at_load_time(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "time"):
            self._load(
                api,
                packet,
                assessment,
                assets,
                plan,
                clock=lambda: datetime.fromisoformat(
                    "2026-08-11T08:01:00+08:00"
                ),
            )

    def test_packet_or_assessment_identity_drift_is_rejected(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        changed_packet = copy.deepcopy(packet)
        changed_packet["selections"][0]["version"] = "1.0.1"

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "packet"):
            self._load(api, changed_packet, assessment, assets, plan)

        changed_assessment = copy.deepcopy(assessment)
        changed_assessment["packet_sha256"] = "a" * 64
        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "packet"):
            self._load(api, packet, changed_assessment, assets, plan)

    def test_manifest_set_identity_drift_is_rejected_before_asset_access(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        changed_assessment = copy.deepcopy(assessment)
        changed_assessment["manifest_set_sha256"] = "a" * 64
        accesses = []

        class AccessTrackingMapping(dict):
            def __getitem__(self, key):
                accesses.append(key)
                return super().__getitem__(key)

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "manifest"):
            self._load(
                api,
                packet,
                changed_assessment,
                AccessTrackingMapping(assets),
                plan,
            )
        self.assertEqual(accesses, [])

    def test_asset_mapping_cannot_mutate_assessment_after_identity_gate(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        expected_assessment_digest = api.skill_drift_assessment_digest(assessment)

        class MutatingMapping(dict):
            def __iter__(self):
                assessment["manifest_set_sha256"] = "a" * 64
                return super().__iter__()

        result = self._load(
            api, packet, assessment, MutatingMapping(assets), plan
        )
        self.assertEqual(
            result.receipt["drift_assessment_sha256"], expected_assessment_digest
        )

    def test_asset_access_order_follows_compiled_packet(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        accesses = []

        class AccessTrackingMapping(dict):
            def __getitem__(self, key):
                accesses.append(key)
                return super().__getitem__(key)

        self._load(api, packet, assessment, AccessTrackingMapping(assets), plan)
        self.assertEqual(
            accesses, [selection["skill_id"] for selection in packet["selections"]]
        )

    def test_asset_digest_must_match_packet_and_drift_assessment(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        changed_assets = dict(assets)
        changed_assets["project.active"] = b"changed"

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "digest"):
            self._load(api, packet, assessment, changed_assets, plan)

    def test_relabelled_plan_requires_separate_authorization(self):
        api = self._require_api()
        from context_control_plane.layered_skill_plan import (
            layered_skill_load_plan_digest,
        )

        packet, assessment, assets, _, plan = self._case()
        original_digest = layered_skill_load_plan_digest(plan)
        relabeled = copy.deepcopy(plan)
        next(
            item
            for item in relabeled["bindings"]
            if item["skill_id"] == "external.reference"
        )["layer"] = "S0"

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "authorized"):
            self._load(
                api,
                packet,
                assessment,
                assets,
                relabeled,
                authorizer=AllowListedPlanAuthorizer(original_digest),
            )

    def test_missing_or_malformed_authorization_is_denied(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()

        for authorizer in (object(), AllowListedPlanAuthorizer("a" * 64, "bad ref")):
            with self.subTest(authorizer=authorizer):
                with self.assertRaisesRegex(
                    api.LayeredSkillLoadInputError, "authoriz"
                ):
                    self._load(
                        api,
                        packet,
                        assessment,
                        assets,
                        plan,
                        authorizer=authorizer,
                    )

    def test_plan_must_cover_packet_and_include_s0(self):
        from context_control_plane.layered_skill_plan import (
            LayeredSkillLoadPlanError,
            compile_layered_skill_load_plan,
        )

        packet, _, _, bindings, _ = self._case()
        missing = dict(bindings)
        missing.pop("external.reference")
        without_s0 = dict(bindings)
        without_s0["core.bootstrap"] = "S1"

        with self.assertRaisesRegex(LayeredSkillLoadPlanError, "cover"):
            compile_layered_skill_load_plan(
                packet,
                layer_bindings=missing,
                requested_layers=["S0", "S2"],
                observed_at=LOAD_TIME,
            )
        with self.assertRaisesRegex(LayeredSkillLoadPlanError, "S0"):
            compile_layered_skill_load_plan(
                packet,
                layer_bindings=without_s0,
                requested_layers=["S0", "S2"],
                observed_at=LOAD_TIME,
            )

    def test_requested_layers_require_s0_and_reject_duplicates_or_unknowns(self):
        from context_control_plane.layered_skill_plan import (
            LayeredSkillLoadPlanError,
            compile_layered_skill_load_plan,
        )

        packet, _, _, bindings, _ = self._case()
        for requested_layers in (["S2"], ["S0", "S0"], ["S0", "S4"]):
            with self.subTest(requested_layers=requested_layers):
                with self.assertRaisesRegex(LayeredSkillLoadPlanError, "layer"):
                    compile_layered_skill_load_plan(
                        packet,
                        layer_bindings=bindings,
                        requested_layers=requested_layers,
                        observed_at=LOAD_TIME,
                    )

    def test_receipt_runtime_rejects_identifier_over_256_characters(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        receipt = self._load(api, packet, assessment, assets, plan).receipt
        receipt["skills"][0]["skill_id"] = "a" * 257

        with self.assertRaisesRegex(ValueError, "ID"):
            api.validate_layered_skill_load_receipt(receipt)

    def test_receipt_layer_binding_digest_must_match_skill_entries(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        receipt = self._load(api, packet, assessment, assets, plan).receipt
        receipt["layer_bindings_sha256"] = "a" * 64

        with self.assertRaisesRegex(ValueError, "binding"):
            api.validate_layered_skill_load_receipt(receipt)

    def test_receipt_requires_s0_skill(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        receipt = self._load(api, packet, assessment, assets, plan).receipt
        bootstrap = next(
            skill for skill in receipt["skills"] if skill["skill_id"] == "core.bootstrap"
        )
        bootstrap["layer"] = "S1"
        bootstrap["loaded"] = False
        receipt["layers"][0]["skill_count"] = 0
        receipt["layers"][0]["content_bytes"] = 0
        receipt["layers"][1]["skill_count"] = 2
        receipt["layers"][1]["content_bytes"] = 5120
        receipt["loaded_content_bytes"] = 4096
        receipt["omitted_content_bytes"] = 21504
        receipt["content_reduction_basis_points"] = 8400
        receipt["layer_bindings_sha256"] = api.layer_bindings_digest(
            {skill["skill_id"]: skill["layer"] for skill in receipt["skills"]}
        )

        with self.assertRaisesRegex(ValueError, "S0"):
            api.validate_layered_skill_load_receipt(receipt)

    def test_schema_and_runtime_reject_non_rfc3339_load_times(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        receipt = self._load(api, packet, assessment, assets, plan).receipt
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))

        for invalid_time in (
            "2026-08-11 08:00:00+08:00",
            "2026-08-11T08:00:00+08",
            "2026-08-11T24:00:00+08:00",
            "2026-08-11T08:00:00+01:60",
            "2026-08-11T08:00:00+22:99",
        ):
            with self.subTest(invalid_time=invalid_time):
                candidate = copy.deepcopy(receipt)
                candidate["assessment_observed_at"] = invalid_time
                candidate["loaded_at"] = invalid_time
                self.assertTrue(
                    list(Draft202012Validator(schema).iter_errors(candidate))
                )
                with self.assertRaisesRegex(ValueError, "invalid"):
                    api.validate_layered_skill_load_receipt(candidate)

    def test_direct_paths_and_non_bytes_assets_are_rejected(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        path_assets = dict(assets)
        path_assets["core.bootstrap"] = Path("/untrusted/SKILL.md")

        with self.assertRaisesRegex(api.LayeredSkillLoadInputError, "bytes"):
            self._load(api, packet, assessment, path_assets, plan)

    def test_bytes_subclasses_cannot_forge_content_lengths(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()

        class LyingBytes(bytes):
            def __len__(self):
                return 1

        subclass_assets = {
            skill_id: LyingBytes(content) for skill_id, content in assets.items()
        }
        result = self._load(api, packet, assessment, subclass_assets, plan)

        self.assertEqual(result.receipt["available_content_bytes"], 25600)
        self.assertEqual(result.receipt["loaded_content_bytes"], 5120)
        self.assertTrue(all(type(asset) is bytes for asset in result.assets.values()))

    def test_receipt_is_strict_canonical_and_registered(self):
        api = self._require_api()
        packet, assessment, assets, _, plan = self._case()
        result = self._load(api, packet, assessment, assets, plan)
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            list(Draft202012Validator(schema).iter_errors(result.receipt)), []
        )
        reversed_receipt = copy.deepcopy(result.receipt)
        reversed_receipt["skills"].reverse()
        reversed_receipt["layers"].reverse()
        self.assertEqual(
            api.canonical_layered_skill_load_bytes(result.receipt),
            api.canonical_layered_skill_load_bytes(reversed_receipt),
        )

        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.layered-skill-load"
        )
        self.assertEqual(entry["artifact_path"], SCHEMA_RELATIVE_PATH)
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )

    def test_versioned_fixture_is_canonical(self):
        api = self._require_api()
        fixture_bytes = self.load_fixture_path.read_bytes()
        fixture = json.loads(fixture_bytes)
        api.validate_layered_skill_load_receipt(fixture)
        self.assertEqual(
            fixture_bytes,
            api.canonical_layered_skill_load_bytes(fixture) + b"\n",
        )


if __name__ == "__main__":
    unittest.main()
