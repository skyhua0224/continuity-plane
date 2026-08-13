import base64
import copy
import dataclasses
import hashlib
import importlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from types import MappingProxyType

import yaml
from jsonschema import Draft202012Validator


LOAD_TIME = "2026-08-11T08:00:00+08:00"
MANIFEST_FIXTURE = "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"


class AllowListedPlanAuthorizer:
    def __init__(self, plan_sha256):
        self.plan_sha256 = plan_sha256

    def authorize(self, *, plan_sha256, packet_sha256, manifest_set_sha256):
        if plan_sha256 == self.plan_sha256:
            return "authorization.m4-05.test"
        return None


class M405ProviderSkillAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        try:
            cls.api = importlib.import_module(
                "context_control_plane.provider_skill_adapter"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "provider Skill adapter module is missing")
        return self.api

    def _case(
        self,
        *,
        active_size=4096,
        active_content=None,
        bootstrap_id="core.bootstrap",
        active_id="project.active",
    ):
        from context_control_plane.compiled_skill_packet import compile_skill_packet
        from context_control_plane.layered_skill_loader import load_layered_skills
        from context_control_plane.layered_skill_plan import (
            compile_layered_skill_load_plan,
            layered_skill_load_plan_digest,
        )
        from context_control_plane.skill_drift_quarantine import assess_skill_drift

        fixture = json.loads(
            (self.root / MANIFEST_FIXTURE).read_text(encoding="utf-8")
        )
        active_content = (
            b"a" * active_size if active_content is None else active_content
        )
        specs = (
            (bootstrap_id, "S0", b"b" * 1024),
            (active_id, "S2", active_content),
        )
        template = fixture["manifests"][0]
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
            "schema_version": fixture["schema_version"],
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
        authorizer = AllowListedPlanAuthorizer(
            layered_skill_load_plan_digest(plan)
        )
        loaded = load_layered_skills(
            packet,
            assessment,
            assets,
            layer_plan=plan,
            plan_authorizer=authorizer,
            trusted_clock=lambda: datetime.fromisoformat(LOAD_TIME),
        )
        self._assessment = assessment
        return packet, loaded, plan, authorizer

    def _compose(self, adapter, packet, loaded, plan, authorizer):
        return adapter.compose(
            packet,
            loaded,
            layer_plan=plan,
            plan_authorizer=authorizer,
            drift_assessment=self._assessment,
        )

    def test_codex_and_claude_replay_share_one_neutral_effect(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()

        codex = api.get_provider_skill_adapter("codex")
        claude = api.get_provider_skill_adapter("claude")
        codex_effect = self._compose(codex, packet, loaded, plan, authorizer)
        claude_effect = self._compose(claude, packet, loaded, plan, authorizer)

        api.validate_provider_skill_effect(codex_effect)
        api.validate_provider_skill_effect(claude_effect)
        self.assertEqual(
            api.provider_neutral_effect_digest(codex_effect),
            api.provider_neutral_effect_digest(claude_effect),
        )
        self.assertEqual(codex_effect["composition_body_bytes"], 5120)
        self.assertEqual(claude_effect["composition_body_bytes"], 5120)
        self.assertEqual(
            codex_effect["composition_sections"],
            claude_effect["composition_sections"],
        )
        self.assertEqual(
            [section["skill_id"] for section in codex_effect["composition_sections"]],
            ["core.bootstrap", "project.active"],
        )
        self.assertEqual(
            codex_effect["selected_skill_ids"],
            claude_effect["selected_skill_ids"],
        )
        self.assertEqual(
            codex_effect["selected_rule_ids"],
            claude_effect["selected_rule_ids"],
        )
        self.assertNotEqual(
            codex_effect["provider_payload_sha256"],
            claude_effect["provider_payload_sha256"],
        )
        self.assertEqual(
            codex_effect["adapter_surface_contract"],
            "context.adapter-surface/codex-app-server-skill-input/v1alpha1",
        )
        self.assertEqual(
            claude_effect["adapter_surface_contract"],
            "context.adapter-surface/claude-agent-sdk-filesystem-skill/v1alpha1",
        )
        self.assertFalse(codex_effect["writes_runtime_state"])
        self.assertIsNone(codex_effect["state_revision"])

    def test_each_provider_replay_is_byte_deterministic(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()

        for provider_id in ("codex", "claude"):
            with self.subTest(provider_id=provider_id):
                adapter = api.get_provider_skill_adapter(provider_id)
                first = self._compose(adapter, packet, loaded, plan, authorizer)
                second = self._compose(adapter, packet, loaded, plan, authorizer)
                self.assertEqual(first, second)
                self.assertEqual(
                    api.provider_effect_digest(first),
                    api.provider_effect_digest(second),
                )
                self.assertEqual(
                    api.recover_provider_composition_sections(
                        api.materialize_provider_payload(first)
                    ),
                    first["composition_sections"],
                )

    def test_effect_rejects_packet_or_load_identity_drift(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        adapter = api.get_provider_skill_adapter("codex")

        changed_packet = copy.deepcopy(packet)
        changed_packet["selections"][0]["version"] = "1.0.1"
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "packet"):
            self._compose(adapter, changed_packet, loaded, plan, authorizer)

        changed_receipt = loaded.receipt
        changed_receipt["packet_sha256"] = "a" * 64
        from context_control_plane.layered_skill_loader import (
            canonical_layered_skill_load_bytes,
        )

        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "receipt"):
            self._compose(
                adapter,
                packet,
                dataclasses.replace(
                    loaded,
                    _receipt_bytes=canonical_layered_skill_load_bytes(changed_receipt),
                ),
                plan,
                authorizer,
            )

    def test_effect_rejects_unverified_or_missing_composition_bytes(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        adapter = api.get_provider_skill_adapter("codex")
        changed_assets = dict(loaded.assets)
        changed_assets["project.active"] = b"changed"

        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "content"):
            self._compose(
                adapter,
                packet,
                dataclasses.replace(
                    loaded,
                    assets=MappingProxyType(changed_assets),
                ),
                plan,
                authorizer,
            )

        effect = self._compose(adapter, packet, loaded, plan, authorizer)
        effect["composition_sections"][1]["content_b64"] = base64.b64encode(
            b"changed"
        ).decode("ascii")
        with self.assertRaisesRegex(ValueError, "composition"):
            api.validate_provider_skill_effect(effect)

    def test_effect_rejects_self_consistent_receipt_content_not_bound_to_packet(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        receipt = loaded.receipt
        replacement = b"replacement content"
        skill = next(
            item for item in receipt["skills"] if item["skill_id"] == "project.active"
        )
        skill["content_sha256"] = hashlib.sha256(replacement).hexdigest()
        skill["content_bytes"] = len(replacement)
        receipt["available_content_bytes"] = 1024 + len(replacement)
        receipt["loaded_content_bytes"] = 1024 + len(replacement)
        receipt["omitted_content_bytes"] = 0
        next(item for item in receipt["layers"] if item["layer"] == "S2")[
            "content_bytes"
        ] = len(replacement)
        from context_control_plane.layered_skill_loader import (
            canonical_layered_skill_load_bytes,
        )

        forged = dataclasses.replace(
            loaded,
            _receipt_bytes=canonical_layered_skill_load_bytes(receipt),
            assets=MappingProxyType(
                {"core.bootstrap": loaded.assets["core.bootstrap"], "project.active": replacement}
            ),
        )
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "packet"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                forged,
                plan,
                authorizer,
            )

    def test_effect_accepts_only_controlled_loader_results(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "controlled"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                SimpleNamespace(receipt=loaded.receipt, assets=loaded.assets),
                plan,
                authorizer,
            )

    def test_effect_rejects_composition_larger_than_schema_limit(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case(
            active_size=8 * 1024 * 1024 + 1
        )
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "composition"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                loaded,
                plan,
                authorizer,
            )

    def test_effect_rejects_self_consistent_requested_layer_rewrite(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        receipt = loaded.receipt
        receipt["requested_layers"] = ["S0"]
        next(item for item in receipt["skills"] if item["skill_id"] == "project.active")[
            "loaded"
        ] = False
        next(item for item in receipt["layers"] if item["layer"] == "S2")[
            "loaded"
        ] = False
        receipt["loaded_content_bytes"] = 1024
        receipt["omitted_content_bytes"] = 4096
        receipt["content_reduction_basis_points"] = 8000
        from context_control_plane.layered_skill_loader import (
            canonical_layered_skill_load_bytes,
        )

        forged = dataclasses.replace(
            loaded,
            _receipt_bytes=canonical_layered_skill_load_bytes(receipt),
            assets=MappingProxyType({"core.bootstrap": loaded.assets["core.bootstrap"]}),
        )
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "plan"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                forged,
                plan,
                authorizer,
            )

    def test_effect_rejects_drift_assessment_receipt_rewrite(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        receipt = loaded.receipt
        receipt["drift_assessment_sha256"] = "a" * 64
        from context_control_plane.layered_skill_loader import (
            canonical_layered_skill_load_bytes,
        )

        forged = dataclasses.replace(
            loaded,
            _receipt_bytes=canonical_layered_skill_load_bytes(receipt),
        )
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "drift"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                forged,
                plan,
                authorizer,
            )

    def test_materialized_skills_follow_provider_filesystem_contracts(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        for provider_id in ("codex", "claude"):
            with self.subTest(provider_id=provider_id):
                effect = self._compose(
                    api.get_provider_skill_adapter(provider_id),
                    packet,
                    loaded,
                    plan,
                    authorizer,
                )
                payload = json.loads(api.materialize_provider_payload(effect))
                assets = payload["filesystem_assets"]
                aliases = [asset["provider_skill_name"] for asset in assets]
                self.assertEqual(len(aliases), len(set(aliases)))
                for asset, section in zip(
                    assets, effect["composition_sections"], strict=True
                ):
                    alias = asset["provider_skill_name"]
                    self.assertRegex(alias, r"^[a-z0-9-]{1,64}$")
                    self.assertEqual(Path(asset["path"]).parent.name, alias)
                    materialized = base64.b64decode(
                        asset["materialized_content_b64"], validate=True
                    )
                    header = (
                        f"---\nname: {alias}\n"
                        f"description: Context Control Plane Skill {section['skill_id']}\n"
                        "---\n"
                    ).encode("ascii")
                    self.assertTrue(materialized.startswith(header))
                    self.assertEqual(
                        materialized[len(header) :],
                        base64.b64decode(section["content_b64"], validate=True),
                    )
                if provider_id == "codex":
                    self.assertEqual(
                        payload["turn_start"]["input"],
                        [
                            item
                            for alias, asset in zip(aliases, assets, strict=True)
                            for item in (
                                {"type": "text", "text": f"${alias}"},
                                {
                                    "type": "skill",
                                    "name": alias,
                                    "path": asset["path"],
                                },
                            )
                        ],
                    )
                else:
                    self.assertEqual(
                        payload["agent_sdk_init"]["skills"], aliases
                    )
                    self.assertEqual(
                        payload["agent_sdk_init"]["setting_sources"],
                        ["project"],
                    )

    def test_provider_adapter_rejects_source_skill_frontmatter(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case(
            active_content=b"---\nname: foreign-skill\n---\nbody\n"
        )

        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "body-only"):
            self._compose(
                api.get_provider_skill_adapter("codex"),
                packet,
                loaded,
                plan,
                authorizer,
            )

    def test_recovery_rejects_aggregate_rule_limit_overflow(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        effect = self._compose(
            api.get_provider_skill_adapter("codex"),
            packet,
            loaded,
            plan,
            authorizer,
        )
        payload = json.loads(api.materialize_provider_payload(effect))
        first, second = payload["filesystem_assets"]
        first["rule_ids"] = [f"rule-{index:05d}" for index in range(32768)]
        second["rule_ids"] = [
            f"rule-{index:05d}" for index in range(32768, 65537)
        ]
        changed = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "rule"):
            api.recover_provider_composition_sections(changed)

    def test_codex_recovery_rejects_unmodeled_turn_start_fields(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        effect = self._compose(
            api.get_provider_skill_adapter("codex"),
            packet,
            loaded,
            plan,
            authorizer,
        )
        payload = json.loads(api.materialize_provider_payload(effect))
        payload["turn_start"]["unmodeled_write_capability"] = True
        changed = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "Codex"):
            api.recover_provider_composition_sections(changed)

    def test_claude_recovery_rejects_malformed_skill_names(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case()
        effect = self._compose(
            api.get_provider_skill_adapter("claude"),
            packet,
            loaded,
            plan,
            authorizer,
        )
        payload = json.loads(api.materialize_provider_payload(effect))
        payload["agent_sdk_init"]["skills"] = "core.bootstrap"
        changed = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        with self.assertRaisesRegex(api.ProviderSkillAdapterError, "Claude"):
            api.recover_provider_composition_sections(changed)

    def test_composition_preserves_s0_before_s2_for_reverse_sorted_ids(self):
        api = self._require_api()
        packet, loaded, plan, authorizer = self._case(
            bootstrap_id="z.bootstrap",
            active_id="a.active",
        )
        effect = self._compose(
            api.get_provider_skill_adapter("codex"),
            packet,
            loaded,
            plan,
            authorizer,
        )
        self.assertEqual(
            [
                (section["layer"], section["skill_id"])
                for section in effect["composition_sections"]
            ],
            [("S0", "z.bootstrap"), ("S2", "a.active")],
        )

    def test_effect_schema_and_registry_are_current(self):
        api = self._require_api()
        schema_path = self.root / "schemas/m4-05/provider-skill-adapter-effect.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        packet, loaded, plan, authorizer = self._case()
        effect = self._compose(
            api.get_provider_skill_adapter("codex"),
            packet,
            loaded,
            plan,
            authorizer,
        )
        api.validate_provider_skill_effect(effect)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(effect)), [])
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.provider-skill-adapter-effect"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
