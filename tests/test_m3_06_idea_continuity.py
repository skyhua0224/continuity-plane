"""M3-06 candidate Idea capture and continuation authority."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

import context_control_plane.idea_continuity_benchmark as idea_benchmark
from context_control_plane.idea_continuity_benchmark import (
    run_idea_continuity_benchmark,
    validate_idea_continuity_benchmark_receipt,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
)
from context_control_plane.state_mcp import (
    RequestContext,
    StateMCPService,
    state_mcp_tool_definitions,
)


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M306IdeaContinuityTests(unittest.TestCase):
    @staticmethod
    def snapshot():
        return {
            "schema_version": "context.typed-state/v3alpha1",
            "project": {
                "project_id": "project-idea-continuity",
                "revision": 9,
                "governance_ref": "artifact://governance/m306",
                "active_work_ids": ["work-active"],
                "primary_work_id": "work-active",
                "current_decision_ids": [],
                "active_constraint_ids": [],
                "open_blocker_ids": [],
                "effect_high_watermark": 0,
                "updated_at": "2026-08-14T08:00:00+08:00",
            },
            "works": [
                M306IdeaContinuityTests._work("campaign", "campaign", None, "ready"),
                M306IdeaContinuityTests._work("goal", "goal", "campaign", "ready"),
                M306IdeaContinuityTests._work("work-active", "work", "goal", "active"),
                M306IdeaContinuityTests._work("work-target", "work", "goal", "ready"),
            ],
            "claims": [
                {
                    "claim_id": "claim-active",
                    "work_id": "work-active",
                    "actor_ref": "actor-owner",
                    "status": "active",
                    "expected_project_revision": 9,
                    "claimed_at": "2026-08-14T07:30:00+08:00",
                    "lease_expires_at": "2026-08-14T10:00:00+08:00",
                    "released_at": None,
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "idea/active"}
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

    @staticmethod
    def _work(work_id, kind, parent_work_id, status):
        return {
            "work_id": work_id,
            "kind": kind,
            "title": work_id,
            "status": status,
            "parent_work_id": parent_work_id,
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [
                {
                    "scope_kind": "capability",
                    "scope_ref": "idea/active" if work_id == "work-active" else work_id,
                }
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

    def setUp(self):
        self.snapshot = self.snapshot()
        self.context = RequestContext("actor-owner", "authorization-owner")

    def make_request(self, *, request_id="idea-capture-1", action="capture-and-continue", **updates):
        request = {
            "schema_version": "context.idea-capture-request/v1alpha1",
            "request_id": request_id,
            "project_id": self.snapshot["project"]["project_id"],
            "expected_revision": self.snapshot["project"]["revision"],
            "idea_id": f"idea-{request_id}",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_abcdefghijklmnopqrstuvwxyz",
            "summary": "Keep the idea available for later review.",
            "action": action,
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-06",
            "correlation_ref": "campaign:M3",
        }
        request.update(updates)
        return request

    @staticmethod
    def make_service(store, *, registry_digest="a" * 64):
        return StateMCPService(
            store,
            authorizer=_AllowAuthorizer(),
            registry_digest=registry_digest,
            clock=lambda: "2026-08-14T08:30:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

    def _capture(self, request):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteStateStore(Path(directory.name) / "state.sqlite3")
        store.initialize()
        store.create_project(copy.deepcopy(self.snapshot))
        response = self.make_service(store).call_tool(
            "context.idea.capture", request, context=self.context
        )
        return response, store

    def test_capture_and_continue_preserves_active_execution_authority(self):
        response, store = self._capture(self.make_request())
        self.assertTrue(response["ok"], response["error"])

        stored = store.read_project(self.snapshot["project"]["project_id"])
        events = store.read_events(self.snapshot["project"]["project_id"])
        self.assertEqual(stored["project"]["revision"], 10)
        self.assertEqual(stored["project"]["active_work_ids"], ["work-active"])
        self.assertEqual(stored["project"]["primary_work_id"], "work-active")
        self.assertEqual(stored["project"]["effect_high_watermark"], 0)
        self.assertEqual(stored["works"], self.snapshot["works"])
        self.assertEqual(
            {
                key: stored["claims"][0][key]
                for key in ("claim_id", "work_id", "actor_ref", "status", "scope_owners", "lease_expires_at")
            },
            {
                key: self.snapshot["claims"][0][key]
                for key in ("claim_id", "work_id", "actor_ref", "status", "scope_owners", "lease_expires_at")
            },
        )
        self.assertEqual(stored["ideas"][0]["status"], "candidate")
        self.assertEqual(stored["ideas"][0]["parent_work_id"], "work-active")
        self.assertEqual(stored["ideas"][0]["return_work_id"], "work-active")
        self.assertIsNone(stored["ideas"][0]["promotion_target"])
        self.assertEqual(events[0]["schema_version"], "context.idea-event/v1alpha1")
        self.assertEqual(events[0]["idea_transition"]["operation"], "capture-and-continue")

    def test_parked_idea_preserves_expiry_and_never_activates_work(self):
        response, store = self._capture(
            self.make_request(
                action="park",
                expiry="2026-08-15T08:30:00+08:00",
            )
        )
        self.assertTrue(response["ok"], response["error"])
        stored = store.read_project(self.snapshot["project"]["project_id"])
        self.assertEqual(stored["ideas"][0]["status"], "parked")
        self.assertEqual(stored["ideas"][0]["expiry"], "2026-08-15T08:30:00+08:00")
        self.assertEqual(stored["project"]["primary_work_id"], "work-active")
        self.assertEqual(stored["claims"][0]["work_id"], "work-active")

    def test_explicit_switch_intent_records_only_a_switch_proposal(self):
        response, store = self._capture(
            self.make_request(
                action="propose-switch",
                switch_target_work_id="work-target",
            )
        )
        self.assertTrue(response["ok"], response["error"])
        stored = store.read_project(self.snapshot["project"]["project_id"])
        events = store.read_events(self.snapshot["project"]["project_id"])
        self.assertEqual(stored["ideas"][0]["status"], "proposed")
        self.assertEqual(stored["ideas"][0]["promotion_target"], "work-target")
        self.assertEqual(stored["project"]["primary_work_id"], "work-active")
        self.assertEqual(stored["claims"][0]["work_id"], "work-active")
        self.assertEqual(events[0]["idea_transition"]["switch_target_work_id"], "work-target")
        self.assertIsNone(events[0]["task_transition"])

    def test_capture_rejects_expired_or_unbound_or_unauthorized_proposal_without_mutation(self):
        cases = (
            self.make_request(expiry="2026-08-14T08:30:00+08:00"),
            self.make_request(parent_work_id="work-target"),
            self.make_request(return_work_id="work-target"),
            self.make_request(action="propose-switch"),
            self.make_request(action="capture-and-continue", switch_target_work_id="work-target"),
        )
        for request in cases:
            with self.subTest(request=request):
                response, store = self._capture(request)
                self.assertFalse(response["ok"])
                self.assertEqual(store.read_project(request["project_id"]), self.snapshot)
                self.assertEqual(store.read_events(request["project_id"]), [])

    def test_v3_generic_commit_cannot_bypass_idea_capture_gate(self):
        request = self.make_request()
        generic = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "forged-generic-idea",
            "project_id": request["project_id"],
            "expected_revision": 9,
            "causation_ref": "work:M3-06",
            "correlation_ref": "campaign:M3",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "ideas",
                    "object_id": request["idea_id"],
                    "value": {
                        "idea_id": request["idea_id"],
                        "parent_work_id": "work-active",
                        "source_ref": request["source_ref"],
                        "summary": request["summary"],
                        "status": "candidate",
                        "return_work_id": "work-active",
                        "expiry": None,
                        "attempt_budget": None,
                        "promotion_target": None,
                        "evidence_ids": [],
                    },
                }
            ],
        }
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteStateStore(Path(directory.name) / "state.sqlite3")
        store.initialize()
        store.create_project(copy.deepcopy(self.snapshot))
        response = self.make_service(store).call_tool(
            "context.state.commit", generic, context=self.context
        )
        self.assertFalse(response["ok"])
        self.assertEqual(store.read_project(generic["project_id"]), self.snapshot)
        self.assertEqual(store.read_events(generic["project_id"]), [])

    def test_capture_replays_same_receipt_after_service_restart(self):
        request = self.make_request(request_id="idea-durable-replay")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            first = self.make_service(store).call_tool(
                "context.idea.capture", request, context=self.context
            )
            replay = self.make_service(store).call_tool(
                "context.idea.capture", request, context=self.context
            )

        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(replay, first)

    def test_capture_replays_original_receipt_after_intervening_event_and_restart(self):
        first_request = self.make_request(request_id="idea-durable-before-later-event")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            first = self.make_service(store).call_tool(
                "context.idea.capture", first_request, context=self.context
            )
            later_request = self.make_request(
                request_id="idea-durable-later-event",
                expected_revision=10,
            )
            later = self.make_service(store).call_tool(
                "context.idea.capture", later_request, context=self.context
            )
            replay = self.make_service(store).call_tool(
                "context.idea.capture", first_request, context=self.context
            )

        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(later["ok"], later["error"])
        self.assertEqual(replay, first)

    def test_capture_replays_same_event_bound_receipt_after_registry_upgrade(self):
        request = self.make_request(request_id="idea-replay-after-registry-upgrade")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            first = self.make_service(store, registry_digest="a" * 64).call_tool(
                "context.idea.capture", request, context=self.context
            )
            replay = self.make_service(store, registry_digest="b" * 64).call_tool(
                "context.idea.capture", request, context=self.context
            )

        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(replay, first)

    def test_v4_state_event_and_v1_idea_event_share_one_replay_chain(self):
        decision = {
            "decision_id": "decision-before-idea",
            "work_id": "work-active",
            "status": "accepted",
            "statement": "Preserve active execution authority during Idea capture.",
            "decided_at": "2026-08-14T08:30:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": [],
        }
        commit_request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "state-before-idea",
            "project_id": self.snapshot["project"]["project_id"],
            "expected_revision": 9,
            "causation_ref": "work:M3-06",
            "correlation_ref": "campaign:M3",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "decisions",
                    "object_id": decision["decision_id"],
                    "value": decision,
                }
            ],
        }
        idea_request = self.make_request(
            request_id="idea-after-v4-state-event",
            expected_revision=10,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store)
            committed = service.call_tool(
                "context.state.commit", commit_request, context=self.context
            )
            captured = service.call_tool(
                "context.idea.capture", idea_request, context=self.context
            )
            events = store.read_events(commit_request["project_id"])
            stored = store.read_project(commit_request["project_id"])

        self.assertTrue(committed["ok"], committed["error"])
        self.assertTrue(captured["ok"], captured["error"])
        self.assertEqual(
            [event["schema_version"] for event in events],
            ["context.state-event/v4alpha1", "context.idea-event/v1alpha1"],
        )
        self.assertEqual(events[1]["previous_event_sha256"], events[0]["event_sha256"])
        self.assertEqual(replay_state_events(self.snapshot, events), stored)

    def test_reducer_rejects_a_forged_idea_event_that_changes_execution_authority(self):
        request = self.make_request()
        response, _ = self._capture(request)
        captured = response["result"]["event"]
        project_after = copy.deepcopy(captured["project_after"])
        project_after["active_work_ids"] = ["work-target"]
        project_after["primary_work_id"] = "work-target"
        forged = build_state_event(
            event_id=captured["event_id"],
            event_type=captured["event_type"],
            project_id=captured["project_id"],
            sequence_no=captured["sequence_no"],
            revision_before=captured["revision_before"],
            occurred_at=captured["occurred_at"],
            actor_ref=captured["actor_ref"],
            causation_ref=captured["causation_ref"],
            correlation_ref=captured["correlation_ref"],
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=captured["changes"],
            project_after=project_after,
            idea_transition=captured["idea_transition"],
            schema_version="context.idea-event/v1alpha1",
        )

        with self.assertRaisesRegex(StateEventError, "Idea event cannot change active execution authority"):
            replay_state_events(self.snapshot, [forged])

    def test_reducer_rejects_an_expired_or_unbound_idea_event(self):
        response, _ = self._capture(self.make_request())
        captured = response["result"]["event"]
        changes = copy.deepcopy(captured["changes"])
        changes[0]["value"]["expiry"] = captured["occurred_at"]
        forged = build_state_event(
            event_id=captured["event_id"],
            event_type=captured["event_type"],
            project_id=captured["project_id"],
            sequence_no=captured["sequence_no"],
            revision_before=captured["revision_before"],
            occurred_at=captured["occurred_at"],
            actor_ref=captured["actor_ref"],
            causation_ref=captured["causation_ref"],
            correlation_ref=captured["correlation_ref"],
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=changes,
            project_after=captured["project_after"],
            idea_transition=captured["idea_transition"],
            schema_version="context.idea-event/v1alpha1",
        )

        with self.assertRaisesRegex(StateEventError, "Idea event expiry"):
            replay_state_events(self.snapshot, [forged])

    def test_reducer_rejects_an_idea_event_with_an_unbound_second_idea(self):
        response, _ = self._capture(self.make_request())
        captured = response["result"]["event"]
        changes = copy.deepcopy(captured["changes"])
        second_idea = copy.deepcopy(changes[0]["value"])
        second_idea["idea_id"] = "idea-unbound-second"
        changes.append(
            {
                "collection": "ideas",
                "object_id": second_idea["idea_id"],
                "value": second_idea,
            }
        )
        with self.assertRaisesRegex(StateEventError, "exactly one Idea"):
            build_state_event(
                event_id=captured["event_id"],
                event_type=captured["event_type"],
                project_id=captured["project_id"],
                sequence_no=captured["sequence_no"],
                revision_before=captured["revision_before"],
                occurred_at=captured["occurred_at"],
                actor_ref=captured["actor_ref"],
                causation_ref=captured["causation_ref"],
                correlation_ref=captured["correlation_ref"],
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=changes,
                project_after=captured["project_after"],
                idea_transition=captured["idea_transition"],
                schema_version="context.idea-event/v1alpha1",
            )

    def test_idea_event_is_an_independent_strict_registered_schema_family(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load((root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        entries = {
            item["schema_id"]: item
            for item in registry["schemas"]
            if item["schema_id"]
            in {"context.state-event", "context.idea-event", "context.idea-capture"}
        }
        self.assertEqual(
            entries["context.state-event"]["current_wire_version"],
            "context.state-event/v4alpha1",
        )
        self.assertEqual(
            entries["context.idea-event"]["current_wire_version"],
            "context.idea-event/v2alpha1",
        )
        self.assertIn(
            "context.idea-event/v1alpha1",
            entries["context.idea-event"]["supported_wire_versions"],
        )
        self.assertEqual(
            entries["context.idea-capture"]["current_wire_version"],
            "context.idea-capture-request/v2alpha1",
        )
        self.assertIn(
            "context.idea-capture-request/v1alpha1",
            entries["context.idea-capture"]["supported_wire_versions"],
        )
        schemas = {}
        for schema_id, entry in entries.items():
            path = root / entry["artifact_path"]
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            schemas[schema_id] = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schemas[schema_id])

        historical_capture = json.loads(
            (root / "schemas/m3-06/idea-capture.schema.json").read_text(encoding="utf-8")
        )
        historical_idea_event = json.loads(
            (root / "schemas/m3-06/idea-event-v1alpha1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(historical_capture)
        Draft202012Validator.check_schema(historical_idea_event)
        request = self.make_request()
        request_validator = Draft202012Validator(historical_capture)
        request_validator.validate(request)
        invalid_request = copy.deepcopy(request)
        invalid_request["source_ref"] = "opaque://full-body"
        with self.assertRaises(ValidationError):
            request_validator.validate(invalid_request)

        response, _ = self._capture(request)
        event = response["result"]["event"]
        event_validator = Draft202012Validator(
            historical_idea_event,
            registry=Registry().with_resource(
                "https://context-control-plane.dev/schema/context.typed-state/v3alpha1",
                Resource.from_contents(
                    json.loads(
                        (root / "schemas/m3-05/typed-state-v3alpha1.schema.json").read_text(
                            encoding="utf-8"
                        )
                    )
                ),
            ),
        )
        event_validator.validate(event)
        invalid_event = copy.deepcopy(event)
        invalid_event["experiment_transition"] = {"forged": True}
        with self.assertRaises(ValidationError):
            event_validator.validate(invalid_event)

        invalid_collection = copy.deepcopy(event)
        invalid_collection["changes"].append(
            {
                "collection": "works",
                "object_id": "work-active",
                "value": copy.deepcopy(self.snapshot["works"][2]),
            }
        )
        with self.assertRaises(ValidationError):
            event_validator.validate(invalid_collection)

        tool = next(
            item
            for item in state_mcp_tool_definitions()
            if item["name"] == "context.idea.capture"
        )
        tool_validator = Draft202012Validator(tool["inputSchema"])
        invalid_tool_request = self.make_request(source_ref="rng_abcdefghijklmnopqrstuvwxy1")
        with self.assertRaises(ValidationError):
            tool_validator.validate(invalid_tool_request)

    def test_benchmark_rejects_any_execution_authority_mutation(self):
        class _MutatingReadSQLiteStore(SQLiteStateStore):
            def read_project(self, project_id):
                snapshot = super().read_project(project_id)
                if snapshot["project"]["revision"] > 9:
                    snapshot["works"][2]["owner_refs"] = ["actor-unexpected"]
                return snapshot

        root = Path(__file__).parents[1]
        with patch.object(
            idea_benchmark,
            "SQLiteStateStore",
            _MutatingReadSQLiteStore,
        ), self.assertRaisesRegex(ValueError, "execution authority"):
            run_idea_continuity_benchmark(
                root=root,
                samples=1,
                observed_at="2026-08-14T08:30:00+08:00",
            )

    def test_zero_service_idea_benchmark_is_strict_and_current(self):
        root = Path(__file__).parents[1]
        generated = run_idea_continuity_benchmark(
            root=root,
            samples=2,
            observed_at="2026-08-14T08:30:00+08:00",
        )
        self.assertEqual(generated["measurement"]["successful_runs"], 2)
        self.assertEqual(generated["environment"]["external_services"], 0)
        validate_idea_continuity_benchmark_receipt(generated, root=root)

        receipt = json.loads(
            (root / "experiments/routing/m3-06-idea-continuity-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_idea_continuity_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["measurement"]["samples"], 40)

        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.idea-continuity-benchmark"
        )
        path = root / entry["artifact_path"]
        self.assertEqual(
            entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(receipt)


if __name__ == "__main__":
    unittest.main()
