"""M3-08 input progression and bounded escalation contract."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.input_progression import (
    InputProgressionError,
    decide_input_progression,
)
from context_control_plane.sticky_router import (
    canonical_route_decision_bytes,
    route_task_input,
)


class M308InputProgressionTests(unittest.TestCase):
    observed_at = "2026-08-14T12:00:00+08:00"

    @staticmethod
    def _work(
        work_id: str,
        status: str,
        *,
        kind: str = "work",
        parent_work_id: str | None = "goal-dispatch",
        blocker_ids: list[str] | None = None,
    ):
        return {
            "work_id": work_id,
            "kind": kind,
            "title": work_id,
            "status": status,
            "parent_work_id": parent_work_id,
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": f"dispatch/{work_id}"}
            ],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": blocker_ids or [],
            "revision": 1,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }

    @staticmethod
    def _claim(work_id: str):
        return {
            "claim_id": f"claim-{work_id}",
            "work_id": work_id,
            "actor_ref": "actor-owner",
            "status": "active",
            "expected_project_revision": 48,
            "claimed_at": "2026-08-14T10:00:00+08:00",
            "lease_expires_at": "2026-08-14T14:00:00+08:00",
            "released_at": None,
            "scope_owners": [
                {"scope_kind": "capability", "scope_ref": f"dispatch/{work_id}"}
            ],
        }

    def _state(self, *, active_work_id: str | None = "work-active"):
        works = [
            self._work(
                "campaign-m3",
                "ready",
                kind="campaign",
                parent_work_id=None,
            ),
            self._work(
                "goal-dispatch",
                "ready",
                kind="goal",
                parent_work_id="campaign-m3",
            ),
            self._work(
                "work-active",
                "active" if active_work_id == "work-active" else "ready",
            ),
            self._work("work-next", "ready"),
        ]
        return {
            "schema_version": "context.typed-state/v2alpha1",
            "project": {
                "project_id": "project-dispatch",
                "revision": 48,
                "governance_ref": "governance://master",
                "active_work_ids": [active_work_id] if active_work_id else [],
                "primary_work_id": active_work_id,
                "current_decision_ids": [],
                "active_constraint_ids": [],
                "open_blocker_ids": [],
                "effect_high_watermark": 0,
                "updated_at": "2026-08-14T11:00:00+08:00",
            },
            "works": works,
            "claims": [self._claim(active_work_id)] if active_work_id else [],
            "ideas": [],
            "decisions": [],
            "constraints": [],
            "evidence": [],
            "blockers": [],
            "effects": [],
        }

    @staticmethod
    def _profile():
        work_sources = []
        obligations = []
        for work_id in ("work-active", "work-next"):
            work_sources.append(
                {
                    "work_source_id": f"source-{work_id}",
                    "project_id": "project-dispatch",
                    "work_id": work_id,
                    "source_kind": "master-workstream",
                    "source_ref": f"opaque://master/{work_id}",
                    "source_revision": "revision-48",
                    "governance_parent_id": "campaign-m3",
                    "dependency_ids": [],
                    "readiness": "active" if work_id == "work-active" else "ready",
                }
            )
            obligations.append(
                {
                    "obligation_id": f"obligation-{work_id}",
                    "work_id": work_id,
                    "mode": "required",
                    "condition_ref": None,
                    "authority": {
                        "kind": "project-governance",
                        "ref": "governance://master",
                    },
                    "automation_class": "autonomous",
                    "verification_profile_ref": "verification://default",
                    "evidence_refs": [],
                    "expires_at": None,
                    "status": "pending",
                    "revision": 1,
                }
            )
        return {
            "schema_version": "context.project-governance-profile/v1alpha1",
            "profile": {
                "profile_id": "profile-dispatch",
                "project_id": "project-dispatch",
                "revision": 48,
                "direction_state": "operational",
                "governance_owner_mode": "single-owner",
                "execution_worker_mode": "single-worker",
                "repository_topology": "monolith",
                "requested_runtime_profile": "local-embedded",
                "task_sources": ["master-workstream"],
                "governance_ref": "governance://master",
                "updated_at": "2026-08-14T11:00:00+08:00",
            },
            "charters": [
                {
                    "charter_id": "charter-dispatch",
                    "project_id": "project-dispatch",
                    "profile_id": "profile-dispatch",
                    "status": "approved",
                    "problem_space": "Deterministic task progression.",
                    "intended_users": ["developer"],
                    "confirmed_constraint_refs": [],
                    "prohibited_side_effects": ["untyped-escalation"],
                    "current_evidence_refs": [],
                    "unknowns": [],
                    "assumptions": [],
                    "decision_owner_ref": "actor://owner",
                    "discovery_campaign_id": "campaign-m3",
                    "attempt_budget": 1,
                    "expiry": None,
                    "return_point_work_id": "work-active",
                    "exit_criteria": ["m3-08-verified"],
                    "mainline_authority": True,
                    "revision": 1,
                }
            ],
            "work_sources": work_sources,
            "obligations": obligations,
            "adaptations": [],
        }

    @staticmethod
    def _route_request(input_kind: str):
        return {
            "schema_version": "context.task-route-request/v1alpha1",
            "request_id": f"route-{input_kind}",
            "project_id": "project-dispatch",
            "expected_project_revision": 48,
            "active_work_id": "work-active",
            "expected_active_work_revision": 1,
            "input_ref": f"opaque://thread/{input_kind}",
            "input_sha256": "a" * 64,
            "input_kind": input_kind,
            "classifier_confidence_millionths": 1_000_000,
            "classifier_provenance_ref": "artifact://classifier/m3-08",
            "target_work_id": None,
            "user_authorization_candidate": False,
            "authorization_candidate_ref": None,
            "evidence_refs": [],
        }

    @staticmethod
    def _request(
        intent_kind: str,
        *,
        route_decision: dict | None = None,
        blocking_decision: dict | None = None,
    ):
        return {
            "schema_version": "context.continuation-dispatch-request/v1alpha1",
            "request_id": f"progress-{intent_kind}",
            "project_id": "project-dispatch",
            "expected_project_revision": 48,
            "governance_profile_id": "profile-dispatch",
            "expected_governance_revision": 48,
            "intent_kind": intent_kind,
            "route_decision_sha256": (
                hashlib.sha256(canonical_route_decision_bytes(route_decision)).hexdigest()
                if route_decision is not None
                else None
            ),
            "blocking_decision_sha256": (
                hashlib.sha256(
                    json.dumps(
                        blocking_decision,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if blocking_decision is not None
                else None
            ),
            "observed_at": "2026-08-14T12:00:00+08:00",
        }

    @staticmethod
    def _add_open_blocker(state: dict, work_id: str, blocker_id: str):
        evidence_id = f"evidence-{blocker_id}"
        state["evidence"].append(
            {
                "evidence_id": evidence_id,
                "kind": "user-decision",
                "artifact_ref": f"artifact://m3-08/{blocker_id}",
                "content_sha256": "c" * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T10:30:00+08:00",
                "verified_at": "2026-08-14T10:31:00+08:00",
            }
        )
        blocker = {
            "blocker_id": blocker_id,
            "status": "open",
            "reason": "Acceptance output requires an owner decision.",
            "blocked_work_ids": [work_id],
            "evidence_ids": [evidence_id],
            "opened_at": "2026-08-14T10:32:00+08:00",
            "resolved_at": None,
            "supersedes_blocker_id": None,
        }
        state["blockers"].append(blocker)
        state["project"]["open_blocker_ids"].append(blocker_id)
        next(item for item in state["works"] if item["work_id"] == work_id)[
            "blocker_ids"
        ].append(blocker_id)
        return blocker, evidence_id

    @staticmethod
    def _blocking_decision(blocker: dict, evidence_id: str):
        return {
            "schema_version": "context.blocking-decision/v1alpha1",
            "blocking_decision_id": "blocking-decision-choice",
            "project_id": "project-dispatch",
            "project_revision": 48,
            "blocker_id": blocker["blocker_id"],
            "blocker_kind": "acceptance-output-choice-required",
            "reason": blocker["reason"],
            "affected_work_ids": list(blocker["blocked_work_ids"]),
            "evidence_ids": [evidence_id],
            "affected_scope_refs": [
                {"scope_kind": "capability", "scope_ref": "dispatch/work-active"}
            ],
            "decision_options": [
                {"option_id": "retain", "summary": "Retain the current output."},
                {"option_id": "replace", "summary": "Replace the output."},
            ],
            "default_option_id": "retain",
            "resume_condition": {
                "kind": "decision",
                "refs": ["decision://acceptance-output"],
            },
            "resolution_actor": "user",
            "safe_reversible_default_available": False,
            "state_write_authority": False,
        }

    def test_non_blocking_inputs_preserve_the_active_leaf(self):
        state = self._state()
        profile = self._profile()
        state_before = copy.deepcopy(state)
        profile_before = copy.deepcopy(profile)
        expected_actions = {
            "continue": "continue-active",
            "status_query": "continue-active",
            "discussion_request": "continue-active",
            "context_addition": "continue-active",
            "idea": "capture-and-continue",
        }

        for input_kind, expected_action in expected_actions.items():
            with self.subTest(input_kind=input_kind):
                route_decision = route_task_input(
                    self._route_request(input_kind), state
                )
                decision = decide_input_progression(
                    self._request("routed_input", route_decision=route_decision),
                    state,
                    profile,
                    route_decision=route_decision,
                    blocking_decision=None,
                )
                self.assertEqual(decision["action"], expected_action)
                self.assertEqual(decision["active_work_id_before"], "work-active")
                self.assertEqual(decision["active_work_id_after"], "work-active")
                self.assertIsNone(decision["selected_work_id"])
                self.assertIsNone(decision["blocker_id"])
                self.assertFalse(decision["state_write_authority"])

        self.assertEqual(state, state_before)
        self.assertEqual(profile, profile_before)

    def test_next_ready_selector_prefers_required_and_is_order_independent(self):
        state = self._state(active_work_id=None)
        profile = self._profile()
        for source in profile["work_sources"]:
            source["readiness"] = "ready"
        next(
            item
            for item in profile["obligations"]
            if item["work_id"] == "work-active"
        )["mode"] = "optional"

        first = decide_input_progression(
            self._request("dispatch_tick"),
            state,
            profile,
            route_decision=None,
            blocking_decision=None,
        )
        shuffled_state = copy.deepcopy(state)
        shuffled_state["works"].reverse()
        shuffled_profile = copy.deepcopy(profile)
        shuffled_profile["work_sources"].reverse()
        shuffled_profile["obligations"].reverse()
        second = decide_input_progression(
            self._request("dispatch_tick"),
            shuffled_state,
            shuffled_profile,
            route_decision=None,
            blocking_decision=None,
        )

        for decision in (first, second):
            self.assertEqual(decision["action"], "select-next-ready")
            self.assertEqual(decision["selected_work_id"], "work-next")
            self.assertEqual(decision["selected_work_revision"], 1)
            self.assertIsNone(decision["active_work_id_before"])
            self.assertIsNone(decision["active_work_id_after"])
            self.assertFalse(decision["state_write_authority"])

    def test_current_typed_blocker_can_emit_a_bounded_user_question(self):
        state = self._state()
        profile = self._profile()
        blocker, evidence_id = self._add_open_blocker(
            state, "work-active", "blocker-choice"
        )
        blocking_decision = self._blocking_decision(blocker, evidence_id)

        decision = decide_input_progression(
            self._request(
                "blocking_decision", blocking_decision=blocking_decision
            ),
            state,
            profile,
            route_decision=None,
            blocking_decision=blocking_decision,
        )

        self.assertEqual(decision["action"], "ask-user")
        self.assertEqual(decision["active_work_id_before"], "work-active")
        self.assertEqual(decision["active_work_id_after"], "work-active")
        self.assertEqual(decision["blocker_id"], "blocker-choice")
        self.assertEqual(
            decision["blocker_kind"], "acceptance-output-choice-required"
        )
        self.assertEqual(decision["reason"], blocker["reason"])
        self.assertEqual(decision["evidence_ids"], [evidence_id])
        self.assertEqual(len(decision["decision_options"]), 2)
        self.assertEqual(decision["default_behavior"], "retain")
        self.assertEqual(
            decision["resume_condition"],
            {"kind": "decision", "refs": ["decision://acceptance-output"]},
        )
        self.assertFalse(decision["state_write_authority"])

    def test_ready_required_work_prevents_premature_blocked_stop(self):
        state = self._state(active_work_id=None)
        profile = self._profile()
        for source in profile["work_sources"]:
            source["readiness"] = "ready"
        blocked_work = next(
            item for item in state["works"] if item["work_id"] == "work-next"
        )
        blocked_work["status"] = "blocked"
        next(
            item
            for item in profile["work_sources"]
            if item["work_id"] == "work-next"
        )["readiness"] = "blocked"
        blocker, evidence_id = self._add_open_blocker(
            state, "work-next", "blocker-external"
        )
        blocking_decision = self._blocking_decision(blocker, evidence_id)
        blocking_decision.update(
            {
                "blocking_decision_id": "blocking-decision-external",
                "blocker_kind": "external-completion-evidence-unavailable",
                "affected_scope_refs": [
                    {
                        "scope_kind": "capability",
                        "scope_ref": "dispatch/work-next",
                    }
                ],
                "decision_options": [],
                "default_option_id": None,
                "resume_condition": {
                    "kind": "evidence",
                    "refs": ["evidence://external-completion"],
                },
                "resolution_actor": "external_system",
            }
        )

        decision = decide_input_progression(
            self._request(
                "blocking_decision", blocking_decision=blocking_decision
            ),
            state,
            profile,
            route_decision=None,
            blocking_decision=blocking_decision,
        )

        self.assertEqual(decision["action"], "select-next-ready")
        self.assertEqual(decision["selected_work_id"], "work-active")
        self.assertIsNone(decision["blocker_id"])

    def test_required_closure_allows_stop_complete_while_optional_remains(self):
        state = self._state(active_work_id=None)
        profile = self._profile()
        completed_work = next(
            item for item in state["works"] if item["work_id"] == "work-next"
        )
        completed_work["status"] = "completed"
        completed_work["evidence_ids"] = ["evidence-work-next-completed"]
        state["evidence"].append(
            {
                "evidence_id": "evidence-work-next-completed",
                "kind": "test",
                "artifact_ref": "artifact://m3-08/work-next-completed",
                "content_sha256": "d" * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T10:40:00+08:00",
                "verified_at": "2026-08-14T10:41:00+08:00",
            }
        )
        next(
            item
            for item in profile["work_sources"]
            if item["work_id"] == "work-active"
        )["readiness"] = "ready"
        next(
            item
            for item in profile["work_sources"]
            if item["work_id"] == "work-next"
        )["readiness"] = "completed"
        optional = next(
            item
            for item in profile["obligations"]
            if item["work_id"] == "work-active"
        )
        optional["mode"] = "optional"
        required = next(
            item
            for item in profile["obligations"]
            if item["work_id"] == "work-next"
        )
        required["status"] = "satisfied"
        required["evidence_refs"] = ["evidence://work-next-completed"]

        decision = decide_input_progression(
            self._request("dispatch_tick"),
            state,
            profile,
            route_decision=None,
            blocking_decision=None,
        )

        self.assertEqual(decision["action"], "stop-complete")
        self.assertEqual(decision["reason_code"], "required-obligations-closed")
        self.assertIsNone(decision["selected_work_id"])
        self.assertFalse(decision["state_write_authority"])

    def test_external_blocker_stop_is_complete_and_auditable(self):
        state = self._state(active_work_id=None)
        profile = self._profile()
        next(
            item
            for item in profile["obligations"]
            if item["work_id"] == "work-active"
        )["mode"] = "optional"
        next(
            item
            for item in profile["work_sources"]
            if item["work_id"] == "work-active"
        )["readiness"] = "ready"
        blocked_work = next(
            item for item in state["works"] if item["work_id"] == "work-next"
        )
        blocked_work["status"] = "blocked"
        next(
            item
            for item in profile["work_sources"]
            if item["work_id"] == "work-next"
        )["readiness"] = "blocked"
        blocker, evidence_id = self._add_open_blocker(
            state, "work-next", "blocker-external-only"
        )
        blocking_decision = self._blocking_decision(blocker, evidence_id)
        blocking_decision.update(
            {
                "blocking_decision_id": "blocking-decision-external-only",
                "blocker_kind": "external-completion-evidence-unavailable",
                "affected_scope_refs": [
                    {
                        "scope_kind": "capability",
                        "scope_ref": "dispatch/work-next",
                    }
                ],
                "decision_options": [],
                "default_option_id": None,
                "resume_condition": {
                    "kind": "evidence",
                    "refs": ["evidence://external-completion"],
                },
                "resolution_actor": "external_system",
            }
        )

        decision = decide_input_progression(
            self._request(
                "blocking_decision", blocking_decision=blocking_decision
            ),
            state,
            profile,
            route_decision=None,
            blocking_decision=blocking_decision,
        )

        self.assertEqual(decision["action"], "stop-blocked")
        self.assertTrue(decision["reason"])
        self.assertEqual(decision["evidence_ids"], [evidence_id])
        self.assertEqual(
            decision["resume_condition"]["kind"], "evidence"
        )

    def test_safe_reversible_default_keeps_the_active_leaf_without_escalation(self):
        state = self._state()
        profile = self._profile()
        blocker, evidence_id = self._add_open_blocker(
            state, "work-active", "blocker-safe-default"
        )
        blocking_decision = self._blocking_decision(blocker, evidence_id)
        blocking_decision["safe_reversible_default_available"] = True

        decision = decide_input_progression(
            self._request(
                "blocking_decision", blocking_decision=blocking_decision
            ),
            state,
            profile,
            route_decision=None,
            blocking_decision=blocking_decision,
        )

        self.assertEqual(decision["action"], "continue-active")
        self.assertEqual(decision["reason_code"], "safe-reversible-default")
        self.assertEqual(decision["active_work_id_after"], "work-active")
        self.assertIsNone(decision["blocker_id"])

    def test_pending_required_without_ready_work_or_typed_blocker_fails_closed(self):
        state = self._state(active_work_id=None)
        profile = self._profile()
        for work_id in ("work-active", "work-next"):
            next(item for item in state["works"] if item["work_id"] == work_id)[
                "status"
            ] = "blocked"
            next(
                item
                for item in profile["work_sources"]
                if item["work_id"] == work_id
            )["readiness"] = "blocked"

        with self.assertRaisesRegex(
            InputProgressionError, "no ready required work"
        ):
            decide_input_progression(
                self._request("dispatch_tick"),
                state,
                profile,
                route_decision=None,
                blocking_decision=None,
            )

    def test_route_decision_hash_and_current_revision_are_mandatory(self):
        state = self._state()
        profile = self._profile()
        route_decision = route_task_input(self._route_request("continue"), state)
        request = self._request("routed_input", route_decision=route_decision)
        request["route_decision_sha256"] = "f" * 64
        with self.assertRaisesRegex(InputProgressionError, "hash"):
            decide_input_progression(
                request,
                state,
                profile,
                route_decision=route_decision,
                blocking_decision=None,
            )

        request = self._request("routed_input", route_decision=route_decision)
        request["expected_project_revision"] = 49
        with self.assertRaisesRegex(InputProgressionError, "revision"):
            decide_input_progression(
                request,
                state,
                profile,
                route_decision=route_decision,
                blocking_decision=None,
            )

    def test_canonical_decision_rejects_forged_or_incomplete_outputs(self):
        from context_control_plane.input_progression import (
            canonical_progression_decision_bytes,
        )

        state = self._state()
        profile = self._profile()
        route_decision = route_task_input(self._route_request("continue"), state)
        decision = decide_input_progression(
            self._request("routed_input", route_decision=route_decision),
            state,
            profile,
            route_decision=route_decision,
            blocking_decision=None,
        )
        canonical = canonical_progression_decision_bytes(decision)
        self.assertEqual(canonical, canonical_progression_decision_bytes(decision))

        forged = copy.deepcopy(decision)
        forged["state_write_authority"] = True
        with self.assertRaisesRegex(InputProgressionError, "write authority"):
            canonical_progression_decision_bytes(forged)

        incomplete = copy.deepcopy(decision)
        incomplete["action"] = "ask-user"
        with self.assertRaisesRegex(InputProgressionError, "ask-user"):
            canonical_progression_decision_bytes(incomplete)

    def test_strict_wire_schemas_are_registered_and_validate_real_objects(self):
        root = Path(__file__).parents[1]
        paths = {
            "context.continuation-dispatch-request": root
            / "schemas/m3-08/continuation-dispatch-request.schema.json",
            "context.blocking-decision": root
            / "schemas/m3-08/blocking-decision.schema.json",
            "context.continuation-dispatch-decision": root
            / "schemas/m3-08/continuation-dispatch-decision.schema.json",
        }
        schemas = {
            schema_id: json.loads(path.read_text(encoding="utf-8"))
            for schema_id, path in paths.items()
        }
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)

        state = self._state()
        profile = self._profile()
        blocker, evidence_id = self._add_open_blocker(
            state, "work-active", "blocker-schema"
        )
        blocking_decision = self._blocking_decision(blocker, evidence_id)
        request = self._request(
            "blocking_decision", blocking_decision=blocking_decision
        )
        decision = decide_input_progression(
            request,
            state,
            profile,
            route_decision=None,
            blocking_decision=blocking_decision,
        )
        Draft202012Validator(
            schemas["context.continuation-dispatch-request"]
        ).validate(request)
        Draft202012Validator(schemas["context.blocking-decision"]).validate(
            blocking_decision
        )
        Draft202012Validator(
            schemas["context.continuation-dispatch-decision"]
        ).validate(decision)

        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        by_id = {entry["schema_id"]: entry for entry in registry["schemas"]}
        for schema_id, path in paths.items():
            entry = by_id[schema_id]
            self.assertEqual(entry["artifact_path"], str(path.relative_to(root)))
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
