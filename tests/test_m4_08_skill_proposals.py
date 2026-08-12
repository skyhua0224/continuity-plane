import copy
import hashlib
import importlib
import json
import time
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_VERSION = "context.skill-proposal/v1alpha1"
PROJECT_REF = "artifact://sha256/" + "a" * 64
VERIFY_REF = "artifact://sha256/" + "b" * 64
PREFERENCE_REF = "artifact://sha256/" + "c" * 64


class M408SkillProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = cls.root / "schemas" / "m4-08" / "skill-proposal.schema.json"
        cls.manifest_schema_path = (
            cls.root / "schemas" / "m4-01" / "skill-manifest-set.schema.json"
        )
        cls.schema = json.loads(cls.schema_path.read_text(encoding="utf-8"))
        manifest_schema = json.loads(
            cls.manifest_schema_path.read_text(encoding="utf-8")
        )
        registry = Registry().with_resource(
            manifest_schema["$id"], Resource.from_contents(manifest_schema)
        )
        cls.schema_validator = Draft202012Validator(
            cls.schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
        try:
            cls.api = importlib.import_module("context_control_plane.skill_proposal")
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "skill_proposal module is missing")
        return self.api

    @staticmethod
    def _project_facts():
        return {
            "repo_id": "demo-project",
            "topology": "monolith",
            "languages": ["python"],
            "build_commands": ["python -m compileall context_control_plane"],
            "test_commands": ["python -m unittest discover -s tests"],
            "source_refs": [PROJECT_REF],
        }

    @staticmethod
    def _verification_profile():
        return {
            "profile_id": "repository-default",
            "required_gates": ["compile", "test", "repository-verifier"],
            "generated_skill_license_ref": "MIT",
            "source_refs": [VERIFY_REF],
        }

    @staticmethod
    def _user_preferences():
        return {
            "preference_ids": ["pref.concise-status"],
            "tool_preferences": ["rg", "unittest"],
            "style_preferences": ["ascii-default"],
            "source_refs": [PREFERENCE_REF],
        }

    def _proposal(self, *, user_preferences=None, generated_at="2026-08-11T00:00:00Z"):
        api = self._require_api()
        return api.generate_skill_proposal(
            self._project_facts(),
            self._verification_profile(),
            self._user_preferences() if user_preferences is None else user_preferences,
            generator_version="1.0.0",
            generated_at=generated_at,
        )

    def test_schema_and_runtime_module_are_present(self):
        api = self._require_api()
        self.assertTrue(self.schema_path.is_file())
        self.assertTrue(hasattr(api, "generate_skill_proposal"))

    def test_generated_proposal_is_strictly_valid_and_candidate_only(self):
        api = self._require_api()
        proposal = self._proposal()
        api.validate_skill_proposal(proposal)
        self.assertEqual(proposal["schema_version"], SCHEMA_VERSION)
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["activation"]["status"], "not_active")
        self.assertTrue(all(item["status"] == "candidate" for item in proposal["candidates"]))
        self.assertTrue(all(item["manifest"]["status"] == "proposed" for item in proposal["candidates"]))

    def test_project_and_user_candidates_carry_input_provenance(self):
        api = self._require_api()
        proposal = self._proposal()
        expected_refs = {PROJECT_REF, VERIFY_REF, PREFERENCE_REF}
        self.assertEqual(set(proposal["input_refs"]), expected_refs)
        for candidate in proposal["candidates"]:
            self.assertTrue(expected_refs.issuperset(candidate["provenance_refs"]))
            self.assertEqual(candidate["permissions"]["state_write"], False)
            self.assertEqual(candidate["permissions"]["task_switch"], False)
        self.assertEqual(
            {candidate["source_kind"] for candidate in proposal["candidates"]},
            {"project", "user"},
        )
        api.validate_skill_proposal(proposal)

    def test_empty_user_preferences_do_not_create_user_skill(self):
        api = self._require_api()
        proposal = self._proposal(
            user_preferences={
                "preference_ids": [],
                "tool_preferences": [],
                "style_preferences": [],
                "source_refs": [],
            }
        )
        api.validate_skill_proposal(proposal)
        self.assertEqual(
            {candidate["source_kind"] for candidate in proposal["candidates"]},
            {"project"},
        )

    def test_same_bounded_inputs_replay_identically(self):
        api = self._require_api()
        first = self._proposal()
        second = self._proposal()
        self.assertEqual(api.canonical_skill_proposal_bytes(first), api.canonical_skill_proposal_bytes(second))
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])

    def test_reordered_set_like_inputs_replay_identically(self):
        api = self._require_api()
        facts = self._project_facts()
        facts["languages"] = ["python", "rust"]
        facts["source_refs"] = [PROJECT_REF, "artifact://sha256/" + "d" * 64]
        profile = self._verification_profile()
        profile["source_refs"] = [VERIFY_REF, "artifact://sha256/" + "e" * 64]
        preferences = self._user_preferences()

        first = api.generate_skill_proposal(
            facts,
            profile,
            preferences,
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        facts["languages"].reverse()
        facts["source_refs"].reverse()
        profile["required_gates"].reverse()
        profile["source_refs"].reverse()
        preferences["preference_ids"].reverse()
        preferences["tool_preferences"].reverse()
        preferences["style_preferences"].reverse()
        second = api.generate_skill_proposal(
            facts,
            profile,
            preferences,
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )

        self.assertEqual(
            api.canonical_skill_proposal_bytes(first),
            api.canonical_skill_proposal_bytes(second),
        )

    def test_build_and_test_command_order_remains_semantic(self):
        api = self._require_api()
        facts = self._project_facts()
        facts["build_commands"] = ["build-one", "build-two"]
        facts["test_commands"] = ["test-one", "test-two"]
        first = api.generate_skill_proposal(
            facts,
            self._verification_profile(),
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        facts["build_commands"].reverse()
        facts["test_commands"].reverse()
        second = api.generate_skill_proposal(
            facts,
            self._verification_profile(),
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        self.assertNotEqual(
            api.canonical_skill_proposal_bytes(first),
            api.canonical_skill_proposal_bytes(second),
        )

    def test_changed_project_fact_changes_fingerprint_and_candidate_digest(self):
        api = self._require_api()
        first = self._proposal()
        facts = self._project_facts()
        facts["test_commands"] = ["python -m unittest discover -s tests -v"]
        changed = api.generate_skill_proposal(
            facts,
            self._verification_profile(),
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        self.assertNotEqual(first["input_fingerprint"], changed["input_fingerprint"])
        first_project = next(item for item in first["candidates"] if item["source_kind"] == "project")
        changed_project = next(item for item in changed["candidates"] if item["source_kind"] == "project")
        self.assertNotEqual(
            first_project["manifest"]["content_sha256"],
            changed_project["manifest"]["content_sha256"],
        )

    def test_user_preferences_do_not_change_or_taint_project_candidate(self):
        self._require_api()
        first = self._proposal()
        changed_preferences = self._user_preferences()
        changed_preferences["preference_ids"] = ["pref.detailed-status"]
        changed_preferences["source_refs"] = ["artifact://sha256/" + "d" * 64]
        changed = self._proposal(user_preferences=changed_preferences)
        first_project = next(item for item in first["candidates"] if item["source_kind"] == "project")
        changed_project = next(item for item in changed["candidates"] if item["source_kind"] == "project")
        self.assertEqual(
            first_project["manifest"]["content_sha256"],
            changed_project["manifest"]["content_sha256"],
        )
        self.assertEqual(
            set(changed_project["provenance_refs"]),
            {PROJECT_REF, VERIFY_REF},
        )

    def test_schema_accepts_generated_proposal(self):
        self._require_api()
        errors = list(self.schema_validator.iter_errors(self._proposal()))
        self.assertEqual(errors, [])

    def test_generated_identifiers_obey_wire_schema_length_boundaries(self):
        api = self._require_api()
        accepted_facts = self._project_facts()
        accepted_facts["repo_id"] = "a" * 234
        accepted = api.generate_skill_proposal(
            accepted_facts,
            self._verification_profile(),
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        self.assertEqual(list(self.schema_validator.iter_errors(accepted)), [])

        rejected_facts = self._project_facts()
        rejected_facts["repo_id"] = "a" * 235
        with self.assertRaises(ValueError):
            api.generate_skill_proposal(
                rejected_facts,
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )

    def test_runtime_rejects_content_over_wire_schema_limit(self):
        api = self._require_api()
        changed = copy.deepcopy(self._proposal())
        content = "# Candidate\n" + "x" * 70_000 + "\n"
        candidate = changed["candidates"][0]
        candidate["content"] = content
        candidate["manifest"]["content_sha256"] = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(list(self.schema_validator.iter_errors(changed)), [])
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(changed)

    def test_unicode_content_respects_the_same_schema_and_runtime_bound(self):
        api = self._require_api()
        changed = copy.deepcopy(self._proposal())
        content = "# Candidate\n" + "界" * 30_000 + "\n"
        candidate = changed["candidates"][0]
        candidate["content"] = content
        candidate["manifest"]["content_sha256"] = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(list(self.schema_validator.iter_errors(changed)), [])
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(changed)

    def test_generator_version_is_strict_semver_in_runtime_and_schema(self):
        api = self._require_api()
        for version in ("1.0.0-01", "1.0.0-alpha.01"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    api.generate_skill_proposal(
                        self._project_facts(),
                        self._verification_profile(),
                        self._user_preferences(),
                        generator_version=version,
                        generated_at="2026-08-11T00:00:00Z",
                    )
                changed = copy.deepcopy(self._proposal())
                changed["generator_version"] = version
                self.assertNotEqual(
                    list(self.schema_validator.iter_errors(changed)),
                    [],
                )

    def test_generated_at_is_semantically_valid_in_runtime_and_schema(self):
        self._require_api()
        for timestamp in ("2026-02-31T00:00:00Z", "2026-08-11T00:00:00+99:99"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    self._proposal(generated_at=timestamp)
                changed = copy.deepcopy(self._proposal())
                changed["generated_at"] = timestamp
                self.assertNotEqual(
                    list(self.schema_validator.iter_errors(changed)),
                    [],
                )

    def test_embedded_manifest_schema_reuses_m4_01_contract(self):
        api = self._require_api()
        mutations = {
            "applicability": lambda manifest: manifest.update(
                applicability=[{"kind": "unknown", "ref": "anything"}]
            ),
            "dependency": lambda manifest: manifest.update(dependencies=[{}]),
            "license": lambda manifest: manifest.update(license_ref="not-a-license"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(self._proposal())
                mutate(changed["candidates"][0]["manifest"])
                with self.assertRaises(ValueError):
                    api.validate_skill_proposal(changed)
                self.assertNotEqual(
                    list(self.schema_validator.iter_errors(changed)),
                    [],
                )

    def test_candidate_license_comes_from_verification_profile(self):
        api = self._require_api()
        profile = self._verification_profile()
        profile["generated_skill_license_ref"] = "Apache-2.0"
        proposal = api.generate_skill_proposal(
            self._project_facts(),
            profile,
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        self.assertEqual(
            {candidate["manifest"]["license_ref"] for candidate in proposal["candidates"]},
            {"Apache-2.0"},
        )

    def test_versioned_fixture_validates_and_round_trips(self):
        api = self._require_api()
        fixture_path = self.root / "experiments" / "skills" / "m4-08-skill-proposal-v1alpha1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        api.validate_skill_proposal(fixture)
        self.assertEqual(json.loads(api.canonical_skill_proposal_bytes(fixture)), fixture)
        self.assertEqual(fixture, self._proposal())

    def test_candidate_content_hashes_to_manifest_and_passes_m4_03_resolver(self):
        from context_control_plane.compiled_skill_packet import compile_skill_packet
        from context_control_plane.skill_drift_quarantine import assess_skill_drift

        api = self._require_api()
        proposal = self._proposal()
        assets = api.proposal_skill_assets(proposal)
        project = next(
            candidate
            for candidate in proposal["candidates"]
            if candidate["source_kind"] == "project"
        )
        skill_id = project["manifest"]["skill_id"]
        self.assertEqual(
            hashlib.sha256(assets[skill_id]).hexdigest(),
            project["manifest"]["content_sha256"],
        )

        approved_manifest = copy.deepcopy(project["manifest"])
        approved_manifest["status"] = "approved"
        manifest_set = {
            "schema_version": "context.skill-manifest-set/v1alpha1",
            "manifests": [approved_manifest],
        }
        packet = compile_skill_packet(
            manifest_set,
            selected_skill_ids=[skill_id],
            observed_at="2026-08-11T00:00:00Z",
        )
        assessment = assess_skill_drift(
            packet,
            manifest_set,
            asset_resolver=assets.get,
            observed_at="2026-08-11T00:00:00Z",
        )
        self.assertEqual(assessment["gate"], "allow")

    def test_candidate_content_contract_rejects_unsafe_markdown_bytes(self):
        api = self._require_api()
        for name, content in {
            "byte-order mark": "\ufeff# Candidate\n",
            "frontmatter": "---\nname: forged\n---\n# Candidate\n",
            "carriage return": "# Candidate\r\n",
            "missing LF terminator": "# Candidate",
            "unpaired surrogate": "# Candidate\n\ud800\n",
        }.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(self._proposal())
                candidate = changed["candidates"][0]
                candidate["content"] = content
                try:
                    content_bytes = content.encode("utf-8")
                except UnicodeEncodeError:
                    content_bytes = b"invalid-utf8"
                candidate["manifest"]["content_sha256"] = hashlib.sha256(
                    content_bytes
                ).hexdigest()
                self.assertNotEqual(
                    list(self.schema_validator.iter_errors(changed)),
                    [],
                )
                with self.assertRaises(ValueError):
                    api.validate_skill_proposal(changed)

    def test_input_verifier_rejects_identity_and_provenance_mutations(self):
        api = self._require_api()
        proposal = self._proposal()
        def forge_content(item):
            content = item["candidates"][0]["content"] + "forged\n"
            item["candidates"][0]["content"] = content
            item["candidates"][0]["manifest"]["content_sha256"] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()

        mutations = {
            "fingerprint": lambda item: item.update(input_fingerprint="f" * 64),
            "content digest": lambda item: item["candidates"][0]["manifest"].update(
                content_sha256="f" * 64
            ),
            "generator version": lambda item: item.update(generator_version="2.0.0"),
            "input refs": lambda item: item.update(
                input_refs=["artifact://sha256/" + "d" * 64]
            ),
            "proposal id": lambda item: item.update(proposal_id="skill-proposal/other"),
            "repo applicability": lambda item: item["candidates"][0]["manifest"].update(
                applicability=[{"kind": "repo", "ref": "repo://other"}]
            ),
            "coordinated content": forge_content,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(proposal)
                mutate(changed)
                with self.assertRaises(ValueError):
                    api.verify_skill_proposal_inputs(
                        changed,
                        self._project_facts(),
                        self._verification_profile(),
                        self._user_preferences(),
                        generator_version="1.0.0",
                        expected_generated_at="2026-08-11T00:00:00Z",
                    )

    def test_input_verifier_rejects_a_retimestamped_proposal(self):
        api = self._require_api()
        changed = self._proposal(generated_at="2026-08-11T01:00:00Z")
        with self.assertRaises(ValueError):
            api.verify_skill_proposal_inputs(
                changed,
                self._project_facts(),
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                expected_generated_at="2026-08-11T00:00:00Z",
            )

    def test_manifest_and_candidate_provenance_must_match(self):
        api = self._require_api()
        mutated = copy.deepcopy(self._proposal())
        mutated["candidates"][0]["manifest"]["provenance_refs"] = mutated["input_refs"][:1]
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)

    def test_candidate_id_must_bind_to_manifest_skill_id(self):
        api = self._require_api()
        mutated = copy.deepcopy(self._proposal())
        mutated["candidates"][0]["candidate_id"] = "proposal/project/other-project"
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)

    def test_duplicate_candidate_source_kind_is_rejected(self):
        api = self._require_api()
        mutated = copy.deepcopy(self._proposal())
        duplicate = copy.deepcopy(mutated["candidates"][0])
        duplicate["candidate_id"] = "proposal/project/second-project"
        duplicate["manifest"]["skill_id"] = "proposal.project.second-project"
        duplicate["manifest"]["rule_ids"] = ["rule.project.second-project.proposal"]
        mutated["candidates"].append(duplicate)
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)

    def test_candidate_cannot_be_promoted_by_mutating_status(self):
        api = self._require_api()
        proposal = self._proposal()
        mutated = copy.deepcopy(proposal)
        mutated["status"] = "active"
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)
        mutated = copy.deepcopy(proposal)
        mutated["candidates"][0]["status"] = "active"
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)

    def test_missing_input_provenance_is_rejected(self):
        api = self._require_api()
        with self.assertRaises(ValueError):
            api.generate_skill_proposal(
                {**self._project_facts(), "source_refs": []},
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )

    def test_unknown_input_fields_are_rejected(self):
        api = self._require_api()
        facts = {**self._project_facts(), "full_transcript": "must not enter proposal"}
        with self.assertRaises(ValueError):
            api.generate_skill_proposal(
                facts,
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )

    def test_unbounded_input_bytes_are_rejected(self):
        api = self._require_api()
        facts = {**self._project_facts(), "build_commands": ["x" * (64 * 1024)]}
        with self.assertRaises(ValueError):
            api.generate_skill_proposal(
                facts,
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )

    def test_oversized_input_list_is_rejected_before_copy_or_sort(self):
        api = self._require_api()

        class OversizedList(list):
            def __deepcopy__(self, memo):
                raise AssertionError("oversized input reached deepcopy")

        facts = {
            **self._project_facts(),
            "languages": OversizedList(["python"] * 4097),
        }
        with self.assertRaises(ValueError):
            api.generate_skill_proposal(
                facts,
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )

    def test_markdown_candidate_encodes_input_backticks(self):
        api = self._require_api()
        facts = {**self._project_facts(), "build_commands": ["echo `date`"]}
        proposal = api.generate_skill_proposal(
            facts,
            self._verification_profile(),
            self._user_preferences(),
            generator_version="1.0.0",
            generated_at="2026-08-11T00:00:00Z",
        )
        project = next(
            item for item in proposal["candidates"] if item["source_kind"] == "project"
        )
        self.assertIn(r"echo \u0060date\u0060", project["content"])
        self.assertNotIn("echo `date`", project["content"])

    def test_unbounded_proposal_output_is_rejected(self):
        api = self._require_api()
        mutated = copy.deepcopy(self._proposal())
        mutated["input_refs"].extend(
            "artifact://sha256/" + f"{index:064x}" for index in range(2500)
        )
        with self.assertRaises(ValueError):
            api.validate_skill_proposal(mutated)

    def test_generation_pipeline_p95_stays_below_local_bound(self):
        api = self._require_api()
        samples = []
        for _ in range(200):
            started = time.perf_counter_ns()
            proposal = self._proposal()
            api.validate_skill_proposal(proposal)
            api.canonical_skill_proposal_bytes(proposal)
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        samples.sort()
        self.assertLess(samples[189], 10.0)

    def test_quantitative_replay_variant_and_authority_probe(self):
        api = self._require_api()
        baseline = self._proposal()
        baseline_bytes = api.canonical_skill_proposal_bytes(baseline)
        replay_mismatches = sum(
            api.canonical_skill_proposal_bytes(self._proposal()) != baseline_bytes
            for _ in range(200)
        )
        identities = set()
        for index in range(40):
            facts = self._project_facts()
            facts["test_commands"] = [f"python -m unittest --shard {index}"]
            proposal = api.generate_skill_proposal(
                facts,
                self._verification_profile(),
                self._user_preferences(),
                generator_version="1.0.0",
                generated_at="2026-08-11T00:00:00Z",
            )
            project = next(
                item
                for item in proposal["candidates"]
                if item["source_kind"] == "project"
            )
            identities.add(
                (
                    proposal["input_fingerprint"],
                    project["manifest"]["content_sha256"],
                )
            )
        granted_permissions = sum(
            value is True
            for candidate in baseline["candidates"]
            for value in candidate["permissions"].values()
        )

        self.assertEqual(replay_mismatches, 0)
        self.assertEqual(len(identities), 40)
        self.assertEqual(granted_permissions, 0)


if __name__ == "__main__":
    unittest.main()
