import copy
import hashlib
import json
import random
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

OBSERVED_AT = "2026-08-14T12:00:00+08:00"
PROVENANCE_REF = "artifact://sha256/" + "a" * 64
APPROVAL_REF = "artifact://sha256/" + "b" * 64
VERIFICATION_REF = "artifact://sha256/" + "c" * 64


def _range():
    return {
        "minimum": "1.0.0",
        "minimum_inclusive": True,
        "maximum": "1.0.0",
        "maximum_inclusive": True,
    }


def _entry(
    skill_id,
    source_kind,
    applicability,
    *,
    status="approved",
    dependencies=None,
    conflicts=None,
    expires_at=None,
    provider_contracts=None,
    schema_refs=None,
):
    manifest_status = "active" if status == "active" else "approved"
    manifest = {
        "skill_id": skill_id,
        "version": "1.0.0",
        "content_sha256": hashlib.sha256(skill_id.encode()).hexdigest(),
        "source_kind": source_kind,
        "license_ref": "MIT",
        "applicability": applicability,
        "rule_ids": [f"rule.{skill_id}.main"],
        "dependencies": [
            {"skill_id": dependency, "version_range": _range()}
            for dependency in (dependencies or [])
        ],
        "conflicts": [
            {"skill_id": conflict, "version_range": _range()}
            for conflict in (conflicts or [])
        ],
        "expires_at": expires_at,
        "compatibility": {
            "schema_refs": schema_refs
            or ["context.compiled-skill-packet/v1alpha1"],
            "provider_contract_refs": provider_contracts
            if provider_contracts is not None
            else ["provider://codex/v1"],
        },
        "provenance_refs": [PROVENANCE_REF],
        "status": manifest_status,
    }
    source_url = {
        "builtin": f"builtin://{skill_id}",
        "workflow": f"workflow://{skill_id}",
    }.get(source_kind, f"https://skills.example/{skill_id}")
    source_revision = (
        "1.0.0" if source_kind in {"builtin", "workflow"} else "d" * 40
    )
    return {
        "catalog_entry_id": f"catalog/{skill_id}",
        "source_kind": source_kind,
        "manifest": manifest,
        "source_url": source_url,
        "source_revision": source_revision,
        "source_path": "SKILL.md",
        "publisher": "context-control-plane",
        "license_ref": "MIT",
        "provenance_refs": [PROVENANCE_REF],
        "capabilities": ["read:repository"],
        "approval_refs": [APPROVAL_REF],
        "verification_refs": [VERIFICATION_REF],
        "permissions": {
            "state_write": False,
            "task_switch": False,
            "claim": False,
            "effect": False,
            "promotion": False,
            "evidence_gate": False,
        },
        "status": status,
    }


def _catalog(entries):
    return {
        "schema_version": "context.skill-catalog/v1alpha1",
        "catalog_id": "skill-catalog/m4-09",
        "catalog_revision": "2026-08-14T00:00:00+08:00",
        "entries": entries,
    }


def _request(catalog, **changes):
    from context_control_plane.skill_catalog import canonical_skill_catalog_bytes

    document = {
        "schema_version": "context.skill-resolution-request/v1alpha1",
        "request_id": "resolve-m4-09",
        "catalog_sha256": hashlib.sha256(
            canonical_skill_catalog_bytes(
                catalog, observed_at=catalog["catalog_revision"]
            )
        ).hexdigest(),
        "project_ref": "project://demo",
        "repo_ref": "repo://main",
        "path_refs": ["path://src/core", "path://tests"],
        "task_ref": "task://code-change",
        "role_ref": "role://executor",
        "operation_ref": "operation://code-change",
        "provider_contract_ref": "provider://codex/v1",
        "required_schema_refs": ["context.compiled-skill-packet/v1alpha1"],
        "required_skill_ids": [],
        "binding_provenance_ref": None,
        "observed_at": OBSERVED_AT,
        "state_write_authority": False,
    }
    document.update(changes)
    return document


