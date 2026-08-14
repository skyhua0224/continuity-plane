import copy
import inspect
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.checkpoint import publish_checkpoint
from context_control_plane.route_apply import RouteApplyError, apply_route
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_events import EVENT_SCHEMA_VERSION_V3
from context_control_plane.state_store import capability_manifest_to_document
from context_control_plane.sticky_router import canonical_route_decision_bytes, route_task_input


class _AllowAuthorizer:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def authorize(self, context, action, project_id):
        return self.allowed


class M303RouteApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = {
            "schema_version": "context.typed-state/v2alpha1",
            "project": {
                "project_id": "project-route-apply",
                "revision": 1,
                "governance_ref": "artifact://governance/m303",
                "active_work_ids": ["work-active"],
                "primary_work_id": "work-active",
                "current_decision_ids": [],
                "active_constraint_ids": [],
                "open_blocker_ids": [],
                "effect_high_watermark": 0,
                "updated_at": "2026-08-13T16:00:00+08:00",
            },
            "works": [
                cls._work("campaign", "campaign", None, "ready"),
                cls._work("goal", "goal", "campaign", "ready"),
                cls._work("work-active", "work", "goal", "active"),
                cls._work("work-target", "work", "goal", "ready"),
            ],
            "claims": [cls._claim("claim-active", "work-active")],
            "ideas": [], "decisions": [], "constraints": [], "evidence": [], "blockers": [], "effects": [],
        }

    @staticmethod
    def _work(work_id, kind, parent, status):
        return {
            "work_id": work_id, "kind": kind, "title": work_id, "status": status,
            "parent_work_id": parent, "dependency_ids": [], "owner_refs": ["actor-owner"],
            "scope_refs": [{"scope_kind": "capability", "scope_ref": f"route/{work_id}"}],
            "overlap_candidate_ids": [], "dedupe_status": "clear", "supersedes_work_id": None,
            "evidence_ids": [], "blocker_ids": [], "revision": 1,
            "return_point_work_id": None, "exit_criteria": [], "attempt_budget": None,
            "expires_at": None, "promotion_target_work_id": None, "mainline_authority": True,
        }

    @staticmethod
    def _claim(claim_id, work_id):
        return {
            "claim_id": claim_id, "work_id": work_id, "actor_ref": "actor-owner", "status": "active",
            "expected_project_revision": 1, "claimed_at": "2026-08-13T15:00:00+08:00",
            "lease_expires_at": "2026-08-13T18:00:00+08:00", "released_at": None,
            "scope_owners": [{"scope_kind": "capability", "scope_ref": f"route/{work_id}"}],
        }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStateStore(self.path)
        self.store.initialize()
        self.artifacts = LocalArtifactStore(Path(self.temp.name) / "artifacts")
        self.artifacts.initialize()
        self.canonical_plan_sha256 = "b" * 64
        self.registry_digest = "c" * 64
        self.state = copy.deepcopy(self.base)
        self.state["project"]["project_id"] = "project-route-apply"
        self.store.create_project(self.state)

    def _request(self, input_kind, *, request_id="route-apply-1", **updates):
        active = self.state["project"]["primary_work_id"]
        work = next(item for item in self.state["works"] if item["work_id"] == active)
        request = {
            "schema_version": "context.task-route-request/v1alpha1",
            "request_id": request_id,
            "project_id": self.state["project"]["project_id"],
            "expected_project_revision": self.state["project"]["revision"],
            "active_work_id": active,
            "expected_active_work_revision": work["revision"],
            "input_ref": "opaque://route/input-1",
            "input_sha256": "1" * 64,
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

    def _decision(self, request):
        return route_task_input(request, self.state)

    @staticmethod
    def _apply_request(decision, *, request_id=None, **updates):
        result = {
            "schema_version": "context.task-route-apply-request/v1alpha1",
            "request_id": request_id or decision["request_id"],
            "project_id": decision["project_id"],
            "proposal_sha256": __import__("hashlib").sha256(canonical_route_decision_bytes(decision)).hexdigest(),
            "operation": {
                "continue": "continue",
                "capture-candidate-and-continue": "continue",
                "propose-child": "child",
                "propose-switch": "switch",
                "propose-correction": "correction",
            }[decision["route"]],
            "expected_project_revision": decision["project_revision"],
            "expected_active_work_id": decision["active_work_id_before"],
            "expected_active_work_revision": decision["active_work_revision_before"],
            "target_work_id": decision["target_work_id"],
            "target_work_revision": decision["target_work_revision"],
            "authorization_ref": None,
            "checkpoint_ref": None,
            "checkpoint_binding": None,
            "child_work": None,
            "correction_changes": None,
            "supersedes_event_id": None,
            "causation_ref": "route-apply:test",
            "correlation_ref": "campaign:m303",
        }
        result.update(updates)
        return result

    def _publish_checkpoint(self, store=None):
        store = store or self.store
        snapshot = store.read_project(self.state["project"]["project_id"])
        events = store.read_events(self.state["project"]["project_id"])
        event_head = (
            {
                "sequence_no": events[-1]["sequence_no"],
                "event_sha256": events[-1]["event_sha256"],
            }
            if events
            else None
        )
        checkpoint_ref = publish_checkpoint(
            {
                "snapshot": snapshot,
                "revision": snapshot["project"]["revision"],
                "event_head": event_head,
                "registry_digest": self.registry_digest,
                "capabilities": capability_manifest_to_document(store.capability_manifest),
            },
            self.artifacts,
            canonical_plan_sha256=self.canonical_plan_sha256,
        )
        active = snapshot["project"]["primary_work_id"]
        binding = {
            "checkpoint_revision": snapshot["project"]["revision"],
            "checkpoint_event_head": event_head,
            "return_work_id": active,
            "return_work_revision": next(
                item["revision"]
                for item in snapshot["works"]
                if item["work_id"] == active
            ),
        }
        return checkpoint_ref.to_document(), binding

    def _apply_route(self, store, request, **kwargs):
        return apply_route(
            store,
            request,
            artifact_store=self.artifacts,
            expected_plan_sha256=self.canonical_plan_sha256,
            expected_registry_digest=self.registry_digest,
            **kwargs,
        )

    def test_apply_route_declares_checkpoint_verification_dependencies(self):
        parameters = inspect.signature(apply_route).parameters
        self.assertIn("artifact_store", parameters)
        self.assertIn("expected_plan_sha256", parameters)
        self.assertIn("expected_registry_digest", parameters)

    def test_continue_has_no_state_write(self):
        request = self._request("continue")
        decision = self._decision(request)
        result = self._apply_route(
            self.store,
            self._apply_request(decision),
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.assertEqual(result["status"], "continued")
        self.assertEqual(self.store.read_project(self.state["project"]["project_id"]), self.state)
        self.assertEqual(self.store.read_events(self.state["project"]["project_id"]), [])

    def test_child_remains_proposed_and_active_leaf_is_unchanged(self):
        request = self._request("child_work")
        decision = self._decision(request)
        child = {
            "work_id": "work-child",
            "kind": "work",
            "title": "Child proposal",
            "status": "proposed",
            "parent_work_id": decision["active_work_id_before"],
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [{"scope_kind": "capability", "scope_ref": "route/child"}],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": 0,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }
        result = self._apply_route(
            self.store,
            self._apply_request(decision, child_work=child),
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.assertEqual(result["snapshot"]["project"]["primary_work_id"], self.state["project"]["primary_work_id"])
        self.assertEqual(next(item for item in result["snapshot"]["works"] if item["work_id"] == "work-child")["status"], "proposed")
        self.assertEqual(result["event"]["schema_version"], EVENT_SCHEMA_VERSION_V3)

    def test_switch_is_one_atomic_event_with_return_frame(self):
        active = self.state["project"]["primary_work_id"]
        target = next(item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work")
        request = self._request(
            "switch",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        artifact, binding = self._publish_checkpoint()
        result = self._apply_route(
            self.store,
            self._apply_request(
                decision,
                authorization_ref="auth:trusted",
                checkpoint_ref=artifact,
                checkpoint_binding=binding,
            ),
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.assertEqual(result["snapshot"]["project"]["primary_work_id"], target["work_id"])
        self.assertEqual(len(self.store.read_events(self.state["project"]["project_id"])), 1)
        self.assertEqual([item["event_kind"] for item in result["event"]["task_transition"]["task_events"]], ["task_suspended", "task_activated"])
        self.assertEqual(result["return_frame"]["return_work_id"], active)

    def test_switch_rejects_checkpoint_bound_to_wrong_event_head(self):
        child_request = self._request("child_work", request_id="route-seed-child")
        child_decision = self._decision(child_request)
        child = self._work("work-child", "work", "work-active", "proposed")
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])
        active = self.state["project"]["primary_work_id"]
        target = next(
            item
            for item in self.state["works"]
            if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "switch",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        artifact, binding = self._publish_checkpoint()
        binding["checkpoint_event_head"] = {
            "sequence_no": 1,
            "event_sha256": "f" * 64,
        }
        arguments = self._apply_request(
            decision,
            authorization_ref="auth:trusted",
            checkpoint_ref=artifact,
            checkpoint_binding=binding,
        )

        with self.assertRaisesRegex(RouteApplyError, "event head"):
            self._apply_route(
                self.store,
                arguments,
                decision=decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(len(self.store.read_events(self.state["project"]["project_id"])), 1)

    def test_switch_rejects_a_checkpoint_ref_without_a_published_artifact(self):
        active = self.state["project"]["primary_work_id"]
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "switch",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        missing = {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": "a" * 64,
            "size_bytes": 1,
            "artifact_uri": "artifact://sha256/" + "a" * 64,
        }

        with self.assertRaisesRegex(RouteApplyError, "checkpoint artifact"):
            self._apply_route(
                self.store,
                self._apply_request(
                    decision,
                    authorization_ref="auth:trusted",
                    checkpoint_ref=missing,
                    checkpoint_binding={
                        "checkpoint_revision": self.state["project"]["revision"],
                        "checkpoint_event_head": None,
                        "return_work_id": active,
                        "return_work_revision": next(
                            item["revision"]
                            for item in self.state["works"]
                            if item["work_id"] == active
                        ),
                    },
                ),
                decision=decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(self.store.read_events(self.state["project"]["project_id"]), [])

    def test_switch_rechecks_target_blockers_before_activation(self):
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "switch",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        blocked = copy.deepcopy(self.state)
        blocked["blockers"].append(
            {
                "blocker_id": "blocker-target",
                "status": "open",
                "reason": "target is blocked",
                "blocked_work_ids": [target["work_id"]],
                "evidence_ids": [],
                "opened_at": "2026-08-13T16:30:00+08:00",
                "resolved_at": None,
                "supersedes_blocker_id": None,
            }
        )
        blocked["project"]["open_blocker_ids"] = ["blocker-target"]
        next(item for item in blocked["works"] if item["work_id"] == target["work_id"])[
            "blocker_ids"
        ] = ["blocker-target"]
        blocked_store = SQLiteStateStore(Path(self.temp.name) / "blocked.sqlite3")
        blocked_store.initialize()
        blocked_store.create_project(blocked)
        artifact, binding = self._publish_checkpoint(blocked_store)

        with self.assertRaisesRegex(RouteApplyError, "target-blocked"):
            self._apply_route(
                blocked_store,
                self._apply_request(
                    decision,
                    authorization_ref="auth:trusted",
                    checkpoint_ref=artifact,
                    checkpoint_binding=binding,
                ),
                decision=decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(blocked_store.read_events(blocked["project"]["project_id"]), [])

    def test_interrupt_decision_cannot_be_persisted_as_switch(self):
        active = self.state["project"]["primary_work_id"]
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "interrupt",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        artifact = {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": "a" * 64,
            "size_bytes": 1,
            "artifact_uri": "artifact://sha256/" + "a" * 64,
        }

        with self.assertRaisesRegex(RouteApplyError, "input kind"):
            self._apply_route(
                self.store,
                self._apply_request(
                    decision,
                    operation="switch",
                    authorization_ref="auth:trusted",
                    checkpoint_ref=artifact,
                    checkpoint_binding={
                        "checkpoint_revision": self.state["project"]["revision"],
                        "checkpoint_event_head": None,
                        "return_work_id": active,
                        "return_work_revision": next(
                            item["revision"]
                            for item in self.state["works"]
                            if item["work_id"] == active
                        ),
                    },
                ),
                decision=decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(self.store.read_events(self.state["project"]["project_id"]), [])

    def test_switch_preserves_the_released_claim_when_request_id_matches_claim_id(self):
        active = self.state["project"]["primary_work_id"]
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "switch",
            request_id="active",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        artifact, binding = self._publish_checkpoint()
        result = self._apply_route(
            self.store,
            self._apply_request(
                decision,
                authorization_ref="auth:trusted",
                checkpoint_ref=artifact,
                checkpoint_binding=binding,
            ),
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

        released = next(
            claim
            for claim in result["snapshot"]["claims"]
            if claim["claim_id"] == "claim-active"
        )
        self.assertEqual(released["work_id"], active)
        self.assertEqual(released["status"], "released")
        active_claim = next(
            claim for claim in result["snapshot"]["claims"] if claim["status"] == "active"
        )
        self.assertNotEqual(active_claim["claim_id"], released["claim_id"])
        self.assertEqual(active_claim["work_id"], target["work_id"])

    def test_stale_revision_and_untrusted_authorization_are_rejected_without_write(self):
        target = next(item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work")
        request = self._request("switch", target_work_id=target["work_id"], user_authorization_candidate=True, authorization_candidate_ref="opaque://candidate/auth")
        decision = self._decision(request)
        bad = self._apply_request(decision, authorization_ref="opaque://candidate/auth")
        with self.assertRaisesRegex(RouteApplyError, "authorization"):
            self._apply_route(self.store, bad, decision=decision, context={"subject_ref": "actor-owner", "authorization_ref": "opaque://candidate/auth"}, authorizer=_AllowAuthorizer(), clock=lambda: "2026-08-13T17:00:00+08:00", event_id_factory=lambda request_id: f"event-{request_id}")

        target = next(item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work")
        request = self._request("switch", target_work_id=target["work_id"], user_authorization_candidate=True, authorization_candidate_ref="opaque://candidate/auth")
        decision = self._decision(request)
        stale = self._apply_request(decision, expected_project_revision=999, authorization_ref="auth:trusted")
        with self.assertRaisesRegex(RouteApplyError, "stale"):
            self._apply_route(self.store, stale, decision=decision, context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"}, authorizer=_AllowAuthorizer(), clock=lambda: "2026-08-13T17:00:00+08:00", event_id_factory=lambda request_id: f"event-{request_id}")
        self.assertEqual(self.store.read_events(self.state["project"]["project_id"]), [])

    def test_correction_rejects_a_second_branch_from_the_same_event(self):
        child_request = self._request("child_work", request_id="route-seed-child")
        child_decision = self._decision(child_request)
        child = self._work("work-child", "work", "work-active", "proposed")
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])

        first_request = self._request("correction", request_id="route-correction-1")
        first_decision = self._decision(first_request)
        first_value = copy.deepcopy(
            next(item for item in self.state["works"] if item["work_id"] == "work-child")
        )
        first_value["title"] = "Corrected child"
        first_value["revision"] += 1
        self._apply_route(
            self.store,
            self._apply_request(
                first_decision,
                authorization_ref="auth:trusted",
                correction_changes=[
                    {"collection": "works", "object_id": "work-child", "value": first_value}
                ],
                supersedes_event_id="event-route-seed-child",
            ),
            decision=first_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:45:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])
        second_request = self._request("correction", request_id="route-correction-2")
        second_decision = self._decision(second_request)
        second_value = copy.deepcopy(
            next(item for item in self.state["works"] if item["work_id"] == "work-child")
        )
        second_value["title"] = "Forked child"
        second_value["revision"] += 1

        with self.assertRaisesRegex(RouteApplyError, "already superseded"):
            self._apply_route(
                self.store,
                self._apply_request(
                    second_decision,
                    authorization_ref="auth:trusted",
                    correction_changes=[
                        {
                            "collection": "works",
                            "object_id": "work-child",
                            "value": second_value,
                        }
                    ],
                    supersedes_event_id="event-route-seed-child",
                ),
                decision=second_decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(len(self.store.read_events(self.state["project"]["project_id"])), 2)

    def test_correction_must_change_an_object_from_the_superseded_event(self):
        child_request = self._request("child_work", request_id="route-seed-child")
        child_decision = self._decision(child_request)
        child = self._work("work-child", "work", "work-active", "proposed")
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])
        correction_request = self._request("correction", request_id="route-correction-unrelated")
        correction_decision = self._decision(correction_request)
        unrelated = copy.deepcopy(
            next(item for item in self.state["works"] if item["work_id"] == "work-active")
        )
        unrelated["title"] = "Unrelated active Work change"
        unrelated["revision"] += 1

        with self.assertRaisesRegex(RouteApplyError, "changed keys"):
            self._apply_route(
                self.store,
                self._apply_request(
                    correction_decision,
                    authorization_ref="auth:trusted",
                    correction_changes=[
                        {
                            "collection": "works",
                            "object_id": "work-active",
                            "value": unrelated,
                        }
                    ],
                    supersedes_event_id="event-route-seed-child",
                ),
                decision=correction_decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T17:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(len(self.store.read_events(self.state["project"]["project_id"])), 1)

    def test_same_correction_identity_rejects_a_shorter_change_payload(self):
        child_request = self._request("child_work", request_id="route-seed-child")
        child_decision = self._decision(child_request)
        child = self._work("work-child", "work", "work-active", "proposed")
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])

        correction_request = self._request(
            "correction", request_id="route-correction-retry"
        )
        correction_decision = self._decision(correction_request)
        corrected_child = copy.deepcopy(
            next(item for item in self.state["works"] if item["work_id"] == "work-child")
        )
        corrected_child["title"] = "Corrected child"
        corrected_child["revision"] += 1
        corrected_claim = copy.deepcopy(self.state["claims"][0])
        corrected_claim["expected_project_revision"] = 3
        correction_changes = [
            {
                "collection": "works",
                "object_id": "work-child",
                "value": corrected_child,
            },
            {
                "collection": "claims",
                "object_id": corrected_claim["claim_id"],
                "value": corrected_claim,
            },
        ]
        arguments = self._apply_request(
            correction_decision,
            authorization_ref="auth:trusted",
            correction_changes=correction_changes,
            supersedes_event_id="event-route-seed-child",
        )
        kwargs = dict(
            decision=correction_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:45:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self._apply_route(self.store, arguments, **kwargs)

        shorter_retry = copy.deepcopy(arguments)
        shorter_retry["correction_changes"] = [correction_changes[0]]
        with self.assertRaisesRegex(RouteApplyError, "identity conflicts"):
            self._apply_route(self.store, shorter_retry, **kwargs)

    def test_correction_rejects_duplicate_object_changes_before_commit(self):
        child_request = self._request("child_work", request_id="route-seed-child")
        child_decision = self._decision(child_request)
        child = self._work("work-child", "work", "work-active", "proposed")
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T16:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self.state = self.store.read_project(self.state["project"]["project_id"])
        correction_request = self._request("correction", request_id="route-correction-duplicate")
        correction_decision = self._decision(correction_request)
        first_value = copy.deepcopy(
            next(item for item in self.state["works"] if item["work_id"] == "work-child")
        )
        first_value["title"] = "First replacement"
        first_value["revision"] += 1
        second_value = copy.deepcopy(first_value)
        second_value["title"] = "Conflicting replacement"

        with self.assertRaisesRegex(RouteApplyError, "duplicate"):
            self._apply_route(
                self.store,
                self._apply_request(
                    correction_decision,
                    authorization_ref="auth:trusted",
                    correction_changes=[
                        {
                            "collection": "works",
                            "object_id": "work-child",
                            "value": first_value,
                        },
                        {
                            "collection": "works",
                            "object_id": "work-child",
                            "value": second_value,
                        },
                    ],
                    supersedes_event_id="event-route-seed-child",
                ),
                decision=correction_decision,
                context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-13T16:45:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

        self.assertEqual(len(self.store.read_events(self.state["project"]["project_id"])), 1)

    def test_same_request_replays_exact_receipt(self):
        request = self._request("continue")
        decision = self._decision(request)
        arguments = self._apply_request(decision)
        kwargs = dict(decision=decision, context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"}, authorizer=_AllowAuthorizer(), clock=lambda: "2026-08-13T17:00:00+08:00", event_id_factory=lambda request_id: f"event-{request_id}")
        first = self._apply_route(self.store, arguments, **kwargs)
        second = self._apply_route(self.store, arguments, **kwargs)
        self.assertEqual(first, second)

    def test_same_child_request_replays_exact_receipt_at_current_head(self):
        request = self._request("child_work", request_id="route-child-retry")
        decision = self._decision(request)
        child = self._work("work-child", "work", "work-active", "proposed")
        arguments = self._apply_request(decision, child_work=child)
        kwargs = dict(
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

        first = self._apply_route(self.store, arguments, **kwargs)
        second = self._apply_route(self.store, arguments, **kwargs)

        self.assertEqual(first, second)

    def test_same_switch_request_replays_exact_return_frame(self):
        active = self.state["project"]["primary_work_id"]
        target = next(item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work")
        request = self._request("switch", target_work_id=target["work_id"], user_authorization_candidate=True, authorization_candidate_ref="opaque://candidate/auth")
        decision = self._decision(request)
        artifact, binding = self._publish_checkpoint()
        arguments = self._apply_request(decision, authorization_ref="auth:trusted", checkpoint_ref=artifact, checkpoint_binding=binding)
        kwargs = dict(decision=decision, context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"}, authorizer=_AllowAuthorizer(), clock=lambda: "2026-08-13T17:00:00+08:00", event_id_factory=lambda request_id: f"event-{request_id}")
        first = self._apply_route(self.store, arguments, **kwargs)
        second = self._apply_route(self.store, arguments, **kwargs)
        self.assertEqual(first, second)

    def test_old_switch_retry_is_rejected_after_the_event_head_advances(self):
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        switch_request = self._request(
            "switch",
            request_id="route-switch-old",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        switch_decision = self._decision(switch_request)
        artifact, binding = self._publish_checkpoint()
        switch_arguments = self._apply_request(
            switch_decision,
            authorization_ref="auth:trusted",
            checkpoint_ref=artifact,
            checkpoint_binding=binding,
        )
        switch_kwargs = dict(
            decision=switch_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self._apply_route(self.store, switch_arguments, **switch_kwargs)
        self.state = self.store.read_project(self.state["project"]["project_id"])
        child_request = self._request("child_work", request_id="route-child-after-switch")
        child_decision = self._decision(child_request)
        child = self._work(
            "work-child-after-switch",
            "work",
            self.state["project"]["primary_work_id"],
            "proposed",
        )
        self._apply_route(
            self.store,
            self._apply_request(child_decision, child_work=child),
            decision=child_decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:test"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:05:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

        with self.assertRaisesRegex(RouteApplyError, "receipt is no longer current"):
            self._apply_route(self.store, switch_arguments, **switch_kwargs)

    def test_same_switch_identity_rejects_a_different_checkpoint_payload(self):
        target = next(
            item for item in self.state["works"] if item["status"] == "ready" and item["kind"] == "work"
        )
        request = self._request(
            "switch",
            request_id="route-switch-payload",
            target_work_id=target["work_id"],
            user_authorization_candidate=True,
            authorization_candidate_ref="opaque://candidate/auth",
        )
        decision = self._decision(request)
        artifact, binding = self._publish_checkpoint()
        arguments = self._apply_request(
            decision,
            authorization_ref="auth:trusted",
            checkpoint_ref=artifact,
            checkpoint_binding=binding,
        )
        kwargs = dict(
            decision=decision,
            context={"subject_ref": "actor-owner", "authorization_ref": "auth:trusted"},
            authorizer=_AllowAuthorizer(),
            clock=lambda: "2026-08-13T17:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        self._apply_route(self.store, arguments, **kwargs)
        changed = copy.deepcopy(arguments)
        changed["checkpoint_ref"] = {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": "f" * 64,
            "size_bytes": 1,
            "artifact_uri": "artifact://sha256/" + "f" * 64,
        }

        with self.assertRaisesRegex(RouteApplyError, "identity conflicts"):
            self._apply_route(self.store, changed, **kwargs)


if __name__ == "__main__":
    unittest.main()
