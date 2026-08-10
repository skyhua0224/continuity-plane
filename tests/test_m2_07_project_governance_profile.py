import copy
import hashlib
import importlib
import itertools
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError


WIRE_VERSION = "context.project-governance-profile/v1alpha1"
FIXTURE_VERSION = "context.project-governance-profile-fixtures/v1alpha1"
SCHEMA_RELATIVE_PATH = "schemas/m2-07/project-governance-profile.schema.json"
IMMUTABLE_ADAPTATION_FIELDS = (
    "profile_id",
    "version",
    "proposal_revision",
    "scope",
    "inputs",
    "applicability",
    "changes",
)


def _adaptation_sha256(adaptation):
    payload = {
        field: copy.deepcopy(adaptation[field])
        for field in IMMUTABLE_ADAPTATION_FIELDS
    }
    payload["inputs"] = sorted(payload["inputs"])
    payload["applicability"] = sorted(
        payload["applicability"],
        key=lambda item: (item["kind"], item["ref"]),
    )
    for field in (
        "common_path_refs",
        "common_command_refs",
        "verification_hint_refs",
        "skill_applicability",
    ):
        payload["changes"][field] = sorted(payload["changes"][field])
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class M207ProjectGovernanceProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = cls.root / SCHEMA_RELATIVE_PATH
        cls.fixture_path = (
            cls.root
            / "experiments"
            / "state"
            / "m2-07-project-governance-profile-fixtures.yaml"
        )
        try:
            cls.api = importlib.import_module(
                "context_control_plane.project_governance_profile"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(
            self.api,
            "context_control_plane.project_governance_profile is missing",
        )
        return self.api

    def _valid_document(self):
        adaptation = {
            "adaptation_id": "adaptation-demo-1",
            "profile_id": "profile-demo",
            "version": "1.0.0-alpha.1",
            "proposal_revision": 7,
            "content_sha256": "0" * 64,
            "scope": "project",
            "inputs": ["correction-demo-1", "run-demo-1"],
            "applicability": [
                {"kind": "operation", "ref": "operation://code-change"},
                {"kind": "project", "ref": "project-demo"},
            ],
            "changes": {
                "retrieval_order": ["rg", "lsp"],
                "common_path_refs": ["path://src"],
                "common_command_refs": ["command://test-unit"],
                "verification_hint_refs": ["verification://default"],
                "skill_applicability": ["skill://test-driven-development"],
                "presentation_preferences": {"response_density": "compact"},
            },
            "metrics_before": {
                "bytes_read": 4096,
                "bytes_emitted": 2048,
                "repeated_read_bytes": 1024,
                "verification_failures": 0,
            },
            "metrics_after": {
                "bytes_read": 2048,
                "bytes_emitted": 1024,
                "repeated_read_bytes": 0,
                "verification_failures": 0,
            },
            "safety_veto_results": {
                "E1": True,
                "E2": True,
                "E4": True,
                "E6": True,
                "E8": True,
                "E9": True,
            },
            "replay_receipts": [],
            "expiry": "2026-09-10T00:00:00+08:00",
            "rollback_to": None,
            "status": "candidate",
            "approval_round": 0,
            "approval_ref": None,
            "approved_at": None,
            "activation_revision": None,
        }
        adaptation["content_sha256"] = _adaptation_sha256(adaptation)
        return {
            "schema_version": WIRE_VERSION,
            "profile": {
                "profile_id": "profile-demo",
                "project_id": "project-demo",
                "revision": 8,
                "direction_state": "discovery",
                "governance_owner_mode": "single-owner",
                "execution_worker_mode": "multi-worker",
                "repository_topology": "monolith",
                "requested_runtime_profile": "local-embedded",
                "task_sources": [
                    "external-pm",
                    "issue-backed",
                    "master-workstream",
                    "state-native",
                ],
                "governance_ref": "governance://master",
                "updated_at": "2026-08-10T08:00:00+08:00",
            },
            "charters": [
                {
                    "charter_id": "charter-demo",
                    "project_id": "project-demo",
                    "profile_id": "profile-demo",
                    "status": "candidate",
                    "problem_space": "Durable provider-neutral project context.",
                    "intended_users": ["collaborator", "developer"],
                    "confirmed_constraint_refs": ["constraint://provider-neutral"],
                    "prohibited_side_effects": ["unapproved-authority-change"],
                    "current_evidence_refs": ["evidence://repository-current"],
                    "unknowns": ["team-scale-latency"],
                    "assumptions": ["local-embedded-default"],
                    "decision_owner_ref": "actor://owner",
                    "discovery_campaign_id": "work-discovery",
                    "attempt_budget": 3,
                    "expiry": "2026-09-10T00:00:00+08:00",
                    "return_point_work_id": "work-root",
                    "exit_criteria": ["profile-contract-validated"],
                    "mainline_authority": False,
                    "revision": 1,
                }
            ],
            "work_sources": [
                {
                    "work_source_id": "source-root",
                    "project_id": "project-demo",
                    "work_id": "work-root",
                    "source_kind": "state-native",
                    "source_ref": "opaque://work/root",
                    "source_revision": "revision-1",
                    "governance_parent_id": "work-discovery",
                    "dependency_ids": [],
                    "readiness": "active",
                },
                {
                    "work_source_id": "source-child",
                    "project_id": "project-demo",
                    "work_id": "work-child",
                    "source_kind": "issue-backed",
                    "source_ref": "opaque://issue/child",
                    "source_revision": "issue-revision-4",
                    "governance_parent_id": "work-discovery",
                    "dependency_ids": ["work-root"],
                    "readiness": "ready",
                },
            ],
            "obligations": [
                {
                    "obligation_id": "obligation-required",
                    "work_id": "work-root",
                    "mode": "required",
                    "condition_ref": None,
                    "authority": {
                        "kind": "project-governance",
                        "ref": "governance://master",
                    },
                    "automation_class": "autonomous",
                    "verification_profile_ref": "verification://default",
                    "evidence_refs": ["evidence://repository-current"],
                    "expires_at": None,
                    "status": "pending",
                    "revision": 1,
                },
                {
                    "obligation_id": "obligation-conditional",
                    "work_id": "work-child",
                    "mode": "conditional",
                    "condition_ref": "condition://ci-enabled",
                    "authority": {
                        "kind": "task-source",
                        "ref": "opaque://issue/child",
                    },
                    "automation_class": "approval-gated",
                    "verification_profile_ref": "verification://default",
                    "evidence_refs": [],
                    "expires_at": "2026-09-10T00:00:00+08:00",
                    "status": "pending",
                    "revision": 1,
                },
            ],
            "adaptations": [adaptation],
        }

    def _validate(self, document):
        api = self._require_api()
        return api.validate_project_governance_profile(
            document,
            observed_at="2026-08-10T08:00:00+08:00",
        )

    def _canonical(self, document):
        api = self._require_api()
        return api.canonical_project_governance_bytes(
            document,
            observed_at="2026-08-10T08:00:00+08:00",
        )

    def _assert_invalid(self, document, message_pattern=None):
        api = self._require_api()
        context = (
            self.assertRaisesRegex(api.ProjectGovernanceProfileError, message_pattern)
            if message_pattern
            else self.assertRaises(api.ProjectGovernanceProfileError)
        )
        with context:
            self._validate(document)

    def test_new_strict_wire_is_registered_without_replacing_integration_profile(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        existing = next(
            entry
            for entry in registry["schemas"]
            if entry["schema_id"] == "context.project-profile"
        )
        strict = next(
            entry
            for entry in registry["schemas"]
            if entry["schema_id"] == "context.project-governance-profile"
        )

        self.assertEqual(existing["current_wire_version"], "context.project/v1alpha1")
        self.assertEqual(
            existing["artifact_path"],
            "profiles/alkaidlab/project.example.yaml",
        )
        self.assertEqual(strict["current_wire_version"], WIRE_VERSION)
        self.assertEqual(strict["artifact_path"], SCHEMA_RELATIVE_PATH)
        self.assertEqual(strict["compatibility_mode"], "strict-versioned")
        self.assertNotEqual(strict["schema_id"], existing["schema_id"])

        schema_bytes = self.schema_path.read_bytes()
        self.assertEqual(
            strict["content_sha256"],
            hashlib.sha256(schema_bytes).hexdigest(),
        )

    def test_requested_runtime_profile_is_explicit_and_provider_neutral(self):
        for capability in (
            "local-embedded",
            "forge-coordinated",
            "local-coordinator",
            "shared-strong",
        ):
            with self.subTest(capability=capability):
                document = self._valid_document()
                document["profile"]["requested_runtime_profile"] = capability
                self._validate(document)

        invalid = self._valid_document()
        invalid["profile"]["requested_runtime_profile"] = "postgres-required"
        self._assert_invalid(invalid, "runtime|capability|profile")

    def test_requested_runtime_profile_cannot_claim_adapter_capabilities(self):
        document = self._valid_document()
        self._validate(document)

        overclaim = copy.deepcopy(document)
        overclaim["profile"]["runtime_capability_profile"] = "shared-strong"
        self._assert_invalid(overclaim, "runtime|capability|profile|unexpected")

    def test_malformed_enum_values_are_domain_errors(self):
        for field in (
            "direction_state",
            "governance_owner_mode",
            "execution_worker_mode",
            "repository_topology",
        ):
            for malformed in ([], {}):
                with self.subTest(field=field, malformed=type(malformed).__name__):
                    document = self._valid_document()
                    document["profile"][field] = malformed
                    self._assert_invalid(document, field)

    def test_semver_numeric_prerelease_identifiers_reject_leading_zeroes(self):
        document = self._valid_document()
        adaptation = document["adaptations"][0]
        adaptation["version"] = "1.0.0-01"
        adaptation["content_sha256"] = _adaptation_sha256(adaptation)

        self._assert_invalid(document, "semver|version|prerelease")
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema, format_checker=None).validate(document)

    def test_applicability_path_repo_and_operation_require_typed_refs(self):
        invalid_refs = {
            "path": "/home/alice/secret/project",
            "repo": "https://example.invalid/private/repo",
            "operation": "rm -rf /tmp/project",
        }
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=None)
        for kind, ref in invalid_refs.items():
            with self.subTest(kind=kind):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                if kind == "operation":
                    operation = next(
                        item
                        for item in adaptation["applicability"]
                        if item["kind"] == "operation"
                    )
                    operation["ref"] = ref
                else:
                    adaptation["applicability"].append({"kind": kind, "ref": ref})
                adaptation["content_sha256"] = _adaptation_sha256(adaptation)

                self._assert_invalid(document, f"{kind}|applicability|ref")
                with self.assertRaises(ValidationError):
                    validator.validate(document)

    def test_blank_strings_strict_timestamps_and_activation_revision_fail_closed(self):
        blank_ref = self._valid_document()
        blank_ref["profile"]["governance_ref"] = "   "
        blank_ref["obligations"][0]["authority"]["ref"] = "   "
        self._assert_invalid(blank_ref, "non-empty|string|blank")

        blank_list_item = self._valid_document()
        blank_list_item["charters"][0]["intended_users"] = ["   "]
        self._assert_invalid(blank_list_item, "non-empty|string|blank")

        loose_timestamp = self._valid_document()
        loose_timestamp["profile"]["updated_at"] = "2026-08-10 08:00:00+08:00"
        self._assert_invalid(loose_timestamp, "RFC3339|timestamp")

        zero_revision = self._valid_document()
        adaptation = zero_revision["adaptations"][0]
        adaptation.update(
            status="active",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=0,
        )
        self._assert_invalid(zero_revision, "activation|revision|positive")

        future_activation = self._valid_document()
        adaptation = future_activation["adaptations"][0]
        adaptation.update(
            status="active",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=9,
        )
        adaptation["content_sha256"] = _adaptation_sha256(adaptation)
        self._assert_invalid(future_activation, "activation|revision|profile")

        zero_proposal = self._valid_document()
        zero_proposal["adaptations"][0]["proposal_revision"] = 0
        self._assert_invalid(zero_proposal, "proposal|revision|positive")

        future_proposal = self._valid_document()
        future_proposal["adaptations"][0]["proposal_revision"] = 9
        future_proposal["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            future_proposal["adaptations"][0]
        )
        self._assert_invalid(future_proposal, "proposal|revision|profile")

    def test_current_validation_requires_a_trusted_observation_time(self):
        api = self._require_api()
        document = self._valid_document()
        with self.assertRaisesRegex(api.ProjectGovernanceProfileError, "observed_at"):
            api.canonical_project_governance_bytes(document)

        future_profile = self._valid_document()
        future_profile["profile"]["updated_at"] = "2026-08-11T08:00:00+08:00"
        self._assert_invalid(future_profile, "profile|updated|future|observed")

    def test_liveness_validation_requires_explicit_observed_time(self):
        document = self._valid_document()
        api = self._require_api()
        with self.assertRaisesRegex(api.ProjectGovernanceProfileError, "observed_at"):
            api.validate_project_governance_profile(document)
        with self.assertRaisesRegex(api.ProjectGovernanceProfileError, "observed_at"):
            api.canonical_project_governance_bytes(document)

        expired = self._valid_document()
        expired["charters"][0]["expiry"] = "2026-08-10T08:30:00+08:00"
        with self.assertRaisesRegex(api.ProjectGovernanceProfileError, "expiry|expired"):
            api.canonical_project_governance_bytes(
                expired,
                observed_at="2026-08-10T09:00:00+08:00",
            )

    def test_user_and_provider_adaptation_scopes_require_typed_binding(self):
        valid_user = self._valid_document()
        user_adaptation = valid_user["adaptations"][0]
        user_adaptation["scope"] = "user"
        user_adaptation["applicability"].append(
            {"kind": "user", "ref": "user://developer"}
        )
        user_adaptation["content_sha256"] = _adaptation_sha256(user_adaptation)
        self._validate(valid_user)

        missing_user = self._valid_document()
        missing_user["adaptations"][0]["scope"] = "user"
        missing_user["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            missing_user["adaptations"][0]
        )
        self._assert_invalid(missing_user, "user|scope|applicability")

        valid_provider = self._valid_document()
        provider_adaptation = valid_provider["adaptations"][0]
        provider_adaptation["scope"] = "provider-adapter"
        provider_adaptation["applicability"].append(
            {"kind": "provider", "ref": "provider://codex"}
        )
        provider_adaptation["content_sha256"] = _adaptation_sha256(
            provider_adaptation
        )
        self._validate(valid_provider)

        raw_provider = copy.deepcopy(valid_provider)
        provider_item = next(
            item
            for item in raw_provider["adaptations"][0]["applicability"]
            if item["kind"] == "provider"
        )
        provider_item["ref"] = "codex"
        raw_provider["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            raw_provider["adaptations"][0]
        )
        self._assert_invalid(raw_provider, "provider|typed|ref")

        cross_scope = self._valid_document()
        cross_scope["adaptations"][0]["applicability"].append(
            {"kind": "user", "ref": "user://developer"}
        )
        cross_scope["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            cross_scope["adaptations"][0]
        )
        self._assert_invalid(cross_scope, "user|scope")

    def test_typed_refs_expiry_and_obligation_authority_remain_bounded(self):
        raw_path = self._valid_document()
        raw_path["adaptations"][0]["changes"]["common_path_refs"] = ["/tmp/project"]
        raw_path["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            raw_path["adaptations"][0]
        )
        self._assert_invalid(raw_path, "path|ref")

        raw_verification = self._valid_document()
        raw_verification["adaptations"][0]["changes"]["verification_hint_refs"] = [
            "unit-tests"
        ]
        raw_verification["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            raw_verification["adaptations"][0]
        )
        self._assert_invalid(raw_verification, "verification|ref")

        raw_skill = self._valid_document()
        raw_skill["adaptations"][0]["changes"]["skill_applicability"] = ["tdd"]
        raw_skill["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            raw_skill["adaptations"][0]
        )
        self._assert_invalid(raw_skill, "skill|ref")

        source_required = self._valid_document()
        source_required["obligations"][1]["mode"] = "required"
        source_required["obligations"][1]["condition_ref"] = None
        self._assert_invalid(source_required, "task-source|required|authority")

        source_autonomous = self._valid_document()
        source_autonomous["obligations"][1]["mode"] = "optional"
        source_autonomous["obligations"][1]["condition_ref"] = None
        source_autonomous["obligations"][1]["automation_class"] = "autonomous"
        self._assert_invalid(source_autonomous, "task-source|autonomous|authority")

        expired_charter = self._valid_document()
        expired_charter["charters"][0]["expiry"] = "2026-08-09T08:00:00+08:00"
        self._assert_invalid(expired_charter, "charter|expiry|expired")

        for status in ("candidate", "shadow", "approved", "active"):
            with self.subTest(expired_adaptation=status):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                adaptation["status"] = status
                adaptation["expiry"] = "2026-08-09T08:00:00+08:00"
                if status in {"shadow", "approved", "active"}:
                    adaptation["replay_receipts"] = [
                        "artifact://sha256/" + "a" * 64
                    ]
                if status in {"approved", "active"}:
                    adaptation["approval_round"] = 1
                    adaptation["approval_ref"] = "approval://round-1"
                    adaptation["approved_at"] = "2026-08-08T08:00:00+08:00"
                if status == "active":
                    adaptation["activation_revision"] = 8
                adaptation["content_sha256"] = _adaptation_sha256(adaptation)
                self._assert_invalid(document, "adaptation|expiry|expired")

        for status in ("quarantined", "rejected", "superseded"):
            with self.subTest(terminal_adaptation=status):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                adaptation["status"] = status
                adaptation["expiry"] = "2026-08-09T08:00:00+08:00"
                adaptation["content_sha256"] = _adaptation_sha256(adaptation)
                self._validate(document)

        superseded_charter = self._valid_document()
        superseded_charter["charters"][0]["status"] = "superseded"
        superseded_charter["charters"][0]["expiry"] = (
            "2026-08-09T08:00:00+08:00"
        )
        self._validate(superseded_charter)

    def test_adaptation_scope_and_set_semantics_are_deterministic(self):
        wrong_project = self._valid_document()
        project_applicability = next(
            item
            for item in wrong_project["adaptations"][0]["applicability"]
            if item["kind"] == "project"
        )
        project_applicability["ref"] = "project-other"
        wrong_project["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            wrong_project["adaptations"][0]
        )
        self._assert_invalid(wrong_project, "applicability|project|profile")

        first = self._valid_document()
        second = copy.deepcopy(first)
        second["profile"]["task_sources"] = list(reversed(second["profile"]["task_sources"]))
        second["work_sources"] = list(reversed(second["work_sources"]))
        second["obligations"] = list(reversed(second["obligations"]))
        second["adaptations"][0]["inputs"] = list(reversed(second["adaptations"][0]["inputs"]))
        second["adaptations"][0]["applicability"] = list(
            reversed(second["adaptations"][0]["applicability"])
        )
        second["adaptations"][0]["content_sha256"] = first["adaptations"][0][
            "content_sha256"
        ]
        self._validate(first)
        self._validate(second)
        api = self._require_api()
        self.assertEqual(
            self._canonical(first),
            self._canonical(second),
        )

        ordered = self._valid_document()
        ordered["adaptations"][0]["changes"]["retrieval_order"] = ["lsp", "rg"]
        ordered["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            ordered["adaptations"][0]
        )
        self._validate(ordered)
        self.assertNotEqual(
            self._canonical(first),
            self._canonical(ordered),
        )

    def test_schema_and_all_governance_objects_are_strict(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], WIRE_VERSION)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for definition in (
            "projectProfile",
            "projectCharter",
            "workSource",
            "workObligation",
            "projectAdaptation",
            "adaptationChanges",
        ):
            with self.subTest(definition=definition):
                self.assertFalse(schema["$defs"][definition]["additionalProperties"])
                self.assertEqual(
                    set(schema["$defs"][definition]["required"]),
                    set(schema["$defs"][definition]["properties"]),
                )

        validator = Draft202012Validator(schema)
        document = self._valid_document()
        validator.validate(document)
        unknown = copy.deepcopy(document)
        unknown["profile"]["unexpected"] = True
        with self.assertRaises(ValidationError):
            validator.validate(unknown)

        self._validate(document)
        self._assert_invalid(unknown, "field|unknown|unexpected")

    def test_all_thirty_six_profile_axis_combinations_round_trip_canonically(self):
        api = self._require_api()
        combinations = itertools.product(
            ("discovery", "governed", "operational"),
            ("single-owner", "multi-owner"),
            ("single-worker", "multi-worker"),
            ("modular", "monolith", "mixed"),
        )
        observed = 0
        for direction, owner_mode, worker_mode, topology in combinations:
            with self.subTest(
                direction=direction,
                owner_mode=owner_mode,
                worker_mode=worker_mode,
                topology=topology,
            ):
                document = self._valid_document()
                profile = document["profile"]
                profile["direction_state"] = direction
                profile["governance_owner_mode"] = owner_mode
                profile["execution_worker_mode"] = worker_mode
                profile["repository_topology"] = topology
                if direction != "discovery":
                    document["charters"][0]["status"] = "approved"
                    document["charters"][0]["mainline_authority"] = True
                self._validate(document)
                canonical = self._canonical(document)
                reordered = dict(reversed(list(document.items())))
                self.assertEqual(
                    canonical,
                    self._canonical(reordered),
                )
                restored = json.loads(canonical)
                self.assertEqual(
                    self._canonical(restored),
                    canonical,
                )
                self.assertEqual(
                    api.round_trip_project_governance_profile(
                        document,
                        observed_at="2026-08-10T08:00:00+08:00",
                    ),
                    restored,
                )
                observed += 1
        self.assertEqual(observed, 36)

    def test_conditional_obligation_requires_condition_and_other_modes_forbid_it(self):
        valid_modes = {
            "required": None,
            "conditional": "condition://enabled",
            "optional": None,
        }
        for mode, condition_ref in valid_modes.items():
            with self.subTest(mode=mode):
                document = self._valid_document()
                obligation = document["obligations"][0]
                obligation["mode"] = mode
                obligation["condition_ref"] = condition_ref
                self._validate(document)

        missing_condition = self._valid_document()
        missing_condition["obligations"][1]["condition_ref"] = None
        self._assert_invalid(missing_condition, "condition")

        for mode in ("required", "optional"):
            document = self._valid_document()
            obligation = document["obligations"][0]
            obligation["mode"] = mode
            obligation["condition_ref"] = "condition://forbidden"
            self._assert_invalid(document, "condition")

    def test_pending_obligation_cannot_remain_live_after_expiry(self):
        expired_pending = self._valid_document()
        expired_pending["obligations"][1]["expires_at"] = (
            "2026-08-09T08:00:00+08:00"
        )
        self._assert_invalid(expired_pending, "obligation|expiry|expired|pending")

        for status in ("satisfied", "waived", "expired"):
            with self.subTest(status=status):
                historical = self._valid_document()
                historical["obligations"][1]["status"] = status
                if status == "satisfied":
                    historical["obligations"][1]["evidence_refs"] = [
                        "evidence://historical-completion"
                    ]
                elif status == "waived":
                    historical["obligations"][1]["evidence_refs"] = [
                        "approval://historical-waiver"
                    ]
                historical["obligations"][1]["expires_at"] = (
                    "2026-08-09T08:00:00+08:00"
                )
                self._validate(historical)

    def test_discovery_charter_cannot_claim_mainline_authority(self):
        document = self._valid_document()
        document["charters"][0]["mainline_authority"] = True
        self._assert_invalid(document, "mainline|discovery")

        document = self._valid_document()
        document["charters"][0]["attempt_budget"] = 0
        self._assert_invalid(document, "attempt|budget")

        document = self._valid_document()
        document["charters"][0]["expiry"] = None
        self._assert_invalid(document, "expiry")

    def test_charter_return_point_must_resolve_to_a_work_source(self):
        document = self._valid_document()
        document["charters"][0]["return_point_work_id"] = "work-missing"

        self._assert_invalid(document, "return|work|missing")

    def test_work_source_kind_must_be_enabled_and_dependencies_form_a_closed_dag(self):
        document = self._valid_document()
        document["profile"]["task_sources"].remove("issue-backed")
        self._assert_invalid(document, "source|task_sources")

        missing = self._valid_document()
        missing["work_sources"][1]["dependency_ids"] = ["work-missing"]
        self._assert_invalid(missing, "dependency|missing")

        self_dependency = self._valid_document()
        self_dependency["work_sources"][0]["dependency_ids"] = ["work-root"]
        self._assert_invalid(self_dependency, "dependency|self|cycle")

        cycle = self._valid_document()
        cycle["work_sources"][0]["dependency_ids"] = ["work-child"]
        self._assert_invalid(cycle, "cycle")

    def test_work_source_ref_must_remain_opaque_when_authority_matches(self):
        document = self._valid_document()
        source = document["work_sources"][1]
        source["source_ref"] = "https://example.invalid/issues/123"
        document["obligations"][1]["authority"]["ref"] = source["source_ref"]

        self._assert_invalid(document, "opaque|source.ref")

    def test_adaptation_lifecycle_preserves_payload_hash_and_requires_approval(self):
        statuses = ("candidate", "shadow", "approved", "active")
        content_hash = None
        for status in statuses:
            with self.subTest(status=status):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                adaptation["status"] = status
                if status in {"shadow", "approved", "active"}:
                    adaptation["replay_receipts"] = ["artifact://sha256/" + "a" * 64]
                if status in {"approved", "active"}:
                    adaptation["approval_round"] = 1
                    adaptation["approval_ref"] = "approval://round-1"
                    adaptation["approved_at"] = "2026-08-10T07:30:00+08:00"
                if status == "active":
                    adaptation["activation_revision"] = 8
                adaptation["content_sha256"] = _adaptation_sha256(adaptation)
                self._validate(document)
                if content_hash is None:
                    content_hash = adaptation["content_sha256"]
                self.assertEqual(adaptation["content_sha256"], content_hash)

        unapproved = self._valid_document()
        adaptation = unapproved["adaptations"][0]
        adaptation["status"] = "active"
        adaptation["replay_receipts"] = ["artifact://sha256/" + "a" * 64]
        adaptation["activation_revision"] = 8
        self._assert_invalid(unapproved, "approval")

        vetoed = self._valid_document()
        adaptation = vetoed["adaptations"][0]
        adaptation["status"] = "approved"
        adaptation["replay_receipts"] = ["artifact://sha256/" + "a" * 64]
        adaptation["approval_round"] = 1
        adaptation["approval_ref"] = "approval://round-1"
        adaptation["approved_at"] = "2026-08-10T07:30:00+08:00"
        adaptation["safety_veto_results"]["E8"] = False
        self._assert_invalid(vetoed, "veto|safety")

    def test_terminal_obligation_requires_evidence_approval_or_expiry_basis(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=None)
        invalid_cases = {
            "satisfied": {"status": "satisfied", "evidence_refs": []},
            "waived": {"status": "waived", "evidence_refs": []},
            "expired": {
                "status": "expired",
                "evidence_refs": [],
                "expires_at": None,
            },
        }
        for label, updates in invalid_cases.items():
            with self.subTest(label=label):
                document = self._valid_document()
                document["obligations"][0].update(updates)
                self._assert_invalid(
                    document,
                    "obligation|evidence|approval|expiry|expired",
                )
                with self.assertRaises(ValidationError):
                    validator.validate(document)

        satisfied = self._valid_document()
        satisfied["obligations"][0].update(
            status="satisfied",
            evidence_refs=["evidence://verification-complete"],
        )
        self._validate(satisfied)

        waived = self._valid_document()
        waived["obligations"][0].update(
            status="waived",
            evidence_refs=["approval://waiver-1"],
        )
        self._validate(waived)

        expired = self._valid_document()
        expired["obligations"][0].update(
            status="expired",
            evidence_refs=[],
            expires_at="2026-08-09T08:00:00+08:00",
        )
        self._validate(expired)

    def test_adaptation_rejects_tamper_expiry_bad_rollback_and_protected_settings(self):
        tampered = self._valid_document()
        tampered["adaptations"][0]["changes"]["retrieval_order"] = ["lsp", "rg"]
        self._assert_invalid(tampered, "hash|digest")

        expired = self._valid_document()
        adaptation = expired["adaptations"][0]
        adaptation["status"] = "active"
        adaptation["replay_receipts"] = ["artifact://sha256/" + "a" * 64]
        adaptation["approval_round"] = 1
        adaptation["approval_ref"] = "approval://round-1"
        adaptation["approved_at"] = "2026-08-01T08:30:00+08:00"
        adaptation["activation_revision"] = 8
        adaptation["expiry"] = "2026-08-09T08:00:00+08:00"
        self._assert_invalid(expired, "expiry|expired")

        rollback = self._valid_document()
        rollback["adaptations"][0]["rollback_to"] = "1.0.0-alpha.1"
        self._assert_invalid(rollback, "rollback")

        protected = self._valid_document()
        protected["adaptations"][0]["changes"]["authorization"] = "allow-all"
        protected["adaptations"][0]["content_sha256"] = _adaptation_sha256(
            protected["adaptations"][0]
        )
        self._assert_invalid(protected, "authorization|protected|field|unknown")

    def test_adaptation_common_commands_are_typed_refs_not_raw_commands(self):
        document = self._valid_document()
        adaptation = document["adaptations"][0]
        adaptation["changes"]["common_command_refs"] = ["rm -rf /"]
        adaptation["content_sha256"] = _adaptation_sha256(adaptation)

        self._assert_invalid(document, "command|ref")

    def test_adaptation_rollback_target_must_resolve_to_a_known_version(self):
        document = self._valid_document()
        document["adaptations"][0]["rollback_to"] = "9.9.9"

        self._assert_invalid(document, "rollback|target|version")

        forward = self._valid_document()
        previous = forward["adaptations"][0]
        current = copy.deepcopy(previous)
        current["adaptation_id"] = "adaptation-demo-forward"
        current["version"] = "0.9.0"
        current["rollback_to"] = previous["version"]
        current["content_sha256"] = _adaptation_sha256(current)
        forward["adaptations"].append(current)
        self._assert_invalid(forward, "rollback|earlier|forward")

        cycle = self._valid_document()
        first = cycle["adaptations"][0]
        first["rollback_to"] = "1.0.1"
        first["content_sha256"] = _adaptation_sha256(first)
        second = copy.deepcopy(first)
        second["adaptation_id"] = "adaptation-demo-cycle"
        second["version"] = "1.0.1"
        second["rollback_to"] = first["version"]
        second["content_sha256"] = _adaptation_sha256(second)
        cycle["adaptations"].append(second)
        self._assert_invalid(cycle, "rollback|cycle|earlier")

    def test_adaptation_rollback_stays_within_the_same_applicability_lane(self):
        same_lane = self._valid_document()
        previous = same_lane["adaptations"][0]
        previous.update(
            status="active",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=8,
        )
        current = copy.deepcopy(previous)
        current["adaptation_id"] = "adaptation-demo-2"
        current["version"] = "1.0.1"
        current["rollback_to"] = previous["version"]
        current.update(
            status="candidate",
            replay_receipts=[],
            approval_round=0,
            approval_ref=None,
            approved_at=None,
            activation_revision=None,
        )
        current["content_sha256"] = _adaptation_sha256(current)
        same_lane["adaptations"].append(current)
        self._validate(same_lane)

        cross_lane = self._valid_document()
        previous = cross_lane["adaptations"][0]
        current = copy.deepcopy(previous)
        current["adaptation_id"] = "adaptation-demo-user"
        current["version"] = "2.0.0"
        current["scope"] = "user"
        current["applicability"].append(
            {"kind": "user", "ref": "user://developer"}
        )
        current["rollback_to"] = previous["version"]
        current["content_sha256"] = _adaptation_sha256(current)
        cross_lane["adaptations"].append(current)
        self._assert_invalid(cross_lane, "rollback|scope|applicability|lane")

        candidate_target = self._valid_document()
        previous = candidate_target["adaptations"][0]
        current = copy.deepcopy(previous)
        current["adaptation_id"] = "adaptation-demo-2"
        current["version"] = "1.0.1"
        current["rollback_to"] = previous["version"]
        current["content_sha256"] = _adaptation_sha256(current)
        candidate_target["adaptations"].append(current)
        self._assert_invalid(candidate_target, "rollback|approved|historical|target")

        approved_but_unactivated = self._valid_document()
        previous = approved_but_unactivated["adaptations"][0]
        previous.update(
            status="approved",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
        )
        current = copy.deepcopy(previous)
        current["adaptation_id"] = "adaptation-demo-2"
        current["version"] = "1.0.1"
        current["rollback_to"] = previous["version"]
        current.update(
            status="candidate",
            replay_receipts=[],
            approval_round=0,
            approval_ref=None,
            approved_at=None,
        )
        current["content_sha256"] = _adaptation_sha256(current)
        approved_but_unactivated["adaptations"].append(current)
        self._assert_invalid(
            approved_but_unactivated,
            "rollback|activated|historical|target",
        )

        forward = self._valid_document()
        source = forward["adaptations"][0]
        source["version"] = "2.0.0"
        source["rollback_to"] = "3.0.0"
        source["content_sha256"] = _adaptation_sha256(source)
        target = copy.deepcopy(source)
        target["adaptation_id"] = "adaptation-demo-future"
        target["version"] = "3.0.0"
        target["rollback_to"] = None
        target["content_sha256"] = _adaptation_sha256(target)
        forward["adaptations"].append(target)
        self._assert_invalid(forward, "rollback|earlier|version|forward")

        cycle = self._valid_document()
        first = cycle["adaptations"][0]
        first["rollback_to"] = "1.0.1"
        second = copy.deepcopy(first)
        second["adaptation_id"] = "adaptation-demo-cycle"
        second["version"] = "1.0.1"
        second["rollback_to"] = first["version"]
        first["content_sha256"] = _adaptation_sha256(first)
        second["content_sha256"] = _adaptation_sha256(second)
        cycle["adaptations"].append(second)
        self._assert_invalid(cycle, "rollback|cycle|earlier|target")

    def test_approval_timestamp_cannot_be_future_or_outlive_expiry(self):
        future = self._valid_document()
        adaptation = future["adaptations"][0]
        adaptation.update(
            status="approved",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-11T08:00:00+08:00",
        )
        self._assert_invalid(future, "approval|future|observed")

        after_expiry = self._valid_document()
        adaptation = after_expiry["adaptations"][0]
        adaptation.update(
            status="approved",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-09-11T08:00:00+08:00",
            expiry="2026-09-10T00:00:00+08:00",
        )
        self._assert_invalid(after_expiry, "approval|expiry|future")

    def test_superseded_adaptation_preserves_prior_approval_provenance(self):
        document = self._valid_document()
        adaptation = document["adaptations"][0]
        adaptation.update(
            status="superseded",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=8,
        )

        self._validate(document)

    def test_active_adaptation_replays_at_and_after_its_activation_revision(self):
        document = self._valid_document()
        document["profile"]["revision"] = 8
        adaptation = document["adaptations"][0]
        adaptation.update(
            status="active",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=8,
        )

        self._validate(document)
        api = self._require_api()
        restored = api.round_trip_project_governance_profile(
            document,
            observed_at="2026-08-10T08:00:00+08:00",
        )
        self.assertEqual(restored["adaptations"][0]["activation_revision"], 8)

        restored["profile"]["revision"] = 9
        self._validate(restored)

    def test_nested_presentation_preferences_cannot_smuggle_protected_settings(self):
        for key in ("authorization", "authorization.mode", "Authorization"):
            with self.subTest(key=key):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                adaptation["changes"]["presentation_preferences"][key] = "allow-all"
                adaptation["content_sha256"] = _adaptation_sha256(adaptation)

                self._assert_invalid(
                    document,
                    "authorization|protected|preference",
                )

    def test_promoted_adaptation_requires_typed_replay_and_approval_refs(self):
        valid_receipt = "artifact://sha256/" + "a" * 64
        cases = {
            "replay receipt": {
                "replay_receipts": ["not-an-artifact"],
                "approval_ref": "approval://round-1",
            },
            "approval ref": {
                "replay_receipts": [valid_receipt],
                "approval_ref": "anything",
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                document = self._valid_document()
                adaptation = document["adaptations"][0]
                adaptation.update(
                    status="approved",
                    replay_receipts=overrides["replay_receipts"],
                    approval_round=1,
                    approval_ref=overrides["approval_ref"],
                    approved_at="2026-08-10T07:30:00+08:00",
                )
                self._assert_invalid(document, "replay|artifact|approval|ref")

    def test_governed_profile_has_only_one_current_mainline_charter(self):
        document = self._valid_document()
        document["profile"]["direction_state"] = "governed"
        current = document["charters"][0]
        current["status"] = "approved"
        current["mainline_authority"] = True
        duplicate = copy.deepcopy(current)
        duplicate["charter_id"] = "charter-demo-second"
        duplicate["revision"] = 2
        document["charters"].append(duplicate)

        self._assert_invalid(document, "charter|mainline|approved|current")

    def test_governance_obligation_authority_is_bound_to_profile_governance_ref(self):
        document = self._valid_document()
        document["obligations"][0]["authority"]["ref"] = (
            "governance://unrelated"
        )

        self._assert_invalid(document, "authority|governance|profile")

    def test_reordered_applicability_cannot_bypass_active_adaptation_uniqueness(self):
        document = self._valid_document()
        first = document["adaptations"][0]
        first.update(
            status="active",
            replay_receipts=["artifact://sha256/" + "a" * 64],
            approval_round=1,
            approval_ref="approval://round-1",
            approved_at="2026-08-10T07:30:00+08:00",
            activation_revision=8,
        )
        second = copy.deepcopy(first)
        second["adaptation_id"] = "adaptation-demo-2"
        second["version"] = "1.0.1"
        second["applicability"] = list(reversed(second["applicability"]))
        second["content_sha256"] = _adaptation_sha256(second)
        document["adaptations"].append(second)

        self._assert_invalid(document, "active|applicability|scope|unique")

    def test_versioned_fixture_round_trips_and_remains_provider_neutral(self):
        fixture_set = yaml.safe_load(self.fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture_set["schema_version"], FIXTURE_VERSION)
        self.assertGreaterEqual(len(fixture_set["cases"]), 1)
        serialized = self.fixture_path.read_text(encoding="utf-8").lower()
        for forbidden in ("alkaidlab", "projectcompute", "foundation sunshine"):
            self.assertNotIn(forbidden, serialized)

        api = self._require_api()
        for case in fixture_set["cases"]:
            with self.subTest(case=case["case_id"]):
                self._validate(case["document"])
                canonical = self._canonical(case["document"])
                restored = json.loads(canonical)
                self.assertEqual(
                    self._canonical(restored),
                    canonical,
                )
                self.assertEqual(
                    api.round_trip_project_governance_profile(
                        case["document"],
                        observed_at="2026-08-10T08:00:00+08:00",
                    ),
                    restored,
                )


if __name__ == "__main__":
    unittest.main()