def _base_catalog():
    return _catalog(
        [
            _entry(
                "builtin.recovery",
                "builtin",
                [{"kind": "operation", "ref": "operation://code-change"}],
                status="active",
            ),
            _entry(
                "project.testing",
                "project",
                [
                    {"kind": "project", "ref": "project://demo"},
                    {"kind": "repo", "ref": "repo://main"},
                    {"kind": "task", "ref": "task://code-change"},
                    {"kind": "operation", "ref": "operation://code-change"},
                ],
                dependencies=["builtin.recovery"],
            ),
            _entry(
                "workflow.executor",
                "workflow",
                [
                    {"kind": "role", "ref": "role://executor"},
                    {"kind": "operation", "ref": "operation://code-change"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
                status="active",
            ),
            _entry(
                "workflow.verifier",
                "workflow",
                [
                    {"kind": "role", "ref": "role://verifier"},
                    {"kind": "operation", "ref": "operation://test"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
                status="active",
            ),
            _entry(
                "user.style",
                "user",
                [
                    {"kind": "role", "ref": "role://executor"},
                    {"kind": "task", "ref": "task://code-change"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
            ),
            _entry(
                "external.claude",
                "external",
                [
                    {"kind": "operation", "ref": "operation://code-change"},
                    {"kind": "provider", "ref": "provider://claude-code"},
                ],
                provider_contracts=["provider://claude-code/v1"],
            ),
        ]
    )


class M409SkillResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_dir = cls.root / "schemas/m4-09"

    def _resolve(self, catalog=None, request=None):
        from context_control_plane.skill_resolver import resolve_skills

        catalog = catalog or _base_catalog()
        return resolve_skills(catalog, request or _request(catalog))

    def test_module_strict_schemas_and_registry_are_contract_surfaces(self):
        from context_control_plane import skill_resolver

        self.assertEqual(
            skill_resolver.REQUEST_SCHEMA_VERSION,
            "context.skill-resolution-request/v1alpha1",
        )
        self.assertEqual(
            skill_resolver.DECISION_SCHEMA_VERSION,
            "context.skill-resolution-decision/v1alpha1",
        )
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        for schema_id, filename in (
            ("context.skill-resolution-request", "skill-resolution-request.schema.json"),
            ("context.skill-resolution-decision", "skill-resolution-decision.schema.json"),
        ):
            schema_path = self.schema_dir / filename
            self.assertTrue(schema_path.is_file())
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertFalse(schema.get("additionalProperties", True))
            entry = next(
                item for item in registry["schemas"] if item["schema_id"] == schema_id
            )
            self.assertEqual(
                entry["content_sha256"],
                hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            )

    def test_resolver_selects_only_matching_role_operation_provider_and_context(self):
        catalog = _base_catalog()
        outcome = self._resolve(catalog)

        self.assertEqual(outcome.decision["disposition"], "resolved")
        self.assertEqual(
            [item["skill_id"] for item in outcome.decision["selected_skills"]],
            [
                "builtin.recovery",
                "project.testing",
                "user.style",
                "workflow.executor",
            ],
        )
        self.assertNotIn("workflow.verifier", outcome.manifest_set_skill_ids)
        self.assertNotIn("external.claude", outcome.manifest_set_skill_ids)
        self.assertEqual(
            outcome.decision["selected_rule_ids"],
            sorted(
                f"rule.{skill_id}.main"
                for skill_id in outcome.manifest_set_skill_ids
            ),
        )
        self.assertFalse(outcome.decision["state_write_authority"])

    def test_same_applicability_kind_is_or_and_distinct_kinds_are_and(self):
        path_skill = _entry(
            "project.paths",
            "project",
            [
                {"kind": "path", "ref": "path://src/other"},
                {"kind": "path", "ref": "path://src/core"},
                {"kind": "role", "ref": "role://executor"},
            ],
        )
        wrong_role = copy.deepcopy(path_skill)
        wrong_role["catalog_entry_id"] = "catalog/project.wrong-role"
        wrong_role["manifest"]["skill_id"] = "project.wrong-role"
        wrong_role["manifest"]["content_sha256"] = "e" * 64
        wrong_role["manifest"]["rule_ids"] = ["rule.project.wrong-role.main"]
        for item in wrong_role["manifest"]["applicability"]:
            if item["kind"] == "role":
                item["ref"] = "role://thinker"
        catalog = _catalog([path_skill, wrong_role])

        outcome = self._resolve(catalog)

        self.assertEqual(outcome.manifest_set_skill_ids, ("project.paths",))

    def test_path_applicability_uses_segment_prefix_without_sibling_collision(self):
        entry = _entry(
            "project.path-prefix",
            "project",
            [{"kind": "path", "ref": "path://src/core"}],
        )
        catalog = _catalog([entry])

        descendant = self._resolve(
            catalog,
            _request(catalog, path_refs=["path://src/core/resolver.py"]),
        )
        sibling = self._resolve(
            catalog,
            _request(catalog, path_refs=["path://src/core2/resolver.py"]),
        )

        self.assertEqual(descendant.manifest_set_skill_ids, ("project.path-prefix",))
        self.assertEqual(sibling.decision["disposition"], "quarantined")
        self.assertEqual(sibling.decision["reason_code"], "no-applicable-skill")

    def test_dependency_closure_keeps_dependency_in_compiled_set(self):
        dependency = _entry(
            "builtin.dependency",
            "builtin",
            [{"kind": "operation", "ref": "operation://code-change"}],
            status="active",
        )
        root = _entry(
            "project.root",
            "project",
            [
                {"kind": "project", "ref": "project://demo"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            dependencies=["builtin.dependency"],
        )
        catalog = _catalog([root, dependency])

        outcome = self._resolve(catalog)

        self.assertEqual(
            outcome.manifest_set_skill_ids,
            ("builtin.dependency", "project.root"),
        )
        self.assertEqual(outcome.decision["disposition"], "resolved")

    def test_explicit_binding_is_bound_to_provenance_and_cannot_bypass_applicability(self):
        catalog = _base_catalog()
        request = _request(
            catalog,
            required_skill_ids=["workflow.verifier"],
            binding_provenance_ref="artifact://sha256/" + "f" * 64,
        )

        outcome = self._resolve(catalog, request)

        self.assertEqual(outcome.decision["disposition"], "quarantined")
        self.assertEqual(outcome.decision["reason_code"], "required-skill-inapplicable")
        self.assertIsNone(outcome.packet)

    def test_higher_priority_context_match_suppresses_lower_conflict(self):
        project = _entry(
            "project.secure",
            "project",
            [
                {"kind": "project", "ref": "project://demo"},
                {"kind": "repo", "ref": "repo://main"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            conflicts=["workflow.lightweight"],
        )
        workflow = _entry(
            "workflow.lightweight",
            "workflow",
            [
                {"kind": "role", "ref": "role://executor"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            status="active",
        )
        catalog = _catalog([workflow, project])

        outcome = self._resolve(catalog)

        self.assertEqual(outcome.manifest_set_skill_ids, ("project.secure",))
        self.assertEqual(
            outcome.decision["suppressed_skills"],
            [
                {
                    "skill_id": "workflow.lightweight",
                    "reason_code": "lower-priority-conflict",
                    "conflicting_with": "project.secure",
                }
            ],
        )

    def test_equal_priority_conflict_and_dependency_conflict_fail_closed(self):
        first = _entry(
            "workflow.first",
            "workflow",
            [
                {"kind": "role", "ref": "role://executor"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            status="active",
            conflicts=["workflow.second"],
        )
        second = _entry(
            "workflow.second",
            "workflow",
            [
                {"kind": "role", "ref": "role://executor"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            status="active",
        )
        catalog = _catalog([first, second])
        outcome = self._resolve(catalog)
        self.assertEqual(outcome.decision["disposition"], "quarantined")
        self.assertEqual(outcome.decision["reason_code"], "unresolved-conflict")

        dependency = copy.deepcopy(second)
        dependency["manifest"]["skill_id"] = "workflow.dependency"
        dependency["catalog_entry_id"] = "catalog/workflow.dependency"
        dependency["manifest"]["content_sha256"] = "9" * 64
        dependency["manifest"]["rule_ids"] = ["rule.workflow.dependency.main"]
        dependency["manifest"]["applicability"] = [
            {"kind": "operation", "ref": "operation://code-change"}
        ]
        root = _entry(
            "project.root",
            "project",
            [
                {"kind": "project", "ref": "project://demo"},
                {"kind": "operation", "ref": "operation://code-change"},
            ],
            dependencies=["workflow.dependency"],
            conflicts=["workflow.dependency"],
        )
        dependency_catalog = _catalog([root, dependency])
        dependency_outcome = self._resolve(dependency_catalog)
        self.assertEqual(dependency_outcome.decision["disposition"], "quarantined")
        self.assertEqual(
            dependency_outcome.decision["reason_code"], "dependency-conflict"
        )

    def test_expiry_boundary_quarantines_without_selecting_expired_skill(self):
        expiring = _entry(
            "workflow.expiring",
            "workflow",
            [{"kind": "operation", "ref": "operation://code-change"}],
            status="active",
            expires_at=OBSERVED_AT,
        )
        catalog = _catalog([expiring])

        outcome = self._resolve(catalog)

        self.assertEqual(outcome.decision["disposition"], "quarantined")
        self.assertEqual(outcome.decision["reason_code"], "expired-applicable-skill")
        self.assertEqual(outcome.manifest_set_skill_ids, ())

    def test_canonical_replay_is_order_independent_and_inputs_are_not_mutated(self):
        from context_control_plane.skill_resolver import (
            canonical_skill_resolution_decision_bytes,
            canonical_skill_resolution_request_bytes,
        )

        catalog = _base_catalog()
        request = _request(catalog)
        catalog_before = copy.deepcopy(catalog)
        request_before = copy.deepcopy(request)
        first = self._resolve(catalog, request)

        shuffled = copy.deepcopy(catalog)
        random.Random(409).shuffle(shuffled["entries"])
        reordered_request = _request(
            shuffled,
            path_refs=list(reversed(request["path_refs"])),
            required_schema_refs=list(reversed(request["required_schema_refs"])),
        )
        second = self._resolve(shuffled, reordered_request)

        self.assertEqual(catalog, catalog_before)
        self.assertEqual(request, request_before)
        self.assertEqual(
            canonical_skill_resolution_request_bytes(request),
            canonical_skill_resolution_request_bytes(reordered_request),
        )
        self.assertEqual(
            canonical_skill_resolution_decision_bytes(first.decision),
            canonical_skill_resolution_decision_bytes(second.decision),
        )

    def test_runtime_and_schema_reject_dynamic_state_and_authority_claims(self):
        from context_control_plane.skill_resolver import (
            SkillResolverError,
            validate_skill_resolution_decision,
            validate_skill_resolution_request,
        )

        catalog = _base_catalog()
        request = _request(catalog)
        outcome = self._resolve(catalog, request)
        request_schema = json.loads(
            (self.schema_dir / "skill-resolution-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        decision_schema = json.loads(
            (self.schema_dir / "skill-resolution-decision.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            list(Draft202012Validator(request_schema).iter_errors(request)), []
        )
        self.assertEqual(
            list(
                Draft202012Validator(decision_schema).iter_errors(outcome.decision)
            ),
            [],
        )
        for field, value in (
            ("active_task_id", "work-m4-09"),
            ("claim_id", "claim-m4-09"),
            ("owner", "actor://executor"),
            ("revision", 49),
        ):
            changed = copy.deepcopy(request)
            changed[field] = value
            self.assertTrue(
                list(Draft202012Validator(request_schema).iter_errors(changed))
            )
            with self.assertRaisesRegex(SkillResolverError, "fields"):
                validate_skill_resolution_request(changed)

        changed = copy.deepcopy(outcome.decision)
        changed["state_write_authority"] = True
        self.assertTrue(
            list(Draft202012Validator(decision_schema).iter_errors(changed))
        )
        with self.assertRaisesRegex(SkillResolverError, "authority"):
            validate_skill_resolution_decision(changed)


if __name__ == "__main__":
    unittest.main()
