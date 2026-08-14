import copy
import hashlib
import json
import statistics
import time
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.sticky_router import (
    StickyRouteError,
    canonical_route_decision_bytes,
    canonical_route_request_bytes,
    route_task_input,
)
from context_control_plane.sticky_router_benchmark import benchmark_sticky_routes


class M302StickyRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]

    def setUp(self):
        self.state = {
            "schema_version": "context.typed-state/v2alpha1",
            "project": {
                "project_id": "project-routing",
                "revision": 43,
                "governance_ref": "artifact://governance/master-43",
                "active_work_ids": ["work-router"],
                "primary_work_id": "work-router",
                "current_decision_ids": [],
                "active_constraint_ids": [],
                "open_blocker_ids": [],
                "effect_high_watermark": 0,
                "updated_at": "2026-08-13T15:00:00+08:00",
            },
            "works": [
                self._work("campaign-m3", "campaign", None, "ready"),
                self._work("goal-routing", "goal", "campaign-m3", "ready"),
                self._work("work-router", "work", "goal-routing", "active"),
                self._work("work-checkpoint", "work", "goal-routing", "ready"),
            ],
            "claims": [self._active_claim("claim-router", "work-router")],
            "ideas": [],
            "decisions": [],
            "constraints": [],
            "evidence": [],
            "blockers": [],
            "effects": [],
        }

    def _request(self, input_kind, **updates):
        request = {
            "schema_version": "context.task-route-request/v1alpha1",
            "request_id": "route-request-001",
            "project_id": "project-routing",
            "expected_project_revision": 43,
            "active_work_id": "work-router",
            "expected_active_work_revision": 1,
            "input_ref": "source://opaque/thread/range-001",
            "input_sha256": "a" * 64,
            "input_kind": input_kind,
            "classifier_confidence_millionths": 1_000_000,
            "classifier_provenance_ref": "artifact://classifier/route-v1",
            "target_work_id": None,
            "user_authorization_candidate": False,
            "authorization_candidate_ref": None,
            "evidence_refs": [],
        }
        request.update(updates)
        return request

    @staticmethod
    def _work(work_id, kind, parent_id, status):
        return {
            "work_id": work_id,
            "kind": kind,
            "title": work_id,
            "status": status,
            "parent_work_id": parent_id,
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": f"routing/{work_id}"}
            ],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": 1,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }

    @staticmethod
    def _active_claim(claim_id, work_id):
        return {
            "claim_id": claim_id,
            "work_id": work_id,
            "actor_ref": "actor-owner",
            "status": "active",
            "expected_project_revision": 43,
            "claimed_at": "2026-08-13T14:00:00+08:00",
            "lease_expires_at": "2026-08-13T16:00:00+08:00",
            "released_at": None,
            "scope_owners": [
                {"scope_kind": "capability", "scope_ref": f"routing/{work_id}"}
            ],
        }

    def _switch_request(self, **updates):
        defaults = {
            "target_work_id": "work-checkpoint",
            "user_authorization_candidate": True,
            "authorization_candidate_ref": "source://opaque/thread/auth-range-001",
        }
        defaults.update(updates)
        return self._request("switch", **defaults)

    def test_non_blocking_inputs_keep_the_active_leaf(self):
        expected = {
            "continue": "continue",
            "status_query": "continue",
            "discussion_request": "continue",
            "context_addition": "continue",
            "idea": "capture-candidate-and-continue",
        }
        for input_kind, route in expected.items():
            with self.subTest(input_kind=input_kind):
                decision = route_task_input(self._request(input_kind), self.state)
                self.assertEqual(decision["route"], route)
                self.assertEqual(decision["active_work_id_before"], "work-router")
                self.assertEqual(decision["active_work_id_after"], "work-router")
                self.assertEqual(decision["active_work_revision_before"], 1)
                self.assertEqual(decision["active_work_revision_after"], 1)
                self.assertFalse(decision["checkpoint_required_before_activation"])
                self.assertFalse(decision["route_proposal_required"])
                self.assertFalse(decision["state_write_authority"])

    def test_m3_08_only_input_kinds_are_rejected(self):
        for input_kind in ("blocking_decision", "stop_or_replace"):
            with self.subTest(input_kind=input_kind):
                with self.assertRaisesRegex(StickyRouteError, "unsupported"):
                    route_task_input(self._request(input_kind), self.state)

    def test_wire_contract_uses_only_canonical_snake_case_input_kinds(self):
        for input_kind in (
            "status-query",
            "discussion-request",
            "context-addition",
            "child-work",
        ):
            with self.subTest(input_kind=input_kind):
                with self.assertRaisesRegex(StickyRouteError, "unsupported"):
                    route_task_input(self._request(input_kind), self.state)

    def test_switch_authorization_is_always_an_unverified_candidate(self):
        missing = route_task_input(
            self._request("switch", target_work_id="work-checkpoint"), self.state
        )
        unbound = route_task_input(
            self._request(
                "switch",
                target_work_id="work-checkpoint",
                user_authorization_candidate=True,
            ),
            self.state,
        )
        candidate = route_task_input(self._switch_request(), self.state)

        self.assertEqual(missing["route"], "continue")
        self.assertEqual(missing["reason_code"], "authorization-candidate-required")
        self.assertEqual(unbound["route"], "continue")
        self.assertEqual(unbound["reason_code"], "authorization-candidate-ref-required")
        self.assertEqual(candidate["route"], "propose-switch")
        self.assertEqual(
            candidate["reason_code"], "authorization-candidate-requires-m3-03-verification"
        )
        self.assertTrue(candidate["checkpoint_required_before_activation"])
        self.assertTrue(candidate["route_proposal_required"])
        self.assertTrue(candidate["review_required"])
        self.assertFalse(candidate["authorization_verified"])
        self.assertFalse(candidate["state_write_authority"])

    def test_low_confidence_preserves_idea_correction_and_switch_signals(self):
        idea = route_task_input(
            self._request("idea", classifier_confidence_millionths=0), self.state
        )
        correction = route_task_input(
            self._request("correction", classifier_confidence_millionths=499_999),
            self.state,
        )
        switch = route_task_input(
            self._switch_request(classifier_confidence_millionths=499_999), self.state
        )
        boundary = route_task_input(
            self._switch_request(classifier_confidence_millionths=500_000), self.state
        )

        self.assertEqual(idea["route"], "capture-candidate-and-continue")
        self.assertEqual(idea["reason_code"], "low-confidence-idea-candidate")
        self.assertEqual(correction["route"], "propose-correction")
        self.assertTrue(correction["write_protection_required"])
        self.assertEqual(switch["route"], "propose-switch")
        self.assertEqual(
            switch["reason_code"], "low-confidence-switch-candidate-requires-review"
        )
        self.assertTrue(switch["review_required"])
        self.assertEqual(
            boundary["reason_code"], "authorization-candidate-requires-m3-03-verification"
        )

    def test_correction_and_child_work_are_candidate_routes_only(self):
        correction = route_task_input(self._request("correction"), self.state)
        child = route_task_input(self._request("child_work"), self.state)

        self.assertEqual(correction["route"], "propose-correction")
        self.assertTrue(correction["route_proposal_required"])
        self.assertTrue(correction["write_protection_required"])
        self.assertEqual(child["route"], "propose-child")
        self.assertTrue(child["route_proposal_required"])
        self.assertIsNone(child["target_work_id"])
        for decision in (correction, child):
            self.assertEqual(decision["active_work_id_after"], "work-router")
            self.assertFalse(decision["authorization_verified"])
            self.assertFalse(decision["state_write_authority"])

    def test_unknown_or_irrelevant_target_never_interrupts_the_sticky_path(self):
        cases = (
            (
                self._request("status_query", target_work_id="missing-work"),
                "sticky-active-leaf",
            ),
            (
                self._request("switch", target_work_id="missing-work"),
                "target-unknown",
            ),
            (
                self._switch_request(
                    target_work_id="missing-work",
                    classifier_confidence_millionths=0,
                ),
                "target-unknown",
            ),
        )
        for request, reason in cases:
            with self.subTest(input_kind=request["input_kind"]):
                decision = route_task_input(request, self.state)
                self.assertEqual(decision["route"], "continue")
                self.assertEqual(decision["reason_code"], reason)
                self.assertEqual(decision["active_work_id_after"], "work-router")

    def test_non_ready_target_stays_sticky(self):

        for status in ("proposed", "blocked", "active"):
            state = copy.deepcopy(self.state)
            target = self._work(f"work-{status}", "work", "goal-routing", status)
            state["works"].append(target)
            if status == "active":
                state["project"]["active_work_ids"].append(target["work_id"])
                state["claims"].append(self._active_claim("claim-second", target["work_id"]))
            decision = route_task_input(
                self._switch_request(target_work_id=target["work_id"]), state
            )
            self.assertEqual(decision["route"], "continue")
            self.assertEqual(decision["reason_code"], "target-not-ready")
            self.assertEqual(decision["target_work_revision"], 1)

    def test_canonical_decision_rejects_impossible_route_combinations(self):
        switch = route_task_input(self._switch_request(), self.state)
        child = route_task_input(self._request("child_work"), self.state)
        continued = route_task_input(self._request("continue"), self.state)
        variants = []
        invalid = copy.deepcopy(switch)
        invalid["input_kind"] = "idea"
        variants.append(invalid)
        invalid = copy.deepcopy(switch)
        invalid["target_work_id"] = None
        invalid["target_work_revision"] = None
        variants.append(invalid)
        invalid = copy.deepcopy(child)
        invalid["route_proposal_required"] = False
        invalid["review_required"] = False
        variants.append(invalid)
        invalid = copy.deepcopy(continued)
        invalid["route_proposal_required"] = True
        invalid["review_required"] = True
        variants.append(invalid)
        invalid = copy.deepcopy(continued)
        invalid["target_work_id"] = "work-checkpoint"
        invalid["target_work_revision"] = 1
        variants.append(invalid)

        for invalid in variants:
            with self.subTest(route=invalid["route"], input_kind=invalid["input_kind"]):
                with self.assertRaises(StickyRouteError):
                    canonical_route_decision_bytes(invalid)

    def test_ready_target_with_dependency_or_blocker_cannot_be_proposed(self):
        dependency_state = copy.deepcopy(self.state)
        dependency_state["works"].append(
            self._work("work-prerequisite", "work", "goal-routing", "ready")
        )
        dependency_state["works"][3]["dependency_ids"] = ["work-prerequisite"]

        blocker_state = copy.deepcopy(self.state)
        blocker_state["blockers"].append(
            {
                "blocker_id": "blocker-target",
                "status": "open",
                "reason": "target is blocked",
                "blocked_work_ids": ["work-checkpoint"],
                "evidence_ids": [],
                "opened_at": "2026-08-13T14:30:00+08:00",
                "resolved_at": None,
                "supersedes_blocker_id": None,
            }
        )
        blocker_state["project"]["open_blocker_ids"] = ["blocker-target"]
        blocker_state["works"][3]["blocker_ids"] = ["blocker-target"]

        reverse_only_blocker_state = copy.deepcopy(blocker_state)
        reverse_only_blocker_state["works"][3]["blocker_ids"] = []

        cases = (
            (dependency_state, "work-checkpoint", "target-dependencies-incomplete"),
            (blocker_state, "work-checkpoint", "target-blocked"),
            (reverse_only_blocker_state, "work-checkpoint", "target-blocked"),
        )
        for state, target_id, reason in cases:
            with self.subTest(reason=reason):
                decision = route_task_input(
                    self._switch_request(target_work_id=target_id), state
                )
                self.assertEqual(decision["route"], "continue")
                self.assertEqual(decision["reason_code"], reason)
                self.assertFalse(decision["checkpoint_required_before_activation"])
                self.assertFalse(decision["route_proposal_required"])

    def test_request_must_match_current_project_and_work_revisions(self):
        for field, value in (
            ("project_id", "other-project"),
            ("expected_project_revision", 42),
            ("active_work_id", "work-checkpoint"),
            ("expected_active_work_revision", 2),
        ):
            with self.subTest(field=field):
                with self.assertRaises(StickyRouteError):
                    route_task_input(self._request("continue", **{field: value}), self.state)

    def test_every_route_is_input_and_authoritative_state_side_effect_free(self):
        cases = (
            self._request("continue"),
            self._request("idea"),
            self._request("correction"),
            self._request("child_work"),
            self._switch_request(),
        )
        for request in cases:
            with self.subTest(input_kind=request["input_kind"]):
                request_before = copy.deepcopy(request)
                state_before = copy.deepcopy(self.state)
                route_task_input(request, self.state)
                self.assertEqual(request, request_before)
                self.assertEqual(self.state, state_before)
                self.assertEqual(self.state["project"]["revision"], 43)
                self.assertEqual(self.state["ideas"], [])
                self.assertEqual(self.state["decisions"], [])
                self.assertEqual(self.state["effects"], [])

    def test_request_and_decision_schemas_are_strict_registered_and_hashed(self):
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        schemas = {}
        for schema_id, relative in (
            ("context.task-route-request", "schemas/m3-02/task-route-request.schema.json"),
            ("context.task-route-decision", "schemas/m3-02/task-route-decision.schema.json"),
        ):
            entry = next(item for item in registry["schemas"] if item["schema_id"] == schema_id)
            path = self.root / relative
            self.assertEqual(entry["artifact_path"], relative)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["content_sha256"])
            schemas[schema_id] = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(schemas[schema_id]["additionalProperties"])

        request = self._switch_request()
        decision = route_task_input(request, self.state)
        Draft202012Validator(schemas["context.task-route-request"]).validate(request)
        Draft202012Validator(schemas["context.task-route-decision"]).validate(decision)
        for document, schema in ((request, schemas["context.task-route-request"]), (decision, schemas["context.task-route-decision"])):
            invalid = copy.deepcopy(document)
            invalid["unexpected"] = True
            self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

        request_validator = Draft202012Validator(schemas["context.task-route-request"])
        invalid_request = self._request("child_work", target_work_id="work-checkpoint")
        self.assertTrue(list(request_validator.iter_errors(invalid_request)))
        invalid_request = self._request(
            "continue",
            user_authorization_candidate=True,
            authorization_candidate_ref="source://opaque/thread/auth-range-001",
        )
        self.assertTrue(list(request_validator.iter_errors(invalid_request)))
        invalid_request = self._request("continue", classifier_provenance_ref="   ")
        self.assertTrue(list(request_validator.iter_errors(invalid_request)))

        decision_validator = Draft202012Validator(schemas["context.task-route-decision"])
        invalid_decision = copy.deepcopy(decision)
        invalid_decision["input_kind"] = "idea"
        self.assertTrue(list(decision_validator.iter_errors(invalid_decision)))
        invalid_decision = copy.deepcopy(decision)
        invalid_decision["target_work_id"] = None
        invalid_decision["target_work_revision"] = None
        self.assertTrue(list(decision_validator.iter_errors(invalid_decision)))
        invalid_decision = route_task_input(self._request("status_query"), self.state)
        invalid_decision["authorization_candidate_ref"] = "source://opaque/thread/auth-001"
        self.assertTrue(list(decision_validator.iter_errors(invalid_decision)))
        with self.assertRaises(StickyRouteError):
            canonical_route_decision_bytes(invalid_decision)
        invalid_decision = route_task_input(self._request("status_query"), self.state)
        invalid_decision["target_work_id"] = "work-checkpoint"
        invalid_decision["target_work_revision"] = 1
        self.assertTrue(list(decision_validator.iter_errors(invalid_decision)))
        with self.assertRaises(StickyRouteError):
            canonical_route_decision_bytes(invalid_decision)

    def test_canonical_replay_binds_request_provenance_and_rejects_tampering(self):
        fixture = json.loads(
            (self.root / "experiments/routing/m3-02-route-replay.json").read_text(
                encoding="utf-8"
            )
        )
        request = fixture["request"]
        state = fixture["state"]
        decision = route_task_input(request, state)
        request_bytes = canonical_route_request_bytes(request)
        decision_bytes = canonical_route_decision_bytes(decision)

        self.assertEqual(decision, fixture["expected_decision"])
        self.assertEqual(hashlib.sha256(request_bytes).hexdigest(), fixture["request_sha256"])
        self.assertEqual(hashlib.sha256(decision_bytes).hexdigest(), fixture["decision_sha256"])
        self.assertEqual(json.loads(request_bytes), request)
        self.assertEqual(json.loads(decision_bytes), decision)
        self.assertEqual(decision["request_sha256"], fixture["request_sha256"])

        tampered = copy.deepcopy(decision)
        tampered["authorization_verified"] = True
        with self.assertRaises(StickyRouteError):
            canonical_route_decision_bytes(tampered)
        tampered = copy.deepcopy(decision)
        tampered["active_work_revision_after"] += 1
        with self.assertRaises(StickyRouteError):
            canonical_route_decision_bytes(tampered)

    def test_one_thousand_route_decisions_cover_fixture_matrix_and_stay_bounded(self):
        fixture = yaml.safe_load(
            (self.root / "experiments/routing/m3-02-sticky-router.yaml").read_text(
                encoding="utf-8"
            )
        )
        requests = []
        for input_kind, count in fixture["cases"].items():
            for index in range(count):
                updates = {"request_id": f"route-{input_kind}-{index}"}
                if input_kind in {"interrupt", "switch"}:
                    updates.update(
                        {
                            "target_work_id": "work-checkpoint",
                            "user_authorization_candidate": index % 2 == 0,
                            "authorization_candidate_ref": (
                                f"source://opaque/thread/auth-{index}"
                                if index % 2 == 0
                                else None
                            ),
                        }
                    )
                requests.append(self._request(input_kind, **updates))

        result = benchmark_sticky_routes(requests, self.state)
        self.assertEqual(result["sample_count"], fixture["sample_count"])
        self.assertEqual(result["active_leaf_preserved"], fixture["sample_count"])
        self.assertEqual(result["deterministic_replays"], fixture["sample_count"])
        self.assertEqual(result["unauthorized_switches"], 0)
        self.assertTrue(result["requests_unchanged"])
        self.assertTrue(result["state_unchanged"])
        self.assertLess(
            result["p95_route_latency_ms"],
            fixture["acceptance"]["p95_route_latency_ms_max"],
        )
        self.assertLess(
            result["p50_route_latency_ms"],
            fixture["acceptance"]["p50_route_latency_ms_max"],
        )

    def test_verification_receipt_binds_current_artifacts_and_observed_metrics(self):
        receipt = json.loads(
            (self.root / "experiments/routing/m3-02-verification-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["schema_version"], "context.routing-verification/v1alpha1")
        self.assertEqual(receipt["task_id"], "M3-02")
        expected_artifacts = {
            "context_control_plane/sticky_router.py",
            "context_control_plane/sticky_router_benchmark.py",
            "experiments/routing/m3-02-route-replay.json",
            "experiments/routing/m3-02-sticky-router.yaml",
            "schemas/m3-02/task-route-decision.schema.json",
            "schemas/m3-02/task-route-request.schema.json",
            "tests/test_m3_02_sticky_router.py",
        }
        artifact_paths = []
        for artifact in receipt["artifacts"]:
            self.assertEqual(set(artifact), {"path", "content_sha256"})
            relative = artifact["path"]
            digest = artifact["content_sha256"]
            self.assertFalse(Path(relative).is_absolute())
            self.assertNotIn("..", Path(relative).parts)
            artifact_paths.append(relative)
            self.assertEqual(
                hashlib.sha256((self.root / relative).read_bytes()).hexdigest(), digest
            )
        self.assertEqual(len(artifact_paths), len(set(artifact_paths)))
        self.assertEqual(set(artifact_paths), expected_artifacts)
        metrics = receipt["metrics"]
        self.assertEqual(metrics["sample_count"], 1_000)
        self.assertEqual(metrics["active_leaf_preserved"], 1_000)
        self.assertEqual(metrics["deterministic_replays"], 1_000)
        self.assertEqual(metrics["unauthorized_switches"], 0)
        self.assertTrue(metrics["state_unchanged"])
        self.assertLess(metrics["p95_route_latency_ms"], 10.0)

        fixture = yaml.safe_load(
            (self.root / "experiments/routing/m3-02-sticky-router.yaml").read_text(
                encoding="utf-8"
            )
        )
        requests = []
        for input_kind, count in fixture["cases"].items():
            for index in range(count):
                updates = {"request_id": f"receipt-{input_kind}-{index}"}
                if input_kind in {"interrupt", "switch"}:
                    updates.update(
                        {
                            "target_work_id": "work-checkpoint",
                            "user_authorization_candidate": index % 2 == 0,
                            "authorization_candidate_ref": (
                                f"source://opaque/thread/receipt-auth-{index}"
                                if index % 2 == 0
                                else None
                            ),
                        }
                    )
                requests.append(self._request(input_kind, **updates))
        live = benchmark_sticky_routes(requests, self.state)
        for field in (
            "sample_count",
            "active_leaf_preserved",
            "deterministic_replays",
            "unauthorized_switches",
            "requests_unchanged",
            "state_unchanged",
        ):
            self.assertEqual(live[field], metrics[field])


if __name__ == "__main__":
    unittest.main()
