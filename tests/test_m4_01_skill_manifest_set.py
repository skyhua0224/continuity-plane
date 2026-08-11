import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


WIRE_VERSION = "context.skill-manifest-set/v1alpha1"
SCHEMA_RELATIVE_PATH = "schemas/m4-01/skill-manifest-set.schema.json"
FIXTURE_RELATIVE_PATH = (
    "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
)
SPDX_SNAPSHOT_RELATIVE_PATH = "schemas/m4-01/spdx-license-ids-3.28.0.json"
SPDX_SOURCE_REVISION = "c4a7237ec8f4654e867546f9f409749300f1bf4c"
SPDX_SOURCE_SHA256 = "f728c534d8bd1044fc515a2ddb2292be99559021d830bfa3281be0bcd36302ee"
ARTIFACT_LICENSE_REF = "artifact://sha256/" + "a" * 64
ARTIFACT_PROVENANCE_REF = "artifact://sha256/" + "b" * 64
_EXACT_VERSION = object()


def _version_range(
    minimum,
    maximum=_EXACT_VERSION,
    *,
    minimum_inclusive=True,
    maximum_inclusive=True,
):
    if maximum is _EXACT_VERSION:
        maximum = minimum
    return {
        "minimum": minimum,
        "minimum_inclusive": minimum_inclusive,
        "maximum": maximum,
        "maximum_inclusive": maximum_inclusive,
    }


def _dependency(skill_id, minimum, maximum=_EXACT_VERSION, **range_options):
    return {
        "skill_id": skill_id,
        "version_range": _version_range(
            minimum,
            maximum,
            **range_options,
        ),
    }


def _conflict(skill_id, minimum, maximum=_EXACT_VERSION, **range_options):
    return _dependency(skill_id, minimum, maximum, **range_options)


