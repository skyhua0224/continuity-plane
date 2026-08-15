import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

EXPERIMENT_VERSION = "context.patch-ab-experiment/v1alpha2"
BLIND_SET_VERSION = "context.patch-ab-blind-set/v1alpha2"
PACKET_VERSION = "context.patch-ab-verifier-packet/v1alpha1"
SCORE_VERSION = "context.patch-ab-verifier-score/v1alpha2"
RESULT_VERSION = "context.patch-ab-result/v1alpha2"
BENCHMARK_VERSION = "context.patch-ab-benchmark/v1alpha2"


def _seal(document: dict, field: str) -> dict:
    unsigned = copy.deepcopy(document)
    unsigned.pop(field, None)
    document[field] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return document


class M704PatchABEvaluationTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def module(self):
        module = importlib.import_module("context_control_plane.patch_ab_evaluation")
        self.assertIsNotNone(module)
        return module

    @staticmethod
    def budget() -> dict:
        return {
            "input_tokens": 8192,
            "output_tokens": 4096,
            "tool_calls": 24,
            "wall_time_ms": 600000,
        }

    def case(
        self,
        *,
        suffix: str = "fixed",
        entropy: str = "entropy:fixed",
        treatment_quality: dict | None = None,
    ) -> dict:
        module = self.module()
        artifacts: dict[str, bytes] = {}

        def store_bytes(payload: bytes) -> str:
            ref = "artifact://sha256/" + hashlib.sha256(payload).hexdigest()
            artifacts[ref] = payload
            return ref

        def store(label: str) -> str:
            return store_bytes(label.encode("utf-8"))

        fixture_ref = store(f"fixture:{suffix}")
        entropy_ref = store(entropy)
        qualities = {
            "control": {
                "build_passed": True,
                "test_passed": True,
                "mutation_score_basis_points": 8200,
                "rework_count": 4,
            },
            "treatment": treatment_quality
            or {
                "build_passed": True,
                "test_passed": True,
                "mutation_score_basis_points": 8400,
                "rework_count": 2,
            },
        }
        candidates = []
        quality_resolutions = {}
        for variant in ("control", "treatment"):
            candidate_id = f"candidate/{variant}/{suffix}"
            patch_ref = store(f"patch:{variant}:{suffix}")
            quality = copy.deepcopy(qualities[variant])
            decision_ref = store_bytes(
                module.build_patch_ab_quality_evidence(
                    evidence_kind="verification-decision",
                    evidence_id=f"evidence/decision/{variant}/{suffix}",
                    work_id="M7-04",
                    candidate_id=candidate_id,
                    patch_ref=patch_ref,
                    fixture_ref=fixture_ref,
                    quality=quality,
                    status="satisfied",
                )
            )
            run_ref = store_bytes(
                module.build_patch_ab_quality_evidence(
                    evidence_kind="verification-run",
                    evidence_id=f"evidence/run/{variant}/{suffix}",
                    work_id="M7-04",
                    candidate_id=candidate_id,
                    patch_ref=patch_ref,
                    fixture_ref=fixture_ref,
                    quality=quality,
                    status="passed",
                )
            )
            admission_ref = store_bytes(
                module.build_patch_ab_quality_evidence(
                    evidence_kind="claim-admission",
                    evidence_id=f"evidence/claim/{variant}/{suffix}",
                    work_id="M7-04",
                    candidate_id=candidate_id,
                    patch_ref=patch_ref,
                    fixture_ref=fixture_ref,
                    quality=quality,
                    status="allow",
                )
            )
            evidence_refs = [decision_ref, run_ref, admission_ref]
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "variant": variant,
                    "producer_id": f"producer/{variant}/{suffix}",
                    "patch_ref": patch_ref,
                    "run_context": {
                        "provider_id": "provider/fixture",
                        "model_id": "model/fixture-v1",
                        "fixture_ref": fixture_ref,
                        "budget": self.budget(),
                    },
                    "quality": quality,
                    "evidence_refs": evidence_refs,
                }
            )
            quality_resolutions[candidate_id] = {
                "candidate_id": candidate_id,
                "patch_ref": patch_ref,
                "fixture_ref": fixture_ref,
                "quality": copy.deepcopy(quality),
                "verification_decision_ref": decision_ref,
                "verification_run_refs": [run_ref],
                "claim_admission_ref": admission_ref,
                "verification_status": "satisfied",
                "run_status": "passed",
                "claim_decision": "allow",
            }
        draft = {
            "experiment_id": f"experiment/m7-04/{suffix}",
            "work_id": "M7-04",
            "candidates": candidates,
            "verifier_policy": {
                "minimum_independent_verifiers": 2,
                "rubric_ids": [
                    "correctness",
                    "maintainability",
                    "scope_discipline",
                ],
                "score_min_basis_points": 0,
                "score_max_basis_points": 10000,
            },
            "state_write_authority": False,
            "completion_authority": False,
        }

        frozen = module._frozen_experiment_digest(draft)
        provenance_ref = store_bytes(
            module.build_patch_ab_randomizer_provenance(
                provenance_id=f"provenance/randomizer/{suffix}",
                randomizer_id=f"randomizer/{suffix}",
                entropy_ref=entropy_ref,
                entropy_sha256=hashlib.sha256(entropy.encode()).hexdigest(),
                frozen_experiment_sha256=frozen,
            )
        )

        def artifact_resolver(ref: str):
            return artifacts.get(ref)

        experiment = module.finalize_patch_ab_experiment(
            draft,
            randomizer_id=f"randomizer/{suffix}",
            entropy_ref=entropy_ref,
            provenance_ref=provenance_ref,
            artifact_resolver=artifact_resolver,
        )
        blind_set = module.prepare_blind_patch_set(experiment)
        packet = module.prepare_verifier_packet(experiment, blind_set)
        principals = {
            f"producer/control/{suffix}": f"principal/producer/control/{suffix}",
            f"producer/treatment/{suffix}": (
                f"principal/producer/treatment/{suffix}"
            ),
            f"randomizer/{suffix}": f"principal/randomizer/{suffix}",
            "verifier/one": "principal/verifier/one",
            "verifier/two": "principal/verifier/two",
        }

        def principal_resolver(actor_ref: str):
            return principals.get(actor_ref)

        def quality_resolver(candidate: dict):
            value = quality_resolutions.get(candidate["candidate_id"])
            return copy.deepcopy(value) if value is not None else None

        return {
            "experiment": experiment,
            "blind_set": blind_set,
            "packet": packet,
            "artifacts": artifacts,
            "principals": principals,
            "quality_resolutions": quality_resolutions,
            "artifact_resolver": artifact_resolver,
            "principal_resolver": principal_resolver,
            "quality_resolver": quality_resolver,
        }

    def score(
        self,
        case: dict,
        *,
        verifier_id: str,
        assignment: dict,
        score: int,
    ) -> dict:
        module = self.module()
        experiment = case["experiment"]
        packet = case["packet"]
        receipt = {
            "schema_version": SCORE_VERSION,
            "score_id": (
                f"score/{verifier_id.split('/')[-1]}/"
                f"{assignment['assignment_token'].split('/')[-1][:24]}"
            ),
            "experiment_id": experiment["experiment_id"],
            "experiment_sha256": experiment["experiment_sha256"],
            "verifier_packet_sha256": packet["packet_sha256"],
            "verifier_id": verifier_id,
            "verifier_role": "independent",
            "assignment_token": assignment["assignment_token"],
            "rubric_scores": [
                {"rubric_id": rubric_id, "score_basis_points": score}
                for rubric_id in packet["rubric_ids"]
            ],
            "evidence_refs": [],
            "state_write_authority": False,
            "completion_authority": False,
        }
        evidence_ref = "artifact://sha256/" + hashlib.sha256(
            module.build_patch_ab_score_evidence(receipt)
        ).hexdigest()
        case["artifacts"][evidence_ref] = module.build_patch_ab_score_evidence(
            receipt
        )
        receipt["evidence_refs"] = [evidence_ref]
        return _seal(receipt, "score_sha256")

    def scores(
        self,
        case: dict,
        *,
        control_score: int = 8100,
        treatment_score: int = 8500,
        verifier_ids: tuple[str, ...] = ("verifier/one", "verifier/two"),
    ) -> list[dict]:
        treatment_ref = next(
            item["patch_ref"]
            for item in case["experiment"]["candidates"]
            if item["variant"] == "treatment"
        )
        scores = []
        for verifier_id in verifier_ids:
            for assignment in case["packet"]["assignments"]:
                score = (
                    treatment_score
                    if assignment["patch_ref"] == treatment_ref
                    else control_score
                )
                scores.append(
                    self.score(
                        case,
                        verifier_id=verifier_id,
                        assignment=assignment,
                        score=score,
                    )
                )
        return scores

    def evaluate(self, case: dict, scores: list[dict] | None = None, **overrides):
        module = self.module()
        arguments = {
            "verifier_packet": case["packet"],
            "principal_resolver": case["principal_resolver"],
            "artifact_resolver": case["artifact_resolver"],
            "quality_resolver": case["quality_resolver"],
        }
        arguments.update(overrides)
        return module.evaluate_patch_ab(
            case["experiment"],
            case["blind_set"],
            scores if scores is not None else self.scores(case),
            **arguments,
        )

    def test_contract_module_is_available(self):
        self.module()

    def test_blind_set_and_verifier_packet_hide_candidate_identity(self):
        module = self.module()
        case = self.case()
        second = module.prepare_blind_patch_set(copy.deepcopy(case["experiment"]))
        self.assertEqual(case["blind_set"], second)
        self.assertEqual(case["blind_set"]["schema_version"], BLIND_SET_VERSION)
        self.assertEqual(case["packet"]["schema_version"], PACKET_VERSION)
        serialized = json.dumps(case["packet"], sort_keys=True)
        for forbidden in (
            '"variant"',
            '"candidate_id"',
            '"producer_id"',
            '"quality"',
            '"run_context"',
            '"control"',
            '"treatment"',
            '"blind_id"',
        ):
            self.assertNotIn(forbidden, serialized)
        module.validate_verifier_packet(
            case["packet"],
            expected_experiment=case["experiment"],
            expected_blind_set=case["blind_set"],
        )

    def test_entropy_controls_deterministic_blind_assignment(self):
        first = self.case(suffix="lower", entropy="entropy:lower")
        second = self.case(suffix="upper", entropy="entropy:upper")
        self.assertNotEqual(
            first["experiment"]["randomization_seed_sha256"],
            second["experiment"]["randomization_seed_sha256"],
        )
        self.assertNotEqual(first["blind_set"], second["blind_set"])

    def test_same_provider_model_budget_and_fixture_are_mandatory(self):
        module = self.module()
        case = self.case()
        mutations = (
            lambda value: value["candidates"][1]["run_context"].update(
                provider_id="provider/other"
            ),
            lambda value: value["candidates"][1]["run_context"].update(
                model_id="model/other"
            ),
            lambda value: value["candidates"][1]["run_context"]["budget"].update(
                output_tokens=4095
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                experiment = copy.deepcopy(case["experiment"])
                mutation(experiment)
                _seal(experiment, "experiment_sha256")
                with self.assertRaises(module.PatchABContractError):
                    module.validate_patch_ab_experiment(experiment)

    def test_verifier_must_be_independent_and_score_both_candidates(self):
        module = self.module()
        case = self.case()
        scores = self.scores(case)
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, scores[:-1])

        self_review = copy.deepcopy(scores)
        self_review[0]["verifier_id"] = "producer/control/fixed"
        _seal(self_review[0], "score_sha256")
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, self_review)

        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, scores + [copy.deepcopy(scores[0])])

    def test_quality_is_not_degraded_and_rework_is_reduced(self):
        result = self.evaluate(self.case())
        self.assertEqual(result["schema_version"], RESULT_VERSION)
        self.assertEqual(result["decision"], "admit")
        self.assertEqual(result["trust_status"], "trusted")
        self.assertTrue(result["quality_not_degraded"])
        self.assertTrue(result["rework_reduced"])
        self.assertEqual(result["quality_deltas"]["mutation_score_basis_points"], 200)
        self.assertEqual(result["quality_deltas"]["rework_count"], -2)
        self.assertGreater(
            result["quality_deltas"][
                "verifier_score_delta_numerator_basis_points"
            ],
            0,
        )
        self.assertFalse(result["state_write_authority"])
        self.assertFalse(result["completion_authority"])

    def test_each_completion_gate_fails_closed(self):
        cases = {
            "build": {
                "build_passed": False,
                "test_passed": True,
                "mutation_score_basis_points": 8400,
                "rework_count": 2,
            },
            "test": {
                "build_passed": True,
                "test_passed": False,
                "mutation_score_basis_points": 8400,
                "rework_count": 2,
            },
            "mutation": {
                "build_passed": True,
                "test_passed": True,
                "mutation_score_basis_points": 8199,
                "rework_count": 2,
            },
            "rework": {
                "build_passed": True,
                "test_passed": True,
                "mutation_score_basis_points": 8400,
                "rework_count": 4,
            },
        }
        for name, treatment_quality in cases.items():
            with self.subTest(name=name):
                result = self.evaluate(
                    self.case(suffix=name, treatment_quality=treatment_quality)
                )
                self.assertEqual(result["decision"], "reject")

        case = self.case(suffix="verifier")
        result = self.evaluate(
            case,
            self.scores(case, control_score=8500, treatment_score=8499),
        )
        self.assertEqual(result["decision"], "reject")
        self.assertIn("verifier_score_regressed", result["reasons"])

    def test_fractional_verifier_score_regression_fails_closed(self):
        module = self.module()
        case = self.case(suffix="fractional")
        scores = self.scores(case, control_score=8100, treatment_score=8100)
        control_ref = next(
            item["patch_ref"]
            for item in case["experiment"]["candidates"]
            if item["variant"] == "control"
        )
        control_token = next(
            item["assignment_token"]
            for item in case["packet"]["assignments"]
            if item["patch_ref"] == control_ref
        )
        control_score = next(
            score for score in scores if score["assignment_token"] == control_token
        )
        control_score["rubric_scores"][0]["score_basis_points"] = 8101
        old_evidence_ref = control_score["evidence_refs"][0]
        evidence_payload = module.build_patch_ab_score_evidence(control_score)
        new_evidence_ref = "artifact://sha256/" + hashlib.sha256(
            evidence_payload
        ).hexdigest()
        case["artifacts"][new_evidence_ref] = evidence_payload
        control_score["evidence_refs"] = [new_evidence_ref]
        del case["artifacts"][old_evidence_ref]
        _seal(control_score, "score_sha256")
        result = self.evaluate(case, scores)
        delta = result["quality_deltas"]
        self.assertEqual(delta["verifier_score_delta_numerator_basis_points"], -1)
        self.assertEqual(delta["verifier_score_delta_denominator"], 6)
        self.assertEqual(result["decision"], "reject")

    def test_hash_authority_and_expected_input_tampering_is_rejected(self):
        module = self.module()
        case = self.case()
        invalid = copy.deepcopy(case["experiment"])
        invalid["state_write_authority"] = True
        _seal(invalid, "experiment_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_experiment(invalid)

        scores = self.scores(case)
        result = self.evaluate(case, scores)
        tampered = copy.deepcopy(result)
        tampered["quality_deltas"]["rework_count"] = -99
        _seal(tampered, "result_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_result(
                tampered,
                expected_experiment=case["experiment"],
                expected_blind_set=case["blind_set"],
                expected_verifier_packet=case["packet"],
                expected_scores=scores,
                principal_resolver=case["principal_resolver"],
                artifact_resolver=case["artifact_resolver"],
                quality_resolver=case["quality_resolver"],
            )

    def test_strict_schemas_accept_current_contracts(self):
        case = self.case()
        scores = self.scores(case)
        result = self.evaluate(case, scores)
        instances = {
            "schemas/m7-04/patch-ab-experiment.schema.json": case["experiment"],
            "schemas/m7-04/patch-ab-blind-set.schema.json": case["blind_set"],
            "schemas/m7-04/patch-ab-verifier-packet.schema.json": case["packet"],
            "schemas/m7-04/patch-ab-verifier-score.schema.json": scores[0],
            "schemas/m7-04/patch-ab-result.schema.json": result,
        }
        for relative_path, instance in instances.items():
            with self.subTest(relative_path=relative_path):
                schema = json.loads(
                    (self.root / relative_path).read_text(encoding="utf-8")
                )
                Draft202012Validator.check_schema(schema)
                self.assertFalse(schema["additionalProperties"])
                Draft202012Validator(schema).validate(instance)

    def test_typed_evidence_schemas_accept_current_artifacts(self):
        case = self.case(suffix="typed-schema")
        scores = self.scores(case)
        provenance_ref = case["experiment"]["randomizer"]["provenance_ref"]
        score_ref = scores[0]["evidence_refs"][0]
        quality_ref = case["experiment"]["candidates"][0]["evidence_refs"][0]
        instances = {
            "schemas/m7-04/patch-ab-randomizer-provenance.schema.json": json.loads(
                case["artifacts"][provenance_ref]
            ),
            "schemas/m7-04/patch-ab-score-evidence.schema.json": json.loads(
                case["artifacts"][score_ref]
            ),
            "schemas/m7-04/patch-ab-quality-evidence.schema.json": json.loads(
                case["artifacts"][quality_ref]
            ),
        }
        for relative_path, instance in instances.items():
            with self.subTest(relative_path=relative_path):
                schema = json.loads(
                    (self.root / relative_path).read_text(encoding="utf-8")
                )
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(instance)

    def test_fixed_benchmark_is_deterministic_and_fail_closed(self):
        module = self.module()
        first, outcomes = module.build_patch_ab_benchmark(samples=120)
        second, second_outcomes = module.build_patch_ab_benchmark(samples=120)
        self.assertEqual(first, second)
        self.assertEqual(outcomes, second_outcomes)
        self.assertEqual(first["schema_version"], BENCHMARK_VERSION)
        self.assertEqual(first["successful_samples"], 120)
        self.assertEqual(first["expected_admit_count"], first["actual_admit_count"])
        self.assertEqual(first["expected_reject_count"], first["actual_reject_count"])
        self.assertEqual(first["false_admit_count"], 0)
        self.assertEqual(first["false_reject_count"], 0)
        module.validate_patch_ab_benchmark(first, outcomes_bytes=outcomes)

    def test_committed_fixture_and_benchmark_replay_exactly(self):
        module = self.module()
        committed_fixture = json.loads(
            (
                self.root / "experiments/evidence/m7-04-patch-ab-fixture.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(committed_fixture, module.build_fixed_patch_ab_fixture())

        committed_benchmark = json.loads(
            (
                self.root / "experiments/evidence/m7-04-patch-ab-benchmark-results.json"
            ).read_text(encoding="utf-8")
        )
        outcomes = (
            self.root / "experiments/evidence/m7-04-patch-ab-benchmark-outcomes.json"
        ).read_bytes()
        rebuilt, rebuilt_outcomes = module.build_patch_ab_benchmark(samples=1000)
        self.assertEqual(committed_benchmark, rebuilt)
        self.assertEqual(outcomes, rebuilt_outcomes)
        benchmark_schema = json.loads(
            (
                self.root / "schemas/m7-04/patch-ab-benchmark.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(benchmark_schema)
        Draft202012Validator(benchmark_schema).validate(committed_benchmark)
        module.validate_patch_ab_benchmark(committed_benchmark, outcomes_bytes=outcomes)

    def test_alias_self_review_cannot_admit(self):
        module = self.module()
        case = self.case(suffix="aliases")
        scores = self.scores(case)
        aliases = {
            "producer/control-alias": case["principals"][
                "producer/control/aliases"
            ],
            "producer/treatment-alias": case["principals"][
                "producer/treatment/aliases"
            ],
        }
        case["principals"].update(aliases)
        for score in scores:
            score["verifier_id"] = (
                "producer/control-alias"
                if score["verifier_id"] == "verifier/one"
                else "producer/treatment-alias"
            )
            _seal(score, "score_sha256")
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, scores)

    def test_randomizer_cannot_share_canonical_principal_with_verifier(self):
        module = self.module()
        case = self.case(suffix="randomizer-verifier-collision")
        case["principals"]["randomizer/randomizer-verifier-collision"] = case[
            "principals"
        ]["verifier/one"]
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case)

    def test_randomizer_provenance_must_be_typed_and_bound(self):
        module = self.module()
        case = self.case(suffix="untyped-randomizer-provenance")
        provenance_ref = case["experiment"]["randomizer"]["provenance_ref"]
        case["artifacts"][provenance_ref] = b"untyped provenance"
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case)

    def test_score_evidence_must_bind_the_score(self):
        module = self.module()
        case = self.case(suffix="unbound-score-evidence")
        scores = self.scores(case)
        scores[0]["evidence_refs"] = [
            case["experiment"]["candidates"][0]["patch_ref"]
        ]
        _seal(scores[0], "score_sha256")
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, scores)

    def test_quality_evidence_roles_must_be_typed_and_bound(self):
        module = self.module()
        case = self.case(suffix="unbound-quality-evidence")
        for resolution in case["quality_resolutions"].values():
            resolution["verification_decision_ref"] = resolution[
                "verification_run_refs"
            ][0]
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case)

    def test_unresolved_artifacts_and_quality_cannot_admit(self):
        case = self.case()
        result = self.evaluate(
            case,
            principal_resolver=None,
            artifact_resolver=None,
            quality_resolver=None,
        )
        self.assertEqual(result["decision"], "provisional")
        self.assertEqual(result["trust_status"], "unresolved")

    def test_unblind_mapping_is_not_a_public_verifier_api(self):
        self.assertFalse(hasattr(self.module(), "unblind_candidate_ids"))

    def test_runtime_caps_verifiers_and_artifact_refs_like_schema(self):
        module = self.module()
        case = self.case()
        experiment = copy.deepcopy(case["experiment"])
        experiment["candidates"][0]["evidence_refs"] = [
            "artifact://sha256/" + hashlib.sha256(str(index).encode()).hexdigest()
            for index in range(129)
        ]
        _seal(experiment, "experiment_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_experiment(experiment)

        verifier_ids = tuple(f"verifier/{index}" for index in range(17))
        scores = self.scores(case, verifier_ids=verifier_ids)
        with self.assertRaises(module.PatchABContractError):
            self.evaluate(case, scores, principal_resolver=None)

    def test_result_exposes_exact_verifier_score_fraction(self):
        result = self.evaluate(self.case())
        delta = result["quality_deltas"]
        self.assertEqual(
            delta["verifier_score_delta_numerator_basis_points"], 2400
        )
        self.assertEqual(delta["verifier_score_delta_denominator"], 6)

    def test_seed_is_bound_to_frozen_candidates_and_randomizer_provenance(self):
        module = self.module()
        case = self.case()
        experiment = copy.deepcopy(case["experiment"])
        experiment["candidates"][1]["patch_ref"] = experiment["candidates"][0][
            "patch_ref"
        ]
        _seal(experiment, "experiment_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_experiment(experiment)
        self.assertEqual(
            case["experiment"]["randomizer"]["frozen_experiment_sha256"],
            case["experiment"]["frozen_experiment_sha256"],
        )

    def test_benchmark_validator_recomputes_outcomes_and_aggregates(self):
        module = self.module()
        benchmark, outcomes = module.build_patch_ab_benchmark(samples=12)
        tampered = copy.deepcopy(benchmark)
        tampered["actual_admit_count"] += 1
        _seal(tampered, "benchmark_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_benchmark(tampered, outcomes_bytes=outcomes)
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_benchmark(
                benchmark, outcomes_bytes=outcomes + b"\n"
            )

    def test_benchmark_rejects_impossible_admit_flags(self):
        module = self.module()
        benchmark, outcomes = module.build_patch_ab_benchmark(samples=12)
        values = json.loads(outcomes)
        values[0]["quality_not_degraded"] = False
        mutated = (
            json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        benchmark["outcomes_sha256"] = hashlib.sha256(mutated).hexdigest()
        benchmark["outcomes_artifact_ref"] = (
            "artifact://sha256/" + benchmark["outcomes_sha256"]
        )
        benchmark["admitted_quality_nondegradation_count"] -= 1
        _seal(benchmark, "benchmark_sha256")
        with self.assertRaises(module.PatchABContractError):
            module.validate_patch_ab_benchmark(benchmark, outcomes_bytes=mutated)


if __name__ == "__main__":
    unittest.main()
