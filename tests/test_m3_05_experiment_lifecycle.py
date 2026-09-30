import concurrent.futures
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from context_control_plane.experiment_lifecycle import (
    EXPERIMENT_LIFECYCLE_VERDICT_SCHEMA_VERSION,
    evaluate_attempt_gate,
    experiment_contract_sha256,
)
from context_control_plane.experiment_lifecycle_benchmark import (
    run_experiment_lifecycle_benchmark,
    validate_experiment_lifecycle_benchmark_receipt,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
)
from context_control_plane.state_mcp import RequestContext, StateMCPService
from context_control_plane.typed_state import validate_typed_state
from context_control_plane.typed_state_migration import (
    migrate_v2alpha1_to_v3alpha1,
    rollback_v3alpha1_to_v2alpha1,
)


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M305ExperimentLifecycleTests(unittest.TestCase):
    @staticmethod
    def snapshot():
        return {
            "schema_version": "context.typed-state/v3alpha1",
            "project": {
                "project_id": "project-experiment",
                "revision": 9,
                "governance_ref": "artifact://governance/m305",
                "active_work_ids": ["experiment-throughput"],
                "primary_work_id": "experiment-throughput",
                "current_decision_ids": [],
                "active_constraint_ids": [],
                "open_blocker_ids": [],
                "effect_high_watermark": 0,
                "updated_at": "2026-08-14T08:00:00+08:00",
            },
            "works": [
                {
                    "work_id": "campaign",
                    "kind": "campaign",
                    "title": "Campaign",
                    "status": "ready",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": ["actor-owner"],
                    "scope_refs": [
                        {"scope_kind": "capability", "scope_ref": "campaign"}
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
                },
                {
                    "work_id": "mainline-target",
                    "kind": "goal",
                    "title": "Mainline target",
                    "status": "ready",
                    "parent_work_id": "campaign",
                    "dependency_ids": [],
                    "owner_refs": ["actor-owner"],
                    "scope_refs": [
                        {"scope_kind": "capability", "scope_ref": "mainline"}
                    ],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": [],
                    "revision": 2,
                    "return_point_work_id": None,
                    "exit_criteria": [],
                    "attempt_budget": None,
                    "expires_at": None,
                    "promotion_target_work_id": None,
                    "mainline_authority": True,
                },
                {
                    "work_id": "experiment-throughput",
                    "kind": "experiment",
                    "title": "Throughput experiment",
                    "status": "active",
                    "parent_work_id": "mainline-target",
                    "dependency_ids": [],
                    "owner_refs": ["actor-owner"],
                    "scope_refs": [
                        {"scope_kind": "capability", "scope_ref": "experiment"}
                    ],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": [],
                    "revision": 3,
                    "return_point_work_id": "mainline-target",
                    "exit_criteria": ["throughput target", "recovery target"],
                    "attempt_budget": 2,
                    "expires_at": "2026-08-14T09:00:00+08:00",
                    "promotion_target_work_id": "mainline-target",
                    "mainline_authority": False,
                },
            ],
            "claims": [
                {
                    "claim_id": "claim-experiment",
                    "work_id": "experiment-throughput",
                    "actor_ref": "actor-owner",
                    "status": "active",
                    "expected_project_revision": 9,
                    "claimed_at": "2026-08-14T07:30:00+08:00",
                    "lease_expires_at": "2026-08-14T10:00:00+08:00",
                    "released_at": None,
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "experiment"}
                    ],
                }
            ],
            "ideas": [],
            "decisions": [],
            "constraints": [],
            "evidence": [],
            "blockers": [],
            "effects": [],
            "experiment_attempts": [],
            "experiment_promotions": [],
        }

    def test_first_attempt_requires_an_active_claim_and_trusted_time(self):
        validate_typed_state(self.snapshot())
        verdict = evaluate_attempt_gate(
            self.snapshot(),
            actor_ref="actor-owner",
            work_id="experiment-throughput",
            claim_id="claim-experiment",
            expected_revision=9,
            observed_at="2026-08-14T08:30:00+08:00",
        )

        self.assertEqual(
            verdict,
            {
                "schema_version": EXPERIMENT_LIFECYCLE_VERDICT_SCHEMA_VERSION,
                "decision": "allow",
                "read_only": False,
                "reason": "authorized",
                "attempt_no": 1,
            },
        )

        expired = copy.deepcopy(self.snapshot())
        expired["project"]["updated_at"] = "2026-08-14T09:00:00+08:00"
        verdict = evaluate_attempt_gate(
            expired,
            actor_ref="actor-owner",
            work_id="experiment-throughput",
            claim_id="claim-experiment",
            expected_revision=9,
            observed_at="2026-08-14T09:00:00+08:00",
        )
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "experiment_expired")
        self.assertTrue(verdict["read_only"])

    def test_state_mcp_attempt_consumes_one_budget_slot_and_replays(self):
        snapshot = self.snapshot()
        request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-throughput-1",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "attempt_id": "attempt-throughput-1",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            context = RequestContext("actor-owner", "authorization-verifier")
            first = service.call_tool(
                "context.experiment.attempt", request, context=context
            )
            replay = service.call_tool(
                "context.experiment.attempt", request, context=context
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(replay, first)
        self.assertEqual(stored["project"]["revision"], 10)
        self.assertEqual(len(stored["experiment_attempts"]), 1)
        self.assertEqual(stored["experiment_attempts"][0]["attempt_no"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["schema_version"], "context.state-event/v4alpha1")
        self.assertEqual(
            events[0]["experiment_transition"]["operation"], "attempt-started"
        )

    def test_attempt_replays_after_a_new_service_process(self):
        snapshot = self.snapshot()
        request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-durable-replay-1",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "attempt_id": "attempt-durable-replay-1",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            first = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.experiment.attempt",
                request,
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            replay = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.experiment.attempt",
                request,
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(replay, first)
        self.assertEqual(len(events), 1)

    def test_attempt_budget_is_exhausted_without_a_second_event(self):
        snapshot = self.snapshot()

        def request(attempt_id, revision):
            return {
                "schema_version": "context.experiment-attempt-request/v1alpha1",
                "request_id": attempt_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": revision,
                "attempt_id": attempt_id,
                "work_id": "experiment-throughput",
                "claim_id": "claim-experiment",
                "causation_ref": "work:M3-05",
                "correlation_ref": "campaign:M3",
            }

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            first = service.call_tool(
                "context.experiment.attempt",
                request("attempt-budget-1", 9),
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            second = service.call_tool(
                "context.experiment.attempt",
                request("attempt-budget-2", 10),
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            rejected = service.call_tool(
                "context.experiment.attempt",
                request("attempt-budget-3", 11),
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(second["ok"], second["error"])
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "integrity")
        self.assertIn("attempt_budget_exhausted", rejected["error"]["message"])
        self.assertEqual(len(stored["experiment_attempts"]), 2)
        self.assertEqual(len(events), 2)

    def test_concurrent_final_attempts_persist_exactly_one_budget_slot(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        experiment["attempt_budget"] = 1

        def request(attempt_id):
            return {
                "schema_version": "context.experiment-attempt-request/v1alpha1",
                "request_id": attempt_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
                "attempt_id": attempt_id,
                "work_id": "experiment-throughput",
                "claim_id": "claim-experiment",
                "causation_ref": "work:M3-05",
                "correlation_ref": "campaign:M3",
            }

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            barrier = __import__("threading").Barrier(2)

            def submit(attempt_id):
                service = StateMCPService(
                    store,
                    authorizer=_AllowAuthorizer(),
                    registry_digest="a" * 64,
                    clock=lambda: "2026-08-14T08:30:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                )
                barrier.wait(timeout=5)
                return service.call_tool(
                    "context.experiment.attempt",
                    request(attempt_id),
                    context=RequestContext("actor-owner", "authorization-executor"),
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(
                        submit, ("attempt-concurrent-a", "attempt-concurrent-b")
                    )
                )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertEqual(sum(response["ok"] for response in responses), 1)
        self.assertEqual(len(stored["experiment_attempts"]), 1)
        self.assertEqual(stored["experiment_attempts"][0]["attempt_no"], 1)
        self.assertEqual(len(events), 1)

    def test_attempt_rejects_a_claim_that_has_reached_its_lease_boundary(self):
        snapshot = self.snapshot()
        snapshot["claims"][0]["lease_expires_at"] = "2026-08-14T08:30:00+08:00"
        request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-expired-claim",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "attempt_id": "attempt-expired-claim",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.experiment.attempt",
                request,
                context=RequestContext("actor-owner", "authorization-verifier"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "integrity")
        self.assertIn("claim_expired", response["error"]["message"])
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_state_claim_cannot_activate_an_expired_experiment(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        experiment["status"] = "ready"
        snapshot["project"]["active_work_ids"] = []
        snapshot["project"]["primary_work_id"] = None
        snapshot["claims"] = []
        validate_typed_state(snapshot)
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "claim-expired-experiment",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "work_id": experiment["work_id"],
            "claim_id": "claim-after-expiry",
            "scope_owners": copy.deepcopy(experiment["scope_refs"]),
            "lease_expires_at": "2026-08-14T10:00:00+08:00",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T09:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.claim",
                request,
                context=RequestContext("actor-owner", "authorization-owner"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertFalse(response["ok"])
        self.assertIn("experiment_expired", response["error"]["message"])
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_state_claim_cannot_reactivate_an_exhausted_experiment(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        experiment["status"] = "ready"
        snapshot["project"]["active_work_ids"] = []
        snapshot["project"]["primary_work_id"] = None
        claim = snapshot["claims"][0]
        claim["status"] = "released"
        claim["released_at"] = "2026-08-14T08:20:00+08:00"
        snapshot["experiment_attempts"] = [
            {
                "attempt_id": f"exhausted-attempt-{number}",
                "work_id": experiment["work_id"],
                "claim_id": claim["claim_id"],
                "actor_ref": claim["actor_ref"],
                "attempt_no": number,
                "experiment_contract_sha256": experiment_contract_sha256(experiment),
                "started_at": f"2026-08-14T08:0{number}:00+08:00",
            }
            for number in (1, 2)
        ]
        validate_typed_state(snapshot)
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "claim-exhausted-experiment",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "work_id": experiment["work_id"],
            "claim_id": "claim-after-budget",
            "scope_owners": copy.deepcopy(experiment["scope_refs"]),
            "lease_expires_at": "2026-08-14T10:00:00+08:00",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.claim",
                request,
                context=RequestContext("actor-owner", "authorization-owner"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertFalse(response["ok"])
        self.assertIn("attempt_budget_exhausted", response["error"]["message"])
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_promotion_requires_a_separate_verifier_and_verified_exit_evidence(self):
        snapshot = self.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("evidence-throughput", "a"),
                ("evidence-recovery", "b"),
            )
        ]
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-for-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-for-promotion",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        proposal_request = {
            "schema_version": "context.experiment-promotion-proposal-request/v1alpha1",
            "request_id": "promotion-proposal-1",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": "attempt-for-promotion",
            "proposal_id": "promotion-proposal-1",
            "criterion_evidence": {
                "throughput target": ["evidence-throughput"],
                "recovery target": ["evidence-recovery"],
            },
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        approval_request = {
            "schema_version": "context.experiment-promotion-approval-request/v1alpha1",
            "request_id": "promotion-approval-1",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 11,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": "promotion-proposal-1",
            "approval_id": "promotion-approval-1",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = service.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            proposal = service.call_tool(
                "context.experiment.promotion.propose",
                proposal_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            self_approval = service.call_tool(
                "context.experiment.promotion.approve",
                approval_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            approval = service.call_tool(
                "context.experiment.promotion.approve",
                approval_request,
                context=RequestContext("actor-verifier", "authorization-verifier"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertFalse(self_approval["ok"])
        self.assertIn(
            "independent_verifier_required", self_approval["error"]["message"]
        )
        self.assertTrue(approval["ok"], approval["error"])
        self.assertEqual(stored["project"]["revision"], 12)
        self.assertEqual(
            [item["kind"] for item in stored["experiment_promotions"]],
            ["proposed", "approved"],
        )
        self.assertEqual(
            stored["experiment_promotions"][1]["actor_ref"], "actor-verifier"
        )
        self.assertEqual(
            events[-1]["experiment_transition"]["operation"], "promotion-approved"
        )

    def test_promotion_approval_replays_after_a_new_service_process(self):
        snapshot = self.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("evidence-throughput", "a"),
                ("evidence-recovery", "b"),
            )
        ]
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-promotion-replay",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-promotion-replay",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        proposal_request = {
            "schema_version": "context.experiment-promotion-proposal-request/v1alpha1",
            "request_id": "promotion-proposal-replay",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": "attempt-promotion-replay",
            "proposal_id": "promotion-proposal-replay",
            "criterion_evidence": {
                "throughput target": ["evidence-throughput"],
                "recovery target": ["evidence-recovery"],
            },
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        approval_request = {
            "schema_version": "context.experiment-promotion-approval-request/v1alpha1",
            "request_id": "promotion-approval-replay",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 11,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": "promotion-proposal-replay",
            "approval_id": "promotion-approval-replay",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            first_service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = first_service.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            proposal = first_service.call_tool(
                "context.experiment.promotion.propose",
                proposal_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            first = first_service.call_tool(
                "context.experiment.promotion.approve",
                approval_request,
                context=RequestContext("actor-verifier", "authorization-verifier"),
            )
            replay = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.experiment.promotion.approve",
                approval_request,
                context=RequestContext("actor-verifier", "authorization-verifier"),
            )
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(replay, first)
        self.assertEqual(len(events), 3)

    def test_promotion_approval_rejects_a_proposal_with_stale_frozen_work_revisions(
        self,
    ):
        snapshot = self.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("evidence-throughput", "a"),
                ("evidence-recovery", "b"),
            )
        ]
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-stale-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-stale-promotion",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        proposal_request = {
            "schema_version": "context.experiment-promotion-proposal-request/v1alpha1",
            "request_id": "proposal-stale-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": "attempt-stale-promotion",
            "proposal_id": "proposal-stale-promotion",
            "criterion_evidence": {
                "throughput target": ["evidence-throughput"],
                "recovery target": ["evidence-recovery"],
            },
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        approval_request = {
            "schema_version": "context.experiment-promotion-approval-request/v1alpha1",
            "request_id": "approval-stale-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 11,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": "proposal-stale-promotion",
            "approval_id": "approval-stale-promotion",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = service.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            proposal = service.call_tool(
                "context.experiment.promotion.propose",
                proposal_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            stored_before = store.read_project(snapshot["project"]["project_id"])
            changed_target = copy.deepcopy(
                next(
                    item
                    for item in stored_before["works"]
                    if item["work_id"] == "mainline-target"
                )
            )
            changed_target["revision"] += 1
            commit = service.call_tool(
                "context.state.commit",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "advance-mainline-target",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 11,
                    "causation_ref": "work:M3-05",
                    "correlation_ref": "campaign:M3",
                    "supersedes_event_id": None,
                    "changes": [
                        {
                            "collection": "works",
                            "object_id": changed_target["work_id"],
                            "value": changed_target,
                        }
                    ],
                },
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            stale = service.call_tool(
                "context.experiment.promotion.approve",
                {
                    **approval_request,
                    "expected_revision": 12,
                    "expected_target_work_revision": 3,
                },
                context=RequestContext("actor-verifier", "authorization-verifier"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertTrue(commit["ok"], commit["error"])
        self.assertFalse(stale["ok"])
        self.assertIn("proposal revisions are stale", stale["error"]["message"])
        self.assertEqual(
            [item["kind"] for item in stored["experiment_promotions"]], ["proposed"]
        )
        self.assertEqual(len(events), 3)

    def test_promotion_approval_rejects_a_pending_experiment_effect(self):
        snapshot = self.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("evidence-throughput", "a"),
                ("evidence-recovery", "b"),
            )
        ]
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-pending-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-pending-promotion",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        proposal_request = {
            "schema_version": "context.experiment-promotion-proposal-request/v1alpha1",
            "request_id": "proposal-pending-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": "attempt-pending-promotion",
            "proposal_id": "proposal-pending-promotion",
            "criterion_evidence": {
                "throughput target": ["evidence-throughput"],
                "recovery target": ["evidence-recovery"],
            },
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        effect_request = {
            "schema_version": "context.experiment-effect-request/v1alpha1",
            "request_id": "effect-pending-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 11,
            "action": "authorize",
            "effect_id": "effect-pending-promotion",
            "effect_key": "effect-key-pending-promotion",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "attempt_id": "attempt-pending-promotion",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        approval_request = {
            "schema_version": "context.experiment-promotion-approval-request/v1alpha1",
            "request_id": "approval-pending-promotion",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 12,
            "work_id": "experiment-throughput",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": "proposal-pending-promotion",
            "approval_id": "approval-pending-promotion",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = service.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            proposal = service.call_tool(
                "context.experiment.promotion.propose",
                proposal_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            effect = service.call_tool(
                "context.experiment.effect",
                effect_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            approval = service.call_tool(
                "context.experiment.promotion.approve",
                approval_request,
                context=RequestContext("actor-verifier", "authorization-verifier"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertTrue(effect["ok"], effect["error"])
        self.assertFalse(approval["ok"])
        self.assertIn("no pending Experiment effect", approval["error"]["message"])
        self.assertEqual(
            [item["kind"] for item in stored["experiment_promotions"]], ["proposed"]
        )
        self.assertEqual(len(events), 3)

    def test_experiment_effect_requires_a_persisted_attempt_provenance(self):
        snapshot = self.snapshot()
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-for-effect",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-for-effect",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        generic_effect = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "generic-experiment-effect",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "action": "authorize",
            "effect_id": "effect-experiment-1",
            "effect_key": "effect-key-experiment-1",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        experiment_effect = {
            **generic_effect,
            "schema_version": "context.experiment-effect-request/v1alpha1",
            "request_id": "experiment-effect-1",
            "attempt_id": "attempt-for-effect",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = service.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            rejected = service.call_tool(
                "context.state.effect",
                generic_effect,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            authorized = service.call_tool(
                "context.experiment.effect",
                experiment_effect,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertFalse(rejected["ok"])
        self.assertIn("Experiment effect requires", rejected["error"]["message"])
        self.assertTrue(authorized["ok"], authorized["error"])
        self.assertEqual(stored["effects"][0]["attempt_id"], "attempt-for-effect")

    def test_generic_effect_preflight_denies_an_experiment_without_attempt_provenance(
        self,
    ):
        snapshot = self.snapshot()
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "generic-experiment-preflight",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "effect_id": "effect-generic-preflight",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.effect.gate",
                request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["result"]["verdict"]["decision"], "deny")
        self.assertEqual(
            response["result"]["verdict"]["reason"],
            "experiment_attempt_provenance_required",
        )
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_expired_experiment_cannot_authorize_an_effect_after_a_valid_attempt(self):
        snapshot = self.snapshot()
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "attempt-before-expiry",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "attempt_id": "attempt-before-expiry",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        effect_request = {
            "schema_version": "context.experiment-effect-request/v1alpha1",
            "request_id": "effect-after-expiry",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "action": "authorize",
            "effect_id": "effect-after-expiry",
            "effect_key": "effect-key-after-expiry",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "attempt_id": "attempt-before-expiry",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            before_expiry = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            attempt = before_expiry.call_tool(
                "context.experiment.attempt",
                attempt_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T09:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.experiment.effect",
                effect_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertFalse(response["ok"])
        self.assertIn("experiment_expired", response["error"]["message"])
        self.assertEqual(len(stored["experiment_attempts"]), 1)
        self.assertEqual(stored["effects"], [])
        self.assertEqual(len(events), 1)

    def test_final_budget_attempt_can_authorize_its_own_effect(self):
        snapshot = self.snapshot()

        def attempt_request(attempt_id, revision):
            return {
                "schema_version": "context.experiment-attempt-request/v1alpha1",
                "request_id": attempt_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": revision,
                "attempt_id": attempt_id,
                "work_id": "experiment-throughput",
                "claim_id": "claim-experiment",
                "causation_ref": "work:M3-05",
                "correlation_ref": "campaign:M3",
            }

        effect_request = {
            "schema_version": "context.experiment-effect-request/v1alpha1",
            "request_id": "effect-final-budget-attempt",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 11,
            "action": "authorize",
            "effect_id": "effect-final-budget-attempt",
            "effect_key": "effect-key-final-budget-attempt",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "attempt_id": "attempt-final-budget",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            first = service.call_tool(
                "context.experiment.attempt",
                attempt_request("attempt-first-budget", 9),
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            final = service.call_tool(
                "context.experiment.attempt",
                attempt_request("attempt-final-budget", 10),
                context=RequestContext("actor-owner", "authorization-executor"),
            )
            effect = service.call_tool(
                "context.experiment.effect",
                effect_request,
                context=RequestContext("actor-owner", "authorization-executor"),
            )

        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(final["ok"], final["error"])
        self.assertTrue(effect["ok"], effect["error"])

    def test_v2_v3_migration_is_idempotent_and_lifecycle_history_blocks_rollback(self):
        v2 = self.snapshot()
        v2["schema_version"] = "context.typed-state/v2alpha1"
        del v2["experiment_attempts"]
        del v2["experiment_promotions"]
        migrated = migrate_v2alpha1_to_v3alpha1(v2)

        self.assertEqual(migrate_v2alpha1_to_v3alpha1(migrated), migrated)
        self.assertEqual(rollback_v3alpha1_to_v2alpha1(migrated), v2)
        with_history = copy.deepcopy(migrated)
        experiment = next(
            item for item in with_history["works"] if item["kind"] == "experiment"
        )
        with_history["experiment_attempts"] = [
            {
                "attempt_id": "rollback-blocker-attempt",
                "work_id": experiment["work_id"],
                "claim_id": "claim-experiment",
                "actor_ref": "actor-owner",
                "attempt_no": 1,
                "experiment_contract_sha256": experiment_contract_sha256(experiment),
                "started_at": "2026-08-14T08:30:00+08:00",
            }
        ]
        validate_typed_state(with_history)
        with self.assertRaisesRegex(ValueError, "prevents v2 rollback"):
            rollback_v3alpha1_to_v2alpha1(with_history)

    def test_reducer_rejects_attempt_rewrite_and_contract_drift_after_attempt(self):
        snapshot = self.snapshot()
        attempt = {
            "attempt_id": "immutable-attempt",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(
                next(item for item in snapshot["works"] if item["kind"] == "experiment")
            ),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        after_attempt = copy.deepcopy(snapshot)
        after_attempt["experiment_attempts"] = [attempt]
        after_attempt["project"]["revision"] = 10
        after_attempt["project"]["updated_at"] = "2026-08-14T08:30:00+08:00"
        after_attempt["claims"][0]["expected_project_revision"] = 10
        attempt_event = build_state_event(
            event_id="event-immutable-attempt",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at="2026-08-14T08:30:00+08:00",
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": after_attempt["claims"][0],
                },
            ],
            project_after=after_attempt["project"],
            experiment_transition={
                "operation": "attempt-started",
                "request_sha256": "a" * 64,
                "attempt_id": attempt["attempt_id"],
                "promotion_id": None,
                "proposal_id": None,
            },
        )
        restored = replay_state_events(snapshot, [attempt_event])
        self.assertEqual(restored, after_attempt)

        rewritten = copy.deepcopy(attempt)
        rewritten["started_at"] = "2026-08-14T08:31:00+08:00"
        rewrite_after = copy.deepcopy(after_attempt)
        rewrite_after["project"]["revision"] = 11
        rewrite_after["project"]["updated_at"] = "2026-08-14T08:31:00+08:00"
        rewrite_after["claims"][0]["expected_project_revision"] = 11
        rewrite_after["experiment_attempts"] = [rewritten]
        rewrite_event = build_state_event(
            event_id="event-immutable-attempt-rewrite",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=2,
            revision_before=10,
            occurred_at="2026-08-14T08:31:00+08:00",
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=attempt_event["event_sha256"],
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "experiment_attempts",
                    "object_id": rewritten["attempt_id"],
                    "value": rewritten,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": rewrite_after["claims"][0],
                },
            ],
            project_after=rewrite_after["project"],
            experiment_transition=None,
            schema_version="context.state-event/v4alpha1",
        )
        with self.assertRaisesRegex(StateEventError, "append-only"):
            replay_state_events(
                after_attempt,
                [rewrite_event],
                starting_sequence_no=2,
                previous_event_sha256=attempt_event["event_sha256"],
            )

        drifted = copy.deepcopy(
            next(
                item for item in after_attempt["works"] if item["kind"] == "experiment"
            )
        )
        drifted["attempt_budget"] = 3
        drifted["revision"] += 1
        drift_after = copy.deepcopy(after_attempt)
        drift_after["project"]["revision"] = 11
        drift_after["project"]["updated_at"] = "2026-08-14T08:31:00+08:00"
        drift_after["claims"][0]["expected_project_revision"] = 11
        next(
            item for item in drift_after["works"] if item["kind"] == "experiment"
        ).update(drifted)
        drift_event = build_state_event(
            event_id="event-contract-drift",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=2,
            revision_before=10,
            occurred_at="2026-08-14T08:31:00+08:00",
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=attempt_event["event_sha256"],
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "works",
                    "object_id": drifted["work_id"],
                    "value": drifted,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": drift_after["claims"][0],
                },
            ],
            project_after=drift_after["project"],
            schema_version="context.state-event/v4alpha1",
        )
        with self.assertRaisesRegex(StateEventError, "contract is immutable"):
            replay_state_events(
                after_attempt,
                [drift_event],
                starting_sequence_no=2,
                previous_event_sha256=attempt_event["event_sha256"],
            )

    def test_typed_state_rejects_lifecycle_records_after_experiment_expiry(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        snapshot["experiment_attempts"] = [
            {
                "attempt_id": "post-expiry-attempt",
                "work_id": experiment["work_id"],
                "claim_id": "claim-experiment",
                "actor_ref": "actor-owner",
                "attempt_no": 1,
                "experiment_contract_sha256": experiment_contract_sha256(experiment),
                "started_at": "2026-08-14T09:00:00+08:00",
            }
        ]
        with self.assertRaisesRegex(
            ValueError, "experiment attempt cannot start after expiry"
        ):
            validate_typed_state(snapshot)

    def test_typed_state_rejects_promotion_created_at_experiment_expiry(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("expiry-throughput", "a"),
                ("expiry-recovery", "b"),
            )
        ]
        attempt = {
            "attempt_id": "expiry-promotion-attempt",
            "work_id": experiment["work_id"],
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(experiment),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        snapshot["experiment_attempts"] = [attempt]
        snapshot["experiment_promotions"] = [
            {
                "promotion_id": "expiry-promotion",
                "kind": "proposed",
                "proposal_id": "expiry-promotion",
                "work_id": experiment["work_id"],
                "target_work_id": experiment["promotion_target_work_id"],
                "actor_ref": "actor-owner",
                "source_work_revision": experiment["revision"],
                "target_work_revision": 2,
                "attempt_id": attempt["attempt_id"],
                "experiment_contract_sha256": attempt["experiment_contract_sha256"],
                "criterion_evidence": {
                    "throughput target": ["expiry-throughput"],
                    "recovery target": ["expiry-recovery"],
                },
                "created_at": experiment["expires_at"],
            }
        ]

        with self.assertRaisesRegex(
            ValueError, "experiment promotion cannot be created after expiry"
        ):
            validate_typed_state(snapshot)

    def test_replay_rejects_lifecycle_event_at_experiment_expiry(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        attempt = {
            "attempt_id": "expiry-event-attempt",
            "work_id": experiment["work_id"],
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(experiment),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        after = copy.deepcopy(snapshot)
        after["experiment_attempts"] = [attempt]
        after["project"]["revision"] = 10
        after["project"]["updated_at"] = experiment["expires_at"]
        after["claims"][0]["expected_project_revision"] = 10
        event = build_state_event(
            event_id="event-at-experiment-expiry",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at=experiment["expires_at"],
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": after["claims"][0],
                },
            ],
            project_after=after["project"],
            experiment_transition={
                "operation": "attempt-started",
                "request_sha256": "a" * 64,
                "attempt_id": attempt["attempt_id"],
                "promotion_id": None,
                "proposal_id": None,
            },
            schema_version="context.state-event/v4alpha1",
        )

        with self.assertRaisesRegex(
            StateEventError, "lifecycle event occurs after Experiment expiry"
        ):
            replay_state_events(snapshot, [event])

    def test_replay_rejects_unbound_lifecycle_event(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        attempt = {
            "attempt_id": "unbound-lifecycle-event",
            "work_id": experiment["work_id"],
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(experiment),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        after = copy.deepcopy(snapshot)
        after["experiment_attempts"] = [attempt]
        after["project"]["revision"] = 10
        after["project"]["updated_at"] = "2026-08-14T08:30:00+08:00"
        after["claims"][0]["expected_project_revision"] = 10
        event = build_state_event(
            event_id="event-unbound-lifecycle",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at="2026-08-14T08:30:00+08:00",
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": after["claims"][0],
                },
            ],
            project_after=after["project"],
            schema_version="context.state-event/v4alpha1",
        )
        with self.assertRaisesRegex(
            StateEventError, "lifecycle change requires experiment transition"
        ):
            replay_state_events(snapshot, [event])

    def test_generic_commit_cannot_append_an_experiment_attempt(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        attempt = {
            "attempt_id": "forged-generic-attempt",
            "work_id": experiment["work_id"],
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(experiment),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "generic-lifecycle-forgery",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:30:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.commit",
                request,
                context=RequestContext("actor-owner", "authorization-owner"),
            )

            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "invalid_request")
            self.assertEqual(
                store.read_project(snapshot["project"]["project_id"]), snapshot
            )
            self.assertEqual(store.read_events(snapshot["project"]["project_id"]), [])

    def test_event_lifecycle_transition_requires_its_ledger_change(self):
        snapshot = self.snapshot()
        after = copy.deepcopy(snapshot)
        after["project"]["revision"] = 10
        after["project"]["updated_at"] = "2026-08-14T08:30:00+08:00"
        after["claims"][0]["expected_project_revision"] = 10

        with self.assertRaisesRegex(
            StateEventError, "attempt transition requires its ledger change"
        ):
            build_state_event(
                event_id="event-forged-attempt-transition",
                event_type="state-transition",
                project_id=snapshot["project"]["project_id"],
                sequence_no=1,
                revision_before=9,
                occurred_at="2026-08-14T08:30:00+08:00",
                actor_ref="actor-owner",
                causation_ref="work:M3-05",
                correlation_ref="campaign:M3",
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "claims",
                        "object_id": "claim-experiment",
                        "value": after["claims"][0],
                    }
                ],
                project_after=after["project"],
                experiment_transition={
                    "operation": "attempt-started",
                    "request_sha256": "a" * 64,
                    "attempt_id": "missing-ledger-attempt",
                    "promotion_id": None,
                    "proposal_id": None,
                },
                schema_version="context.state-event/v4alpha1",
            )

    def test_approved_promotion_preserves_the_proposal_lineage(self):
        snapshot = self.snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["kind"] == "experiment"
        )
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("lineage-throughput", "a"),
                ("lineage-recovery", "b"),
            )
        ]
        attempt = {
            "attempt_id": "lineage-attempt",
            "work_id": experiment["work_id"],
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(experiment),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        proposal = {
            "promotion_id": "lineage-proposal",
            "kind": "proposed",
            "proposal_id": "lineage-proposal",
            "work_id": experiment["work_id"],
            "target_work_id": experiment["promotion_target_work_id"],
            "actor_ref": "actor-owner",
            "source_work_revision": experiment["revision"],
            "target_work_revision": 2,
            "attempt_id": attempt["attempt_id"],
            "experiment_contract_sha256": attempt["experiment_contract_sha256"],
            "criterion_evidence": {
                "throughput target": ["lineage-throughput"],
                "recovery target": ["lineage-recovery"],
            },
            "created_at": "2026-08-14T08:33:00+08:00",
        }
        forged_approval = copy.deepcopy(proposal)
        forged_approval.update(
            {
                "promotion_id": "lineage-approval",
                "kind": "approved",
                "actor_ref": "actor-verifier",
                "criterion_evidence": {
                    "throughput target": ["lineage-recovery"],
                    "recovery target": ["lineage-throughput"],
                },
                "created_at": "2026-08-14T08:34:00+08:00",
            }
        )
        snapshot["experiment_attempts"] = [attempt]
        snapshot["experiment_promotions"] = [proposal, forged_approval]

        with self.assertRaisesRegex(
            ValueError, "approved promotion must preserve its proposal lineage"
        ):
            validate_typed_state(snapshot)

    def test_v3_v4_and_lifecycle_schemas_are_strict_registered_and_hashed(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {
            item["schema_id"]: item
            for item in registry["schemas"]
            if item["schema_id"]
            in {
                "context.typed-state",
                "context.state-event",
                "context.experiment-lifecycle",
            }
        }
        self.assertEqual(
            entries["context.typed-state"]["current_wire_version"],
            "context.typed-state/v6alpha1",
        )
        self.assertIn(
            "context.typed-state/v3alpha1",
            entries["context.typed-state"]["supported_wire_versions"],
        )
        self.assertIn(
            "context.typed-state/v5alpha1",
            entries["context.typed-state"]["supported_wire_versions"],
        )
        self.assertEqual(
            entries["context.state-event"]["current_wire_version"],
            "context.state-event/v4alpha1",
        )
        self.assertEqual(
            entries["context.experiment-lifecycle"]["current_wire_version"],
            "context.experiment-lifecycle/v1alpha1",
        )
        schemas = {}
        for schema_id, entry in entries.items():
            path = root / entry["artifact_path"]
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self.assertTrue(
                schema.get("additionalProperties") is False
                or schema.get("unevaluatedProperties") is False
            )
            schemas[schema_id] = schema

        historical_typed_state = json.loads(
            (root / "schemas/m3-05/typed-state-v3alpha1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(historical_typed_state)
        Draft202012Validator(historical_typed_state).validate(self.snapshot())
        for object_name in (
            "project",
            "work",
            "claim",
            "idea",
            "decision",
            "constraint",
            "evidence",
            "blocker",
            "effect",
            "attempt",
            "promotion",
        ):
            with self.subTest(object_name=object_name):
                definition = schemas["context.typed-state"]["$defs"][object_name]
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(
                    set(definition["required"]),
                    set(definition["properties"]),
                )
        attempt_request = {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": "schema-attempt",
            "project_id": "project-experiment",
            "expected_revision": 9,
            "attempt_id": "attempt-schema",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        Draft202012Validator(schemas["context.experiment-lifecycle"]).validate(
            attempt_request
        )
        invalid = copy.deepcopy(attempt_request)
        invalid["unexpected"] = True
        with self.assertRaises(ValidationError):
            Draft202012Validator(schemas["context.experiment-lifecycle"]).validate(
                invalid
            )

        mixed_variant = copy.deepcopy(attempt_request)
        mixed_variant["approval_id"] = "foreign-approval-id"
        with self.assertRaises(ValidationError):
            Draft202012Validator(schemas["context.experiment-lifecycle"]).validate(
                mixed_variant
            )

        invalid_effect_request = {
            "schema_version": "context.experiment-effect-request/v1alpha1",
            "request_id": "schema-effect",
            "project_id": "project-experiment",
            "expected_revision": 9,
            "action": "authorize",
            "effect_id": "effect-schema",
            "effect_key": "effect-key-schema",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "attempt_id": "attempt-schema",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "experiment"},
            "result_ref": "artifact://result/forbidden-before-completion",
            "evidence_ids": [],
            "causation_ref": "work:M3-05",
            "correlation_ref": "campaign:M3",
        }
        with self.assertRaises(ValidationError):
            Draft202012Validator(schemas["context.experiment-lifecycle"]).validate(
                invalid_effect_request
            )

        attempt = {
            "attempt_id": "schema-event-attempt",
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "actor_ref": "actor-owner",
            "attempt_no": 1,
            "experiment_contract_sha256": experiment_contract_sha256(
                next(
                    item
                    for item in self.snapshot()["works"]
                    if item["kind"] == "experiment"
                )
            ),
            "started_at": "2026-08-14T08:30:00+08:00",
        }
        after_attempt = self.snapshot()
        after_attempt["experiment_attempts"] = [attempt]
        after_attempt["project"]["revision"] = 10
        after_attempt["project"]["updated_at"] = "2026-08-14T08:30:00+08:00"
        after_attempt["claims"][0]["expected_project_revision"] = 10
        event = build_state_event(
            event_id="event-schema-attempt",
            event_type="state-transition",
            project_id=after_attempt["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at=after_attempt["project"]["updated_at"],
            actor_ref="actor-owner",
            causation_ref="work:M3-05",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-experiment",
                    "value": after_attempt["claims"][0],
                },
            ],
            project_after=after_attempt["project"],
            experiment_transition={
                "operation": "attempt-started",
                "request_sha256": "a" * 64,
                "attempt_id": attempt["attempt_id"],
                "promotion_id": None,
                "proposal_id": None,
            },
        )
        event_validator = Draft202012Validator(
            json.loads(
                (root / "schemas/m3-05/state-event-v4alpha1.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
            registry=Registry().with_resource(
                "https://context-control-plane.dev/schema/context.typed-state/v3alpha1",
                Resource.from_contents(schemas["context.typed-state"]),
            ),
        )
        event_validator.validate(event)
        invalid_event = copy.deepcopy(event)
        invalid_event["experiment_transition"]["promotion_id"] = "forged-promotion"
        with self.assertRaises(ValidationError):
            event_validator.validate(invalid_event)

    def test_zero_service_lifecycle_benchmark_is_strict_and_current(self):
        root = Path(__file__).parents[1]
        generated = run_experiment_lifecycle_benchmark(
            root=root,
            samples=2,
            observed_at="2026-08-14T08:35:00+08:00",
        )
        self.assertEqual(generated["measurement"]["successful_runs"], 2)
        self.assertEqual(generated["environment"]["external_services"], 0)
        validate_experiment_lifecycle_benchmark_receipt(generated, root=root)

        receipt = json.loads(
            (
                root / "experiments/routing/m3-05-experiment-lifecycle-results.json"
            ).read_text(encoding="utf-8")
        )
        validate_experiment_lifecycle_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["measurement"]["samples"], 1000)

        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.experiment-lifecycle-benchmark"
        )
        schema_path = root / entry["artifact_path"]
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(receipt)


if __name__ == "__main__":
    unittest.main()