class M401SkillManifestSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = cls.root / SCHEMA_RELATIVE_PATH
        cls.fixture_path = cls.root / FIXTURE_RELATIVE_PATH
        cls.spdx_snapshot_path = cls.root / SPDX_SNAPSHOT_RELATIVE_PATH
        try:
            cls.api = importlib.import_module(
                "context_control_plane.skill_manifest_set"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "skill_manifest_set module is missing")
        return self.api

    @staticmethod
    def _manifest(
        skill_id,
        *,
        version="1.0.0",
        source_kind="builtin",
        license_ref="MIT",
        applicability=None,
        rule_ids=None,
        dependencies=None,
        conflicts=None,
        compatibility=None,
        provenance_refs=None,
        status="approved",
        expires_at=None,
    ):
        return {
            "skill_id": skill_id,
            "version": version,
            "content_sha256": hashlib.sha256(skill_id.encode("utf-8")).hexdigest(),
            "source_kind": source_kind,
            "license_ref": license_ref,
            "applicability": applicability
            if applicability is not None
            else [
                {"kind": "operation", "ref": "operation://code-change"},
                {"kind": "provider", "ref": "provider://codex"},
            ],
            "rule_ids": rule_ids
            if rule_ids is not None
            else [f"rule.{skill_id}.main"],
            "dependencies": dependencies if dependencies is not None else [],
            "conflicts": conflicts if conflicts is not None else [],
            "expires_at": expires_at,
            "compatibility": compatibility
            if compatibility is not None
            else {
                "schema_refs": ["context.execution-packet/v1alpha1"],
                "provider_contract_refs": ["provider://codex/v1"],
            },
            "provenance_refs": provenance_refs
            if provenance_refs is not None
            else [ARTIFACT_PROVENANCE_REF],
            "status": status,
        }

    def _valid_document(self):
        return {
            "schema_version": WIRE_VERSION,
            "manifests": [
                self._manifest("core.recovery"),
                self._manifest(
                    "project.testing",
                    dependencies=[_dependency("core.recovery", "1.0.0")],
                ),
            ],
        }

    def _standalone_document(self):
        document = self._valid_document()
        document["manifests"] = document["manifests"][:1]
        return document

    def _rich_document(self):
        return {
            "schema_version": WIRE_VERSION,
            "manifests": [
                self._manifest(
                    "project.testing",
                    source_kind="project",
                    license_ref=ARTIFACT_LICENSE_REF,
                    applicability=[
                        {"kind": "repo", "ref": "repo://context-control-plane"},
                        {"kind": "path", "ref": "path://tests"},
                        {"kind": "operation", "ref": "operation://code-change"},
                    ],
                    rule_ids=[
                        "rule.project.testing.verify",
                        "rule.project.testing.red-green",
                    ],
                    dependencies=[
                        _dependency(
                            "external.audit",
                            "2.0.0",
                            "3.0.0",
                            maximum_inclusive=False,
                        ),
                        _dependency("core.recovery", "1.0.0"),
                    ],
                    conflicts=[
                        _conflict("legacy.testing", "1.0.0", "1.9.9"),
                        _conflict("unsafe.testing", "2.0.0"),
                    ],
                    compatibility={
                        "schema_refs": [
                            "context.typed-state/v1alpha1",
                            "context.execution-packet/v1alpha1",
                        ],
                        "provider_contract_refs": [
                            "provider://claude-code/v1",
                            "provider://codex/v1",
                        ],
                    },
                    provenance_refs=[
                        ARTIFACT_PROVENANCE_REF,
                        "artifact://sha256/" + "c" * 64,
                    ],
                ),
                self._manifest(
                    "core.recovery",
                    applicability=[
                        {"kind": "provider", "ref": "provider://codex"},
                        {"kind": "role", "ref": "role://executor"},
                        {"kind": "task", "ref": "task://code-change"},
                    ],
                    rule_ids=[
                        "rule.core.recovery.canary",
                        "rule.core.recovery.checkpoint",
                    ],
                ),
                self._manifest(
                    "external.audit",
                    version="2.1.0",
                    source_kind="external",
                    license_ref=ARTIFACT_LICENSE_REF,
                    applicability=[
                        {"kind": "project", "ref": "project://demo"},
                        {"kind": "provider", "ref": "provider://claude-code"},
                    ],
                    rule_ids=[
                        "rule.external.audit.provenance",
                        "rule.external.audit.scope",
                    ],
                    compatibility={
                        "schema_refs": ["context.execution-packet/v1alpha1"],
                        "provider_contract_refs": ["provider://claude-code/v1"],
                    },
                ),
            ],
        }

    def _validate(self, document, *, observed_at="2026-08-10T08:00:00+08:00"):
        api = self._require_api()
        return api.validate_skill_manifest_set(document, observed_at=observed_at)

    def _assert_invalid(self, document, pattern, *, observed_at="2026-08-10T08:00:00+08:00"):
        api = self._require_api()
        with self.assertRaisesRegex(api.SkillManifestSetError, pattern):
            self._validate(document, observed_at=observed_at)

    def test_module_and_strict_schema_are_contract_surfaces(self):
        api = self._require_api()
        self.assertEqual(api.SCHEMA_VERSION, WIRE_VERSION)
        self.assertTrue(self.schema_path.is_file())
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema.get("additionalProperties", True))
        self.assertEqual(schema["$defs"]["manifest"]["additionalProperties"], False)

    def test_strict_wire_is_registered_without_replacing_metadata_catalog(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        catalog = next(
            entry
            for entry in registry["schemas"]
            if entry["schema_id"] == "context.skill-catalog"
        )
        manifest_set = next(
            entry
            for entry in registry["schemas"]
            if entry["schema_id"] == "context.skill-manifest-set"
        )

        self.assertEqual(
            catalog["current_wire_version"], "context.skill-catalog/v1alpha1"
        )
        self.assertEqual(catalog["artifact_path"], "profiles/skill-catalog.example.yaml")
        self.assertEqual(manifest_set["current_wire_version"], WIRE_VERSION)
        self.assertEqual(manifest_set["artifact_path"], SCHEMA_RELATIVE_PATH)
        self.assertEqual(manifest_set["compatibility_mode"], "strict-versioned")
        self.assertNotEqual(manifest_set["schema_id"], catalog["schema_id"])
        self.assertEqual(
            manifest_set["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )

    def test_valid_set_round_trips_through_json_schema_and_runtime_validator(self):
        document = self._valid_document()
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(document))
        self.assertEqual(errors, [])
        self._validate(document)

    def test_canonical_bytes_sort_all_set_like_fields_without_mutating_input(self):
        api = self._require_api()
        document = self._rich_document()
        original = copy.deepcopy(document)
        reversed_document = copy.deepcopy(document)
        reversed_document["manifests"].reverse()
        for manifest in reversed_document["manifests"]:
            for field in (
                "applicability",
                "rule_ids",
                "dependencies",
                "conflicts",
                "provenance_refs",
            ):
                manifest[field].reverse()
            manifest["compatibility"]["schema_refs"].reverse()
            manifest["compatibility"]["provider_contract_refs"].reverse()
        self.assertEqual(
            api.canonical_skill_manifest_set_bytes(document),
            api.canonical_skill_manifest_set_bytes(reversed_document),
        )
        self.assertEqual(document, original)
        self.assertEqual(reversed_document["manifests"][0]["skill_id"], "external.audit")

    def test_versioned_fixture_is_valid_canonical_and_byte_stable(self):
        api = self._require_api()
        self.assertTrue(self.fixture_path.is_file())
        fixture_bytes = self.fixture_path.read_bytes()
        fixture = json.loads(fixture_bytes)
        self._validate(fixture)
        self.assertEqual(
            fixture_bytes,
            api.canonical_skill_manifest_set_bytes(fixture) + b"\n",
        )

    def test_five_source_kinds_and_declared_statuses_are_strict(self):
        for source_kind in ("builtin", "external", "project", "user", "workflow"):
            with self.subTest(source_kind=source_kind):
                document = self._valid_document()
                document["manifests"][0]["source_kind"] = source_kind
                self._validate(document)

        for status in (
            "proposed",
            "approved",
            "active",
            "quarantined",
            "deprecated",
            "rejected",
        ):
            with self.subTest(status=status):
                document = self._standalone_document()
                document["manifests"][0]["status"] = status
                self._validate(document)

        document = self._valid_document()
        for field, value, pattern in (
            ("source_kind", "unknown", "source_kind"),
            ("content_sha256", "A" * 64, "content_sha256"),
            ("status", "enabled", "status"),
        ):
            broken = copy.deepcopy(document)
            broken["manifests"][0][field] = value
            self._assert_invalid(broken, pattern)

    def test_license_ref_accepts_spdx_and_artifact_refs(self):
        for license_ref in (
            "MIT",
            "Apache-2.0",
            ARTIFACT_LICENSE_REF,
        ):
            with self.subTest(license_ref=license_ref):
                document = self._valid_document()
                document["manifests"][0]["license_ref"] = license_ref
                self._validate(document)

        for license_ref in (
            "",
            "   ",
            "https://example.invalid/project/LICENSE",
            "http://example.invalid/LICENSE",
            "license text with spaces",
            "artifact://",
            "artifact://licenses/project-skill",
            "DefinitelyNotSPDX",
            "Proprietary",
            "Not-A-Real-SPDX-License",
        ):
            with self.subTest(license_ref=license_ref):
                broken = self._valid_document()
                broken["manifests"][0]["license_ref"] = license_ref
                self._assert_invalid(broken, "license_ref")

    def test_spdx_ids_are_bound_to_a_versioned_official_snapshot(self):
        api = self._require_api()
        self.assertEqual(api.SPDX_LICENSE_LIST_VERSION, "3.28.0")
        self.assertTrue(self.spdx_snapshot_path.is_file())
        snapshot = json.loads(self.spdx_snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["license_list_version"], "3.28.0")
        self.assertEqual(snapshot["source_revision"], SPDX_SOURCE_REVISION)
        self.assertEqual(snapshot["source_sha256"], SPDX_SOURCE_SHA256)
        self.assertEqual(snapshot["license_ids"], sorted(set(snapshot["license_ids"])))
        self.assertIn("MIT", snapshot["license_ids"])
        self.assertIn("GPL-3.0-only", snapshot["license_ids"])
        self.assertNotIn("DefinitelyNotSPDX", snapshot["license_ids"])

        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        snapshot_entry = next(
            entry
            for entry in registry["schemas"]
            if entry["schema_id"] == "context.spdx-license-id-snapshot"
        )
        self.assertEqual(
            snapshot_entry["current_wire_version"],
            "context.spdx-license-id-snapshot/v1alpha1",
        )
        self.assertEqual(snapshot_entry["artifact_path"], SPDX_SNAPSHOT_RELATIVE_PATH)
        self.assertEqual(
            snapshot_entry["content_sha256"],
            hashlib.sha256(self.spdx_snapshot_path.read_bytes()).hexdigest(),
        )

        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$defs"]["spdxLicenseId"]["enum"],
            snapshot["license_ids"],
        )

    def test_semver_rejects_leading_zero_prerelease_and_bad_dependency_range(self):
        invalid = self._valid_document()
        invalid["manifests"][0]["version"] = "1.0.0-01"
        self._assert_invalid(invalid, "version|SemVer")

        mismatch = self._valid_document()
        mismatch["manifests"][1]["dependencies"][0]["version_range"] = (
            _version_range("1.0.1")
        )
        self._assert_invalid(mismatch, "dependency.*range|version.*range")

        malformed = self._valid_document()
        malformed["manifests"][1]["dependencies"][0]["version_range"][
            "minimum"
        ] = "1.0.0-01"
        self._assert_invalid(malformed, "dependency.*range|SemVer|version")

        oversized = self._standalone_document()
        oversized["manifests"][0]["version"] = "1.0.0-" + "9" * 5000
        self._assert_invalid(oversized, "version|SemVer|length")
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(oversized)))

    def test_version_range_rejects_empty_and_reversed_intervals(self):
        for version_range in (
            _version_range(
                "1.0.0",
                "1.0.0",
                maximum_inclusive=False,
            ),
            _version_range("2.0.0", "1.0.0"),
        ):
            with self.subTest(version_range=version_range):
                document = self._valid_document()
                document["manifests"][1]["dependencies"][0][
                    "version_range"
                ] = version_range
                self._assert_invalid(document, "version.*range|empty|reversed")

    def test_schema_and_runtime_reject_invalid_unbounded_range_flags(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        invalid_ranges = (
            _version_range(
                None,
                None,
                minimum_inclusive=False,
                maximum_inclusive=False,
            ),
            _version_range(
                None,
                "2.0.0",
                minimum_inclusive=True,
            ),
            _version_range(
                "1.0.0",
                None,
                maximum_inclusive=True,
            ),
        )
        for version_range in invalid_ranges:
            with self.subTest(version_range=version_range):
                document = self._valid_document()
                document["manifests"][1]["dependencies"][0][
                    "version_range"
                ] = version_range
                self._assert_invalid(document, "version.*range|inclusive|unbounded")
                self.assertTrue(list(validator.iter_errors(document)))

    def test_version_range_accepts_deterministic_unbounded_interval(self):
        document = self._valid_document()
        document["manifests"][1]["dependencies"][0]["version_range"] = (
            _version_range(
                "1.0.0",
                None,
                maximum_inclusive=False,
            )
        )
        self._validate(document)

    def test_prerelease_lower_unbounded_and_expiry_equality_boundaries(self):
        precedence = (
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        )
        for lower, higher in zip(precedence, precedence[1:], strict=False):
            with self.subTest(lower=lower, higher=higher):
                accepted = self._valid_document()
                accepted["manifests"][0]["version"] = higher
                accepted["manifests"][1]["dependencies"][0][
                    "version_range"
                ] = _version_range(
                    lower,
                    None,
                    maximum_inclusive=False,
                )
                self._validate(accepted)

                rejected = self._valid_document()
                rejected["manifests"][0]["version"] = lower
                rejected["manifests"][1]["dependencies"][0][
                    "version_range"
                ] = _version_range(
                    higher,
                    None,
                    maximum_inclusive=False,
                )
                self._assert_invalid(rejected, "dependency.*range|version.*range")

        lower_unbounded = self._valid_document()
        lower_unbounded["manifests"][1]["dependencies"][0][
            "version_range"
        ] = _version_range(
            None,
            "1.0.0",
            minimum_inclusive=False,
        )
        self._validate(lower_unbounded)

        equality = self._standalone_document()
        equality["manifests"][0]["expires_at"] = "2026-08-10T08:00:00+08:00"
        self._assert_invalid(
            equality,
            "expired",
            observed_at="2026-08-10T08:00:00+08:00",
        )

    def test_all_seven_typed_applicability_kinds_are_accepted(self):
        document = self._valid_document()
        document["manifests"][0]["applicability"] = [
            {"kind": "task", "ref": "task://code-change"},
            {"kind": "project", "ref": "project://demo"},
            {"kind": "repo", "ref": "repo://context-control-plane"},
            {"kind": "path", "ref": "path://context_control_plane"},
            {"kind": "operation", "ref": "operation://code-change"},
            {"kind": "role", "ref": "role://verifier"},
            {"kind": "provider", "ref": "provider://codex"},
        ]
        self._validate(document)

    def test_typed_applicability_rejects_raw_refs_and_unknown_kinds(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for value in (
            {"kind": "path", "ref": "src/context"},
            {"kind": "unknown", "ref": "unknown://value"},
            {"kind": "operation", "ref": "operation://"},
            {"kind": "task", "ref": "task://code change"},
        ):
            with self.subTest(value=value):
                broken = self._valid_document()
                broken["manifests"][0]["applicability"] = [value]
                self._assert_invalid(broken, "applicability")
                self.assertTrue(list(validator.iter_errors(broken)))

    def test_typed_applicability_rejects_dynamic_state_and_path_traversal(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for value in (
            {
                "kind": "task",
                "ref": "task://code-change?owner=alice&revision=17",
            },
            {"kind": "path", "ref": "path://src/../secrets"},
            {"kind": "role", "ref": "role://executor#claim-12"},
        ):
            with self.subTest(value=value):
                broken = self._valid_document()
                broken["manifests"][0]["applicability"] = [value]
                self._assert_invalid(broken, "applicability")
                self.assertTrue(list(validator.iter_errors(broken)))

    def test_dynamic_task_state_fields_are_rejected(self):
        for field in (
            "active_task_id",
            "idea_id",
            "owner",
            "claim_id",
            "revision",
            "blocker",
            "checkpoint_id",
            "current_evidence",
        ):
            broken = self._valid_document()
            broken["manifests"][0][field] = "dynamic-value"
            self._assert_invalid(broken, field)

    def test_skill_and_rule_ids_are_unique_within_the_selected_set(self):
        duplicate_skill = self._valid_document()
        duplicate_skill["manifests"][1]["skill_id"] = "core.recovery"
        self._assert_invalid(duplicate_skill, "skill_id.*unique|duplicate")

        duplicate_rule = self._valid_document()
        duplicate_rule["manifests"][1]["rule_ids"] = ["rule.core.recovery.main"]
        self._assert_invalid(duplicate_rule, "rule_id.*unique|duplicate")

    def test_manifest_and_rule_collections_are_nonempty_and_ids_are_stable(self):
        empty_set = self._valid_document()
        empty_set["manifests"] = []
        self._assert_invalid(empty_set, "manifests|non-empty")

        empty_rules = self._valid_document()
        empty_rules["manifests"][0]["rule_ids"] = []
        self._assert_invalid(empty_rules, "rule_ids|non-empty")

        for field, value in (
            ("skill_id", "Core Recovery"),
            ("skill_id", "core@recovery"),
            ("rule_ids", ["Rule.core.recovery"]),
            ("rule_ids", ["rule core recovery"]),
        ):
            with self.subTest(field=field, value=value):
                broken = self._valid_document()
                broken["manifests"][0][field] = value
                self._assert_invalid(broken, "skill_id|rule_ids|stable")

    def test_skill_and_rule_ids_reject_path_traversal_segments(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for field, value in (
            ("skill_id", "core/../recovery"),
            ("skill_id", "core/./recovery"),
            ("rule_ids", ["rule.core/../recovery"]),
            ("rule_ids", ["rule.core/./recovery"]),
        ):
            with self.subTest(field=field, value=value):
                broken = self._standalone_document()
                broken["manifests"][0][field] = value
                self._assert_invalid(broken, "skill_id|rule_ids|stable")
                self.assertTrue(list(validator.iter_errors(broken)))

    def test_dependency_closure_rejects_missing_self_and_cycles(self):
        missing = self._valid_document()
        missing["manifests"][1]["dependencies"] = [
            _dependency("missing.skill", "1.0.0")
        ]
        self._assert_invalid(missing, "missing dependency")

        self_dependency = self._valid_document()
        self_dependency["manifests"][0]["dependencies"] = [
            _dependency("core.recovery", "1.0.0")
        ]
        self._assert_invalid(self_dependency, "self dependency")

        cycle = self._valid_document()
        cycle["manifests"][0]["dependencies"] = [
            _dependency("project.testing", "1.0.0")
        ]
        self._assert_invalid(cycle, "cycle")

    def test_dependency_and_conflict_targets_are_unique_per_manifest(self):
        duplicate_dependency = self._valid_document()
        duplicate_dependency["manifests"][1]["dependencies"].append(
            _dependency("core.recovery", "0.9.0", "1.1.0")
        )
        self._assert_invalid(duplicate_dependency, "dependencies.*unique|duplicate")

        duplicate_conflict = self._valid_document()
        duplicate_conflict["manifests"][0]["conflicts"] = [
            _conflict("external.unloaded", "1.0.0"),
            _conflict("external.unloaded", "2.0.0"),
        ]
        self._assert_invalid(duplicate_conflict, "conflicts.*unique|duplicate")

    def test_selected_manifest_dependencies_must_also_be_selected(self):
        for dependency_status in (
            "proposed",
            "quarantined",
            "deprecated",
            "rejected",
        ):
            with self.subTest(dependency_status=dependency_status):
                document = self._valid_document()
                document["manifests"][0]["status"] = dependency_status
                self._assert_invalid(
                    document,
                    "dependency.*selected|selected.*dependency|status",
                )

    def test_conflict_graph_rejects_selected_and_self_conflicts(self):
        selected = self._valid_document()
        selected["manifests"][0]["conflicts"] = [
            _conflict("project.testing", "1.0.0")
        ]
        self._assert_invalid(selected, "conflict")

        self_conflict = self._valid_document()
        self_conflict["manifests"][0]["conflicts"] = [
            _conflict("core.recovery", "2.0.0")
        ]
        self._assert_invalid(self_conflict, "self conflict")

    def test_conflict_range_only_rejects_selected_matching_version(self):
        matching = self._valid_document()
        matching["manifests"][0]["conflicts"] = [
            _conflict(
                "project.testing",
                "0.9.0",
                "2.0.0",
                maximum_inclusive=False,
            )
        ]
        self._assert_invalid(matching, "conflict")

        outside = self._valid_document()
        outside["manifests"][0]["conflicts"] = [
            _conflict(
                "project.testing",
                "2.0.0",
                "3.0.0",
                maximum_inclusive=False,
            )
        ]
        self._validate(outside)

    def test_conflict_target_may_be_declared_without_being_selected(self):
        document = self._valid_document()
        document["manifests"][0]["conflicts"] = [
            _conflict("external.unloaded", "1.0.0", "2.0.0")
        ]
        self._validate(document)

    def test_proposed_can_expire_without_time_but_selected_statuses_require_trusted_time(self):
        proposed = self._standalone_document()
        proposed["manifests"][0]["status"] = "proposed"
        proposed["manifests"][0]["expires_at"] = "2026-08-01T00:00:00+08:00"
        self._validate(proposed, observed_at=None)

        for status in ("approved", "active"):
            with self.subTest(status=status):
                expired = self._valid_document()
                expired["manifests"][0]["status"] = status
                expired["manifests"][0]["expires_at"] = (
                    "2026-08-01T00:00:00+08:00"
                )
                self._assert_invalid(
                    expired,
                    "expired|observed_at",
                    observed_at=None,
                )
                self._assert_invalid(
                    expired,
                    "expired",
                    observed_at="2026-08-10T08:00:00+08:00",
                )

    def test_historical_quarantine_and_deprecated_manifests_can_retain_expired_content(self):
        for status in ("quarantined", "deprecated"):
            historical = self._standalone_document()
            historical["manifests"][0]["status"] = status
            historical["manifests"][0]["expires_at"] = "2026-08-01T00:00:00+08:00"
            self._validate(historical, observed_at="2026-08-10T08:00:00+08:00")

    def test_compatibility_and_provenance_are_typed_refs_and_strict(self):
        missing_provenance = self._valid_document()
        missing_provenance["manifests"][0]["provenance_refs"] = []
        self._assert_invalid(missing_provenance, "provenance")

        raw_compatibility = self._valid_document()
        raw_compatibility["manifests"][0]["compatibility"]["schema_refs"] = [
            "not-a-ref"
        ]
        self._assert_invalid(raw_compatibility, "compatibility")

        malformed_artifact = self._valid_document()
        malformed_artifact["manifests"][0]["provenance_refs"] = [
            "artifact://skills/not-content-addressed"
        ]
        self._assert_invalid(malformed_artifact, "provenance|artifact")

    def test_provenance_refs_reject_dynamic_state_queries(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for provenance_ref in (
            "evidence://current-work?owner=alice&revision=17",
            "evidence://current-work/owner-alice/revision-17",
        ):
            with self.subTest(provenance_ref=provenance_ref):
                broken = self._valid_document()
                broken["manifests"][0]["provenance_refs"] = [provenance_ref]
                self._assert_invalid(broken, "provenance")
                self.assertTrue(list(validator.iter_errors(broken)))

    def test_license_ref_rejects_dynamic_state_urls(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        broken = self._standalone_document()
        broken["manifests"][0]["license_ref"] = (
            "https://licenses.example.invalid/MIT?owner=alice&claim=claim-12"
        )
        self._assert_invalid(broken, "license_ref")
        self.assertTrue(list(validator.iter_errors(broken)))

    def test_provider_applicability_requires_a_matching_contract(self):
        document = self._standalone_document()
        document["manifests"][0]["compatibility"]["provider_contract_refs"] = [
            "provider://claude-code/v1"
        ]
        self._assert_invalid(document, "provider.*contract")

        document["manifests"][0]["compatibility"]["provider_contract_refs"] = []
        self._assert_invalid(document, "provider.*contract")

    def test_unknown_document_and_nested_fields_fail_closed(self):
        unknown_document = self._valid_document()
        unknown_document["generated_at"] = "2026-08-10T08:00:00+08:00"
        self._assert_invalid(unknown_document, "document|unknown|fields")

        unknown_nested = self._valid_document()
        unknown_nested["manifests"][0]["compatibility"]["host"] = "codex"
        self._assert_invalid(unknown_nested, "compatibility|fields")


if __name__ == "__main__":
    unittest.main()
