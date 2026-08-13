import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "context.skill-catalog/v1alpha1"
PROVENANCE_REF = "artifact://sha256/" + "b" * 64
APPROVAL_REF = "artifact://sha256/" + "c" * 64
VERIFICATION_REF = "artifact://sha256/" + "d" * 64


class M407SkillCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = cls.root / "schemas" / "m4-07" / "skill-catalog.schema.json"
        try:
            cls.api = importlib.import_module("context_control_plane.skill_catalog")
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "skill_catalog module is missing")
        return self.api

    @staticmethod
    def _manifest(skill_id, source_kind, *, status="proposed"):
        return {
            "skill_id": skill_id,
            "version": "1.0.0",
            "content_sha256": hashlib.sha256(skill_id.encode()).hexdigest(),
            "source_kind": source_kind,
            "license_ref": "MIT",
            "applicability": [{"kind": "operation", "ref": "operation://code-change"}],
            "rule_ids": [f"rule.{skill_id}.main"],
            "dependencies": [],
            "conflicts": [],
            "expires_at": None,
            "compatibility": {
                "schema_refs": ["context.compiled-skill-packet/v1alpha1"],
                "provider_contract_refs": ["provider://codex/v1"],
            },
            "provenance_refs": [PROVENANCE_REF],
            "status": status,
        }

    def _entry(
        self,
        skill_id="demo.core",
        source_kind="builtin",
        *,
        status="active",
        source_revision="1.0.0",
        manifest_status=None,
        approval_refs=None,
        verification_refs=None,
    ):
        if (
            source_revision == "1.0.0"
            and source_kind in {"external", "project", "user"}
            and status in {"approved", "active"}
        ):
            source_revision = "a" * 40
        if manifest_status is None:
            manifest_status = {
                "candidate": "proposed",
                "approved": "approved",
                "active": "active",
                "quarantined": "quarantined",
                "deprecated": "deprecated",
                "rejected": "rejected",
            }[status]
        manifest = self._manifest(skill_id, source_kind, status=manifest_status)
        return {
            "catalog_entry_id": f"catalog/{skill_id}",
            "source_kind": source_kind,
            "manifest": manifest,
            "source_url": (
                f"builtin://{skill_id}" if source_kind == "builtin" else
                f"workflow://{skill_id}" if source_kind == "workflow" else
                f"https://example.test/{skill_id}"
            ),
            "source_revision": source_revision,
            "source_path": "SKILL.md",
            "publisher": "context-control-plane",
            "license_ref": "MIT",
            "provenance_refs": [PROVENANCE_REF],
            "capabilities": ["read:repository"],
            "approval_refs": approval_refs if approval_refs is not None else (
                [] if status in {"candidate", "quarantined", "rejected"} else [APPROVAL_REF]
            ),
            "verification_refs": verification_refs if verification_refs is not None else (
                [] if status in {"candidate", "quarantined", "rejected"} else [VERIFICATION_REF]
            ),
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

    def _document(self, entries=None):
        return {
            "schema_version": SCHEMA_VERSION,
            "catalog_id": "skill-catalog/default",
            "catalog_revision": "2026-08-11T00:00:00Z",
            "entries": entries if entries is not None else [self._entry()],
        }

    def test_schema_and_runtime_module_are_present(self):
        api = self._require_api()
        self.assertTrue(self.schema_path.is_file())
        self.assertTrue(hasattr(api, "validate_skill_catalog"))

    def test_all_five_source_kinds_validate(self):
        api = self._require_api()
        entries = [
            self._entry("builtin.core", "builtin"),
            self._entry("external.audit", "external", status="approved"),
            self._entry("project.testing", "project", status="approved"),
            self._entry("user.preferences", "user", status="approved"),
            self._entry("workflow.executor", "workflow"),
        ]
        document = self._document(entries)
        api.validate_skill_catalog(document, observed_at="2026-08-11T00:00:00Z")

    def test_schema_accepts_the_canonical_document(self):
        self._require_api()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(self._document()))
        self.assertEqual(errors, [])

    def test_versioned_fixture_validates_and_round_trips(self):
        api = self._require_api()
        fixture_path = self.root / "experiments" / "skills" / "m4-07-skill-catalog-v1alpha1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        api.validate_skill_catalog(fixture)
        self.assertEqual(
            json.loads(api.canonical_skill_catalog_bytes(fixture)),
            fixture,
        )

    def test_canonical_bytes_are_order_independent(self):
        api = self._require_api()
        document = self._document([
            self._entry("z.last", "builtin"),
            self._entry("a.first", "builtin"),
        ])
        reversed_document = copy.deepcopy(document)
        reversed_document["entries"].reverse()
        self.assertEqual(
            api.canonical_skill_catalog_bytes(document),
            api.canonical_skill_catalog_bytes(reversed_document),
        )

    def test_active_external_without_approval_is_rejected(self):
        api = self._require_api()
        entry = self._entry("external.unapproved", "external", status="active", approval_refs=[])
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([entry]))

    def test_dynamic_source_revision_cannot_be_approved_or_active(self):
        api = self._require_api()
        for status in ("approved", "active"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                api.validate_skill_catalog(
                    self._document([
                        self._entry("external.dynamic", "external", status=status, source_revision="dynamic-index")
                    ])
                )

    def test_source_kind_and_manifest_kind_must_match(self):
        api = self._require_api()
        entry = self._entry("project.mismatch", "project", status="approved")
        entry["manifest"]["source_kind"] = "external"
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([entry]))

    def test_license_and_provenance_are_bound_to_manifest(self):
        api = self._require_api()
        entry = self._entry()
        entry["license_ref"] = "Apache-2.0"
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([entry]))
        entry = self._entry()
        entry["provenance_refs"] = []
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([entry]))

    def test_catalog_permissions_cannot_grant_authority(self):
        api = self._require_api()
        entry = self._entry()
        entry["permissions"]["state_write"] = True
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([entry]))

    def test_unapproved_entries_are_candidate_only_and_not_in_manifest_set(self):
        api = self._require_api()
        document = self._document([
            self._entry("builtin.core", "builtin"),
            self._entry("external.candidate", "external", status="candidate"),
        ])
        api.validate_skill_catalog(document)
        manifest_set = api.active_skill_manifest_set(document)
        self.assertEqual([item["skill_id"] for item in manifest_set["manifests"]], ["builtin.core"])

    def test_catalog_entry_binds_to_m4_06_lock_identity(self):
        api = self._require_api()
        entry = self._entry("builtin.core", "builtin")
        api.validate_skill_catalog(self._document([entry]))
        manifest_set = api.active_skill_manifest_set(self._document([entry]))
        from context_control_plane import compiled_skill_packet, skill_compatibility

        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["builtin.core"],
        )
        lock = skill_compatibility.create_skill_compatibility_lock(
            manifest_set,
            packet,
            task_id="task/catalog",
            operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1"],
        )
        api.validate_catalog_lock_binding(entry, lock)
        changed = copy.deepcopy(entry)
        changed["manifest"]["content_sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            api.validate_catalog_lock_binding(changed, lock)

    def test_catalog_lock_rejects_manifest_metadata_and_candidate_status_drift(self):
        api = self._require_api()
        entry = self._entry("external.audit", "external", status="approved")
        document = self._document([entry])
        manifest_set = api.active_skill_manifest_set(document)
        from context_control_plane import compiled_skill_packet, skill_compatibility

        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["external.audit"],
        )
        lock = skill_compatibility.create_skill_compatibility_lock(
            manifest_set,
            packet,
            task_id="task/catalog",
            operation_id="operation://code-change",
            provider_contract_refs=["provider://codex/v1"],
        )
        changed = copy.deepcopy(entry)
        changed["license_ref"] = "artifact://sha256/" + "e" * 64
        changed["manifest"]["license_ref"] = changed["license_ref"]
        with self.assertRaises(ValueError):
            api.validate_catalog_lock_binding(changed, lock)
        candidate = copy.deepcopy(entry)
        candidate["status"] = "candidate"
        candidate["manifest"]["status"] = "proposed"
        candidate["approval_refs"] = []
        candidate["verification_refs"] = []
        with self.assertRaises(ValueError):
            api.validate_catalog_lock_binding(candidate, lock)

    def test_schema_rejects_authority_permission_true(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        candidate = self._document()
        candidate["entries"][0]["permissions"]["state_write"] = True
        errors = list(Draft202012Validator(schema).iter_errors(candidate))
        self.assertTrue(errors)

    def test_approved_external_rejects_unpinned_revision_aliases(self):
        api = self._require_api()
        for revision in ("latest", "HEAD", "main", "refs/heads/main", "1.0.1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                api.validate_skill_catalog(
                    self._document([
                        self._entry(
                            "external.unpinned",
                            "external",
                            status="approved",
                            source_revision=revision,
                        )
                    ])
                )

    def test_catalog_canonicalization_sorts_nested_manifest_arrays(self):
        api = self._require_api()
        document = self._document()
        manifest = document["entries"][0]["manifest"]
        manifest["rule_ids"] = ["rule.demo.core.z", "rule.demo.core.a"]
        manifest["compatibility"]["schema_refs"] = [
            "context.typed-state/v1alpha1",
            "context.compiled-skill-packet/v1alpha1",
        ]
        manifest["compatibility"]["provider_contract_refs"] = [
            "provider://claude/v1",
            "provider://codex/v1",
        ]
        reversed_document = copy.deepcopy(document)
        reversed_manifest = reversed_document["entries"][0]["manifest"]
        reversed_manifest["rule_ids"].reverse()
        reversed_manifest["compatibility"]["schema_refs"].reverse()
        reversed_manifest["compatibility"]["provider_contract_refs"].reverse()
        self.assertEqual(
            api.canonical_skill_catalog_bytes(document),
            api.canonical_skill_catalog_bytes(reversed_document),
        )

    def test_windows_style_source_paths_are_rejected(self):
        api = self._require_api()
        for source_path in (r"..\SKILL.md", r"\tmp\SKILL.md"):
            with self.subTest(source_path=source_path), self.assertRaises(ValueError):
                candidate = self._document()
                candidate["entries"][0]["source_path"] = source_path
                api.validate_skill_catalog(candidate)

    def test_duplicate_catalog_ids_are_rejected(self):
        api = self._require_api()
        first = self._entry("same.id", "builtin")
        second = self._entry("same.id", "builtin")
        with self.assertRaises(ValueError):
            api.validate_skill_catalog(self._document([first, second]))


if __name__ == "__main__":
    unittest.main()
