"""M3-07 deterministic Idea review, dedupe, and correction protection."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_review import (
    IdeaReviewError,
    add_idea_relationship,
    append_idea_occurrence,
    apply_idea_review,
    build_typed_state_v3_to_v4_migration_receipt,
    compute_idea_dedupe_key,
    evaluate_correction_write_gate,
    migrate_typed_state_v3_to_v4,
    open_correction_protection,
    packet_eligible_ideas,
    release_correction_protection,
    rollback_typed_state_v4_to_v3,
    upsert_idea_observation,
    validate_typed_state_v3_to_v4_migration_receipt,
)
from context_control_plane.route_apply import RouteApplyError, apply_route
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
)
from context_control_plane.state_mcp import RequestContext, StateMCPService
from context_control_plane.sticky_router import (
    canonical_route_decision_bytes,
    route_task_input,
)
from context_control_plane.typed_state import validate_typed_state


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M307IdeaReviewTests(unittest.TestCase):
    @staticmethod
    def v4_snapshot():
        return migrate_typed_state_v3_to_v4(
            build_idea_snapshot(), migrated_at="2026-08-14T09:00:00+08:00"
        )

    def snapshot_with_idea(self):
        snapshot = self.v4_snapshot()
        return upsert_idea_observation(
            snapshot,
            idea_id="idea-first",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_abcdefghijklmnopqrstuvwxyz",
            summary="Capture one bounded candidate.",
            scope_ref="work://work-active",
            urgency="later",
            review_at=None,
            occurrence_id="occ-first",
            observed_at="2026-08-14T09:01:00+08:00",
        )

    def snapshot_with_release_evidence(self):
        snapshot = self.snapshot_with_idea()
        snapshot["evidence"].append(
            {
                "evidence_id": "evidence-correction-verified",
                "kind": "test",
                "artifact_ref": "artifact://m3-07/correction-verification",
                "content_sha256": "e" * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T09:08:00+08:00",
                "verified_at": "2026-08-14T09:08:30+08:00",
            }
        )
        validate_typed_state(snapshot)
        return snapshot

    def test_v3_migration_adds_v4_defaults_and_rolls_back_losslessly(self):
        v3 = build_idea_snapshot()
        v4 = migrate_typed_state_v3_to_v4(
            copy.deepcopy(v3), migrated_at="2026-08-14T09:00:00+08:00"
        )
        validate_typed_state(v4)
        self.assertEqual(v4["schema_version"], "context.typed-state/v4alpha1")
        self.assertEqual(v4["idea_relationships"], [])
        self.assertEqual(v4["idea_occurrences"], [])
        self.assertEqual(v4["idea_reviews"], [])
        self.assertEqual(v4["correction_protections"], [])
        self.assertEqual(rollback_typed_state_v4_to_v3(v4), v3)

    def test_migration_receipt_binds_source_head_snapshot_target_and_authority(self):
        source = build_idea_snapshot()
        target = migrate_typed_state_v3_to_v4(
            source, migrated_at="2026-08-14T09:00:00+08:00"
        )
        receipt = build_typed_state_v3_to_v4_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-v3-v4-project-fixture",
            source_event_head_sha256=None,
            registry_digest="a" * 64,
            authorization_ref="authz://test/migrate",
            migrated_at="2026-08-14T09:00:00+08:00",
        )
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["source_revision"], source["project"]["revision"])
        self.assertEqual(receipt["from_schema_version"], "context.typed-state/v3alpha1")
        self.assertEqual(receipt["to_schema_version"], "context.typed-state/v4alpha1")
        validate_typed_state_v3_to_v4_migration_receipt(receipt, source=source, target=target)

        tampered = copy.deepcopy(target)
        tampered["project"]["governance_ref"] = "governance://tampered"
        with self.assertRaisesRegex(IdeaReviewError, "target"):
            validate_typed_state_v3_to_v4_migration_receipt(
                receipt, source=source, target=tampered
            )

    def test_sqlite_migration_persists_an_atomic_v3_v4_boundary_and_receipt(self):
        source = build_idea_snapshot()
        target = migrate_typed_state_v3_to_v4(
            source, migrated_at="2026-08-14T09:00:00+08:00"
        )
        receipt = build_typed_state_v3_to_v4_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-sqlite-v3-v4",
            source_event_head_sha256=None,
            registry_digest="b" * 64,
            authorization_ref="authz://test/migrate",
            migrated_at="2026-08-14T09:00:00+08:00",
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(source)
            persisted = store.migrate_project(
                project_id=source["project"]["project_id"],
                expected_revision=source["project"]["revision"],
                expected_event_head_sha256=None,
                target_snapshot=target,
                migration_receipt=receipt,
            )
            self.assertEqual(persisted, receipt)
            self.assertEqual(store.read_project(source["project"]["project_id"]), target)
            self.assertEqual(
                store.read_migration_receipt(
                    source["project"]["project_id"], receipt["migration_id"]
                ),
                receipt,
            )
            self.assertEqual(
                store.migrate_project(
                    project_id=source["project"]["project_id"],
                    expected_revision=source["project"]["revision"],
                    expected_event_head_sha256=None,
                    target_snapshot=target,
                    migration_receipt=receipt,
                ),
                receipt,
            )

    def test_sqlite_migration_rolls_back_receipt_when_snapshot_update_faults(self):
        source = build_idea_snapshot()
        target = migrate_typed_state_v3_to_v4(
            source, migrated_at="2026-08-14T09:00:00+08:00"
        )
        receipt = build_typed_state_v3_to_v4_migration_receipt(
            source=source,
            target=target,
            migration_id="migration-sqlite-fault",
            source_event_head_sha256=None,
            registry_digest="c" * 64,
            authorization_ref="authz://test/migrate",
            migrated_at="2026-08-14T09:00:00+08:00",
        )
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(
                Path(directory) / "state.sqlite3",
                fault_hook=lambda stage: (
                    (_ for _ in ()).throw(RuntimeError("fault"))
                    if stage == "after_migration_receipt_insert"
                    else None
                ),
            )
            store.initialize()
            store.create_project(source)
            with self.assertRaisesRegex(RuntimeError, "fault"):
                store.migrate_project(
                    project_id=source["project"]["project_id"],
                    expected_revision=source["project"]["revision"],
                    expected_event_head_sha256=None,
                    target_snapshot=target,
                    migration_receipt=receipt,
                )
            self.assertEqual(store.read_project(source["project"]["project_id"]), source)
            self.assertIsNone(
                store.read_migration_receipt(
                    source["project"]["project_id"], receipt["migration_id"]
                )
            )

    def test_v2_capture_converges_same_key_after_restart_without_execution_authority(self):
        snapshot = self.v4_snapshot()
        context = RequestContext("actor-owner", "authorization-owner")

        def request(*, request_id, revision, idea_id, occurrence_id, source_ref):
            return {
                "schema_version": "context.idea-capture-request/v2alpha1",
                "request_id": request_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": revision,
                "idea_id": idea_id,
                "parent_work_id": "work-active",
                "return_work_id": "work-active",
                "source_ref": source_ref,
                "summary": "Keep a bounded candidate for later review.",
                "scope_ref": "work://work-active",
                "urgency": "later",
                "review_at": None,
                "occurrence_id": occurrence_id,
                "action": "capture-and-continue",
                "switch_target_work_id": None,
                "expiry": None,
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database)
            store.initialize()
            store.create_project(snapshot)
            first = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="d" * 64,
                clock=lambda: "2026-08-14T09:01:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.idea.capture",
                request(
                    request_id="capture-first",
                    revision=9,
                    idea_id="idea-first",
                    occurrence_id="occ-first",
                    source_ref="rng_abcdefghijklmnopqrstuvwxyz",
                ),
                context=context,
            )
            second = StateMCPService(
                SQLiteStateStore(database),
                authorizer=_AllowAuthorizer(),
                registry_digest="d" * 64,
                clock=lambda: "2026-08-14T09:02:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.idea.capture",
                request(
                    request_id="capture-second",
                    revision=10,
                    idea_id="idea-submitted-second",
                    occurrence_id="occ-second",
                    source_ref="rng_bcdefghijklmnopqrstuvwxyza",
                ),
                context=context,
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(second["ok"], second["error"])
        self.assertEqual(second["result"]["canonical_idea_id"], "idea-first")
        self.assertEqual(len(stored["ideas"]), 1)
        self.assertEqual(len(stored["idea_occurrences"]), 2)
        self.assertEqual(
            [event["schema_version"] for event in events],
            ["context.idea-event/v2alpha1", "context.idea-event/v2alpha1"],
        )
        self.assertEqual(stored["project"]["primary_work_id"], "work-active")
        self.assertEqual(stored["works"], snapshot["works"])

    def test_v2_capture_refreshes_pending_effect_revision_and_replays(self):
        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        effect_request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "capture-pending-effect-authorize",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "action": "authorize",
            "effect_id": "capture-pending-effect",
            "effect_key": "capture-pending-effect-key",
            "work_id": "work-active",
            "claim_id": "claim-active",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "idea/active"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        capture_request = {
            "schema_version": "context.idea-capture-request/v2alpha1",
            "request_id": "capture-with-pending-effect",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "idea_id": "idea-with-pending-effect",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_bcdefghijklmnopqrstuvwxyza",
            "summary": "Capture a candidate while an effect is pending.",
            "scope_ref": "work://work-active",
            "urgency": "later",
            "review_at": None,
            "occurrence_id": "occ-with-pending-effect",
            "action": "capture-and-continue",
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="d" * 64,
                clock=lambda: "2026-08-14T09:02:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            authorized = service.call_tool(
                "context.state.effect", effect_request, context=context
            )
            captured = service.call_tool(
                "context.idea.capture", capture_request, context=context
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(authorized["ok"], authorized["error"])
        self.assertTrue(captured["ok"], captured["error"])
        self.assertEqual(stored["project"]["revision"], 11)
        effect = next(item for item in stored["effects"] if item["effect_id"] == "capture-pending-effect")
        self.assertEqual(effect["expected_project_revision"], 11)
        self.assertEqual(
            replay_state_events(
                snapshot,
                events,
                starting_sequence_no=1,
                previous_event_sha256=None,
            ),
            stored,
        )

    def test_v2_same_key_stale_cas_rereads_and_appends_the_losing_occurrence(self):
        snapshot = self.v4_snapshot()
        context = RequestContext("actor-owner", "authorization-owner")

        def make_request(request_id, idea_id, occurrence_id, source_ref):
            return {
                "schema_version": "context.idea-capture-request/v2alpha1",
                "request_id": request_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": 9,
                "idea_id": idea_id,
                "parent_work_id": "work-active",
                "return_work_id": "work-active",
                "source_ref": source_ref,
                "summary": "Converge one concurrent candidate.",
                "scope_ref": "work://work-active",
                "urgency": "later",
                "review_at": None,
                "occurrence_id": occurrence_id,
                "action": "capture-and-continue",
                "switch_target_work_id": None,
                "expiry": None,
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database)
            store.initialize()
            store.create_project(snapshot)
            first = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="f" * 64,
                clock=lambda: "2026-08-14T09:01:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.idea.capture",
                make_request(
                    "concurrent-first",
                    "idea-concurrent-first",
                    "occ-concurrent-first",
                    "rng_abcdefghijklmnopqrstuvwxyz",
                ),
                context=context,
            )
            losing = StateMCPService(
                SQLiteStateStore(database),
                authorizer=_AllowAuthorizer(),
                registry_digest="f" * 64,
                clock=lambda: "2026-08-14T09:02:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.idea.capture",
                make_request(
                    "concurrent-losing",
                    "idea-concurrent-losing",
                    "occ-concurrent-losing",
                    "rng_bcdefghijklmnopqrstuvwxyza",
                ),
                context=context,
            )
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(losing["ok"], losing["error"])
        self.assertEqual(losing["result"]["canonical_idea_id"], "idea-concurrent-first")
        self.assertEqual(stored["project"]["revision"], 11)
        self.assertEqual(len(stored["ideas"]), 1)
        self.assertEqual(len(stored["idea_occurrences"]), 2)

    def test_v2_simultaneous_same_key_cas_converges_after_the_loser_rereads(self):
        class RacingStore(SQLiteStateStore):
            barrier = threading.Barrier(2)
            barrier_lock = threading.Lock()
            commit_count = 0

            def commit_event(self, **kwargs):
                with self.barrier_lock:
                    type(self).commit_count += 1
                    synchronize = type(self).commit_count <= 2
                if synchronize:
                    self.barrier.wait(timeout=5)
                return super().commit_event(**kwargs)

        snapshot = self.v4_snapshot()
        context = RequestContext("actor-owner", "authorization-owner")

        def request(index):
            return {
                "schema_version": "context.idea-capture-request/v2alpha1",
                "request_id": f"race-{index}",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": 9,
                "idea_id": f"idea-race-{index}",
                "parent_work_id": "work-active",
                "return_work_id": "work-active",
                "source_ref": (
                    "rng_abcdefghijklmnopqrstuvwxyz"
                    if index == 1
                    else "rng_bcdefghijklmnopqrstuvwxyza"
                ),
                "summary": "Converge a simultaneous candidate.",
                "scope_ref": "work://work-active",
                "urgency": "later",
                "review_at": None,
                "occurrence_id": f"occ-race-{index}",
                "action": "capture-and-continue",
                "switch_target_work_id": None,
                "expiry": None,
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            bootstrap = SQLiteStateStore(database)
            bootstrap.initialize()
            bootstrap.create_project(snapshot)

            def capture(index):
                service = StateMCPService(
                    RacingStore(database),
                    authorizer=_AllowAuthorizer(),
                    registry_digest="1" * 64,
                    clock=lambda: "2026-08-14T09:03:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                )
                return service.call_tool(
                    "context.idea.capture", request(index), context=context
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(capture, (1, 2)))
            stored = bootstrap.read_project(snapshot["project"]["project_id"])

        self.assertTrue(all(response["ok"] for response in responses), responses)
        canonical_ids = {response["result"]["canonical_idea_id"] for response in responses}
        self.assertEqual(len(canonical_ids), 1)
        self.assertEqual(len(stored["ideas"]), 1)
        self.assertEqual(len(stored["idea_occurrences"]), 2)

    def test_capture_and_review_concurrency_is_replayable_without_silent_loss(self):
        class RacingStore(SQLiteStateStore):
            barrier = threading.Barrier(2)
            lock = threading.Lock()
            commits = 0

            def commit_event(self, **kwargs):
                with self.lock:
                    type(self).commits += 1
                    synchronize = type(self).commits <= 2
                if synchronize:
                    self.barrier.wait(timeout=5)
                return super().commit_event(**kwargs)

        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        capture_request = {
            "schema_version": "context.idea-capture-request/v2alpha1",
            "request_id": "capture-review-race-capture",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-race-submitted",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_bcdefghijklmnopqrstuvwxyza",
            "summary": "Capture one bounded candidate.",
            "scope_ref": "work://work-active",
            "urgency": "later",
            "review_at": None,
            "occurrence_id": "occ-capture-review-race",
            "action": "capture-and-continue",
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        review_request = {
            "schema_version": "context.idea-review-request/v1alpha1",
            "request_id": "capture-review-race-review",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-first",
            "review_id": "review-capture-review-race",
            "decision": "keep",
            "urgency": "next",
            "impact": "medium",
            "review_at": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            bootstrap = SQLiteStateStore(database)
            bootstrap.initialize()
            bootstrap.create_project(snapshot)

            def call(tool, request):
                return StateMCPService(
                    RacingStore(database),
                    authorizer=_AllowAuthorizer(),
                    registry_digest="1" * 64,
                    clock=lambda: "2026-08-14T09:03:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                ).call_tool(tool, request, context=context)

            with ThreadPoolExecutor(max_workers=2) as executor:
                capture_future = executor.submit(
                    call, "context.idea.capture", capture_request
                )
                review_future = executor.submit(
                    call, "context.idea.review", review_request
                )
                captured = capture_future.result()
                reviewed = review_future.result()
            if not reviewed["ok"]:
                self.assertEqual(reviewed["error"]["code"], "conflict")
                retry = copy.deepcopy(review_request)
                retry["expected_revision"] = bootstrap.read_project(
                    snapshot["project"]["project_id"]
                )["project"]["revision"]
                reviewed = StateMCPService(
                    bootstrap,
                    authorizer=_AllowAuthorizer(),
                    registry_digest="1" * 64,
                    clock=lambda: "2026-08-14T09:04:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                ).call_tool("context.idea.review", retry, context=context)
            stored = bootstrap.read_project(snapshot["project"]["project_id"])
            events = bootstrap.read_events(snapshot["project"]["project_id"])

        self.assertTrue(captured["ok"], captured["error"])
        self.assertTrue(reviewed["ok"], reviewed["error"])
        self.assertEqual(len(stored["ideas"]), 1)
        self.assertEqual(len(stored["idea_occurrences"]), 2)
        self.assertEqual(len(stored["idea_reviews"]), 1)
        self.assertEqual(len(events), 2)

    def test_v4_generic_commit_cannot_bypass_idea_review_fields(self):
        snapshot = self.snapshot_with_idea()
        forged = copy.deepcopy(snapshot["ideas"][0])
        forged["urgency"] = "immediate"
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "forged-v4-idea-review",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "ideas",
                    "object_id": forged["idea_id"],
                    "value": forged,
                }
            ],
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="e" * 64,
                clock=lambda: "2026-08-14T09:08:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.commit",
                request,
                context=RequestContext("actor-owner", "authorization-owner"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
        self.assertFalse(response["ok"])
        self.assertEqual(stored, snapshot)

    def test_generic_route_correction_rejects_idea_field_mutation_before_state_access(self):
        request = {
            "schema_version": "context.task-route-apply-request/v1alpha1",
            "request_id": "forged-route-idea-correction",
            "project_id": "project-idea-benchmark",
            "proposal_sha256": "a" * 64,
            "operation": "correction",
            "expected_project_revision": 9,
            "expected_active_work_id": "work-active",
            "expected_active_work_revision": 1,
            "target_work_id": None,
            "target_work_revision": None,
            "authorization_ref": "authz://test/correction",
            "checkpoint_ref": None,
            "checkpoint_binding": None,
            "child_work": None,
            "correction_changes": [
                {
                    "collection": "ideas",
                    "object_id": "idea-first",
                    "value": {"idea_id": "idea-first"},
                }
            ],
            "supersedes_event_id": "event-capture-first",
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with self.assertRaisesRegex(RouteApplyError, "dedicated Idea"):
            apply_route(
                None,
                request,
                decision={},
                context={},
                authorizer=None,
                clock=lambda: "2026-08-14T09:08:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )

    def test_v1_history_and_v2_events_recover_from_the_persisted_migration_boundary(self):
        initial = build_idea_snapshot()
        context = RequestContext("actor-owner", "authorization-owner")
        v1_request = {
            "schema_version": "context.idea-capture-request/v1alpha1",
            "request_id": "legacy-before-v4",
            "project_id": initial["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-legacy-before-v4",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_abcdefghijklmnopqrstuvwxyz",
            "summary": "Legacy candidate before migration.",
            "action": "capture-and-continue",
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-06",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(initial)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="2" * 64,
                clock=lambda: "2026-08-14T09:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            legacy = service.call_tool(
                "context.idea.capture", v1_request, context=context
            )
            source = store.read_project(initial["project"]["project_id"])
            old_events = store.read_events(initial["project"]["project_id"])
            boundary = migrate_typed_state_v3_to_v4(
                source, migrated_at="2026-08-14T09:01:00+08:00"
            )
            receipt = build_typed_state_v3_to_v4_migration_receipt(
                source=source,
                target=boundary,
                migration_id="migration-after-v1-history",
                source_event_head_sha256=old_events[-1]["event_sha256"],
                registry_digest="2" * 64,
                authorization_ref="authorization-owner",
                migrated_at="2026-08-14T09:01:00+08:00",
            )
            store.migrate_project(
                project_id=initial["project"]["project_id"],
                expected_revision=10,
                expected_event_head_sha256=old_events[-1]["event_sha256"],
                target_snapshot=boundary,
                migration_receipt=receipt,
            )
            v2_request = {
                "schema_version": "context.idea-capture-request/v2alpha1",
                "request_id": "v2-after-boundary",
                "project_id": initial["project"]["project_id"],
                "expected_revision": 10,
                "idea_id": "idea-v2-after-boundary",
                "parent_work_id": "work-active",
                "return_work_id": "work-active",
                "source_ref": "rng_bcdefghijklmnopqrstuvwxyza",
                "summary": "Candidate after the migration boundary.",
                "scope_ref": "work://work-active",
                "urgency": "next",
                "review_at": None,
                "occurrence_id": "occ-v2-after-boundary",
                "action": "capture-and-continue",
                "switch_target_work_id": None,
                "expiry": None,
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }
            captured = service.call_tool(
                "context.idea.capture", v2_request, context=context
            )
            events = store.read_events(initial["project"]["project_id"])
            stored = store.read_project(initial["project"]["project_id"])

        self.assertTrue(legacy["ok"], legacy["error"])
        self.assertTrue(captured["ok"], captured["error"])
        self.assertEqual(
            [event["schema_version"] for event in events],
            ["context.idea-event/v1alpha1", "context.idea-event/v2alpha1"],
        )
        self.assertEqual(
            replay_state_events(
                boundary,
                [events[1]],
                starting_sequence_no=2,
                previous_event_sha256=events[0]["event_sha256"],
            ),
            stored,
        )
        with self.assertRaisesRegex(StateEventError, "wire version"):
            replay_state_events(initial, events)

    def test_review_and_correction_protection_commit_as_candidate_only_v2_events(self):
        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        review_request = {
            "schema_version": "context.idea-review-request/v1alpha1",
            "request_id": "review-durable",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-first",
            "review_id": "review-durable",
            "decision": "keep",
            "urgency": "immediate",
            "impact": "high",
            "review_at": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        protection_request = {
            "schema_version": "context.idea-correction-protection-request/v1alpha1",
            "request_id": "protect-durable",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "idea_id": "idea-first",
            "protection_id": "protection-durable",
            "affected_work_ids": ["work-active"],
            "affected_scope_refs": ["capability:idea/active"],
            "reason": "Current evidence requires correction review.",
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="3" * 64,
                clock=lambda: "2026-08-14T09:09:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            reviewed = service.call_tool(
                "context.idea.review", review_request, context=context
            )
            protected = service.call_tool(
                "context.idea.correction.protect", protection_request, context=context
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(reviewed["ok"], reviewed["error"])
        self.assertTrue(protected["ok"], protected["error"])
        self.assertEqual(stored["ideas"][0]["urgency"], "immediate")
        self.assertEqual(len(stored["idea_reviews"]), 1)
        self.assertEqual(len(stored["correction_protections"]), 1)
        self.assertEqual(stored["project"]["primary_work_id"], "work-active")
        self.assertEqual(stored["works"], snapshot["works"])
        self.assertEqual(
            [event["idea_transition"]["operation"] for event in events],
            ["review-updated", "correction-guarded"],
        )

    def test_review_event_cannot_smuggle_a_correction_protection(self):
        snapshot = self.snapshot_with_idea()
        occurred_at = "2026-08-14T09:09:00+08:00"
        candidate = apply_idea_review(
            snapshot,
            idea_id="idea-first",
            review_id="review-smuggled-protection",
            reviewer_ref="actor-owner",
            decision="keep",
            urgency="next",
            impact="medium",
            review_at=None,
            evidence_ids=[],
            reviewed_at=occurred_at,
        )
        candidate = open_correction_protection(
            candidate,
            protection_id="protection-smuggled-by-review",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="This write was not authorized by the review operation.",
            evidence_ids=[],
            opened_at=occurred_at,
        )
        candidate["claims"][0]["expected_project_revision"] = 10
        candidate["project"]["revision"] = 10
        candidate["project"]["updated_at"] = occurred_at
        idea = candidate["ideas"][0]
        review = candidate["idea_reviews"][0]
        protection = candidate["correction_protections"][0]
        event = build_state_event(
            event_id="event-review-smuggle",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at=occurred_at,
            actor_ref="actor-owner",
            causation_ref="work:M3-07",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {"collection": "ideas", "object_id": "idea-first", "value": idea},
                {
                    "collection": "idea_reviews",
                    "object_id": review["review_id"],
                    "value": review,
                },
                {
                    "collection": "correction_protections",
                    "object_id": protection["protection_id"],
                    "value": protection,
                },
                {
                    "collection": "claims",
                    "object_id": "claim-active",
                    "value": candidate["claims"][0],
                },
            ],
            project_after=candidate["project"],
            idea_transition={
                "operation": "review-updated",
                "request_sha256": "a" * 64,
                "canonical_idea_id": "idea-first",
                "submitted_idea_id": None,
                "occurrence_id": None,
                "review_id": review["review_id"],
                "protection_id": None,
            },
            schema_version="context.idea-event/v2alpha1",
        )

        with self.assertRaisesRegex(StateEventError, "delta"):
            replay_state_events(snapshot, [event])

    def test_correction_guard_event_cannot_release_an_existing_protection(self):
        snapshot = open_correction_protection(
            self.snapshot_with_idea(),
            protection_id="protection-existing",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Existing active protection.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        occurred_at = "2026-08-14T09:09:00+08:00"
        candidate = copy.deepcopy(snapshot)
        candidate["correction_protections"][0].update(
            {"status": "released", "released_at": occurred_at}
        )
        candidate["claims"][0]["expected_project_revision"] = 10
        candidate["project"]["revision"] = 10
        candidate["project"]["updated_at"] = occurred_at
        event = build_state_event(
            event_id="event-guard-release-smuggle",
            event_type="state-transition",
            project_id=snapshot["project"]["project_id"],
            sequence_no=1,
            revision_before=9,
            occurred_at=occurred_at,
            actor_ref="actor-owner",
            causation_ref="work:M3-07",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "correction_protections",
                    "object_id": "protection-existing",
                    "value": candidate["correction_protections"][0],
                },
                {
                    "collection": "claims",
                    "object_id": "claim-active",
                    "value": candidate["claims"][0],
                },
            ],
            project_after=candidate["project"],
            idea_transition={
                "operation": "correction-guarded",
                "request_sha256": "b" * 64,
                "canonical_idea_id": "idea-first",
                "submitted_idea_id": None,
                "occurrence_id": None,
                "review_id": None,
                "protection_id": "protection-existing",
            },
            schema_version="context.idea-event/v2alpha1",
        )

        with self.assertRaisesRegex(StateEventError, "delta"):
            replay_state_events(snapshot, [event])

    def test_active_correction_protection_denies_only_intersecting_state_commits(self):
        snapshot = open_correction_protection(
            self.snapshot_with_idea(),
            protection_id="protection-active",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Await correction evidence.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        context = RequestContext("actor-owner", "authorization-owner")

        def commit_request(request_id, revision, work):
            return {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": request_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": revision,
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
                "supersedes_event_id": None,
                "changes": [
                    {"collection": "works", "object_id": work["work_id"], "value": work}
                ],
            }

        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="4" * 64,
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            unprotected_work = copy.deepcopy(
                next(work for work in snapshot["works"] if work["work_id"] == "work-target")
            )
            unprotected_work["title"] = "Allowed unrelated update"
            allowed = service.call_tool(
                "context.state.commit",
                commit_request("unprotected-update", 9, unprotected_work),
                context=context,
            )
            current = store.read_project(snapshot["project"]["project_id"])
            protected_work = copy.deepcopy(
                next(work for work in current["works"] if work["work_id"] == "work-active")
            )
            protected_work["title"] = "Forbidden protected update"
            denied = service.call_tool(
                "context.state.commit",
                commit_request("protected-update", 10, protected_work),
                context=context,
            )
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(allowed["ok"], allowed["error"])
        self.assertFalse(denied["ok"])
        self.assertEqual(
            next(work for work in stored["works"] if work["work_id"] == "work-active")[
                "title"
            ],
            "work-active",
        )

    def test_verified_release_reopens_affected_writes_with_durable_provenance(self):
        snapshot = open_correction_protection(
            self.snapshot_with_release_evidence(),
            protection_id="protection-release",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Freeze writes until the correction is independently verified.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        release_request = {
            "schema_version": "context.idea-correction-release-request/v1alpha1",
            "request_id": "release-protection",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-first",
            "protection_id": "protection-release",
            "release_reason": "Current verified evidence resolves the correction.",
            "release_evidence_ids": ["evidence-correction-verified"],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        context = RequestContext("actor-verifier", "authorization-verifier")
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="4" * 64,
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            released = service.call_tool(
                "context.idea.correction.release", release_request, context=context
            )
            after_release = store.read_project(snapshot["project"]["project_id"])
            work = copy.deepcopy(
                next(item for item in after_release["works"] if item["work_id"] == "work-active")
            )
            work["title"] = "Allowed after verified correction release"
            commit = service.call_tool(
                "context.state.commit",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "commit-after-release",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 10,
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                    "supersedes_event_id": None,
                    "changes": [
                        {
                            "collection": "works",
                            "object_id": "work-active",
                            "value": work,
                        }
                    ],
                },
                context=RequestContext("actor-owner", "authorization-owner"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(released["ok"], released["error"])
        protection = stored["correction_protections"][0]
        self.assertEqual(protection["status"], "released")
        self.assertEqual(protection["released_by_ref"], "actor-verifier")
        self.assertEqual(
            protection["release_evidence_ids"], ["evidence-correction-verified"]
        )
        self.assertTrue(commit["ok"], commit["error"])
        self.assertEqual(events[0]["idea_transition"]["operation"], "correction-released")

    def test_release_rejects_missing_unverified_and_reused_evidence(self):
        snapshot = self.snapshot_with_release_evidence()
        snapshot["evidence"].append(
            {
                "evidence_id": "evidence-correction-candidate",
                "kind": "test",
                "artifact_ref": "artifact://m3-07/correction-candidate",
                "content_sha256": "f" * 64,
                "validity": "candidate",
                "observed_at": "2026-08-14T09:08:00+08:00",
                "verified_at": None,
            }
        )
        snapshot = open_correction_protection(
            snapshot,
            protection_id="protection-release-gate",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Require verified evidence.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        for evidence_ids in ([], ["evidence-missing"], ["evidence-correction-candidate"]):
            with self.subTest(evidence_ids=evidence_ids), self.assertRaisesRegex(
                IdeaReviewError, "verified evidence"
            ):
                release_correction_protection(
                    snapshot,
                    protection_id="protection-release-gate",
                    idea_id="idea-first",
                    released_by_ref="actor-verifier",
                    release_reason="Attempt invalid release.",
                    release_evidence_ids=evidence_ids,
                    released_at="2026-08-14T09:10:00+08:00",
                )
        released = release_correction_protection(
            snapshot,
            protection_id="protection-release-gate",
            idea_id="idea-first",
            released_by_ref="actor-verifier",
            release_reason="Verified release.",
            release_evidence_ids=["evidence-correction-verified"],
            released_at="2026-08-14T09:10:00+08:00",
        )
        with self.assertRaisesRegex(IdeaReviewError, "not active"):
            release_correction_protection(
                released,
                protection_id="protection-release-gate",
                idea_id="idea-first",
                released_by_ref="actor-verifier",
                release_reason="Duplicate release.",
                release_evidence_ids=["evidence-correction-verified"],
                released_at="2026-08-14T09:11:00+08:00",
            )

    def test_simultaneous_release_has_one_winner_and_one_explicit_cas_conflict(self):
        class RacingStore(SQLiteStateStore):
            barrier = threading.Barrier(2)

            def commit_event(self, **kwargs):
                self.barrier.wait(timeout=5)
                return super().commit_event(**kwargs)

        snapshot = open_correction_protection(
            self.snapshot_with_release_evidence(),
            protection_id="protection-release-race",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Only one concurrent verifier may release this protection.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            bootstrap = SQLiteStateStore(database)
            bootstrap.initialize()
            bootstrap.create_project(snapshot)

            def release(index):
                return StateMCPService(
                    RacingStore(database),
                    authorizer=_AllowAuthorizer(),
                    registry_digest="4" * 64,
                    clock=lambda: "2026-08-14T09:10:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                ).call_tool(
                    "context.idea.correction.release",
                    {
                        "schema_version": "context.idea-correction-release-request/v1alpha1",
                        "request_id": f"release-race-{index}",
                        "project_id": snapshot["project"]["project_id"],
                        "expected_revision": 9,
                        "idea_id": "idea-first",
                        "protection_id": "protection-release-race",
                        "release_reason": f"Verifier {index} accepted current evidence.",
                        "release_evidence_ids": ["evidence-correction-verified"],
                        "causation_ref": "work:M3-07",
                        "correlation_ref": "campaign:M3",
                    },
                    context=RequestContext(
                        f"actor-verifier-{index}", f"authorization-verifier-{index}"
                    ),
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(release, (1, 2)))
            stored = bootstrap.read_project(snapshot["project"]["project_id"])
            events = bootstrap.read_events(snapshot["project"]["project_id"])

        self.assertEqual(sum(response["ok"] for response in responses), 1)
        loser = next(response for response in responses if not response["ok"])
        self.assertEqual(loser["error"]["code"], "conflict")
        self.assertEqual(stored["correction_protections"][0]["status"], "released")
        self.assertEqual(len(events), 1)

    def test_active_correction_protection_denies_effect_preflight_and_authorization(self):
        snapshot = open_correction_protection(
            self.snapshot_with_idea(),
            protection_id="protection-effect",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Await correction evidence before another side effect.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        context = RequestContext("actor-owner", "authorization-owner")
        common = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "effect_id": "effect-protected",
            "work_id": "work-active",
            "claim_id": "claim-active",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "idea/active"},
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        authorize = {
            **common,
            "request_id": "effect-protected-authorize",
            "action": "authorize",
            "effect_key": "effect-key-protected",
            "result_ref": None,
            "evidence_ids": [],
        }
        preflight = {
            key: value
            for key, value in common.items()
            if key not in {"causation_ref", "correlation_ref"}
        }
        preflight["request_id"] = "effect-protected-preflight"

        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="5" * 64,
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            gate = service.call_tool(
                "context.state.effect.gate", preflight, context=context
            )
            denied = service.call_tool(
                "context.state.effect", authorize, context=context
            )
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(gate["ok"], gate["error"])
        self.assertEqual(gate["result"]["verdict"]["decision"], "deny")
        self.assertEqual(
            gate["result"]["verdict"]["reason"], "correction_protection"
        )
        self.assertFalse(denied["ok"])
        self.assertIn("correction protection", denied["error"]["message"])
        self.assertEqual(stored["effects"], [])

    def test_unprotected_v4_effect_authorization_uses_current_scope_contract(self):
        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-v4-allowed",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "action": "authorize",
            "effect_id": "effect-v4-allowed",
            "effect_key": "effect-key-v4-allowed",
            "work_id": "work-active",
            "claim_id": "claim-active",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "idea/active"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="5" * 64,
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool("context.state.effect", request, context=context)
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(stored["effects"][0]["status"], "authorized")
        self.assertIsNone(stored["effects"][0]["attempt_id"])
        self.assertEqual(events[0]["schema_version"], "context.state-event/v4alpha1")

    def test_protection_freezes_new_effects_but_allows_pending_completion_receipt(self):
        snapshot = self.snapshot_with_idea()
        owner = RequestContext("actor-owner", "authorization-owner")

        def effect_request(*, request_id, revision, action, result_ref=None):
            return {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": request_id,
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": revision,
                "action": action,
                "effect_id": "effect-before-protection",
                "effect_key": "effect-key-before-protection",
                "work_id": "work-active",
                "claim_id": "claim-active",
                "operation": "record-correction",
                "scope_ref": {
                    "scope_kind": "capability",
                    "scope_ref": "idea/active",
                },
                "result_ref": result_ref,
                "evidence_ids": [],
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }

        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="5" * 64,
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            authorized = service.call_tool(
                "context.state.effect",
                effect_request(
                    request_id="effect-before-protection-authorize",
                    revision=9,
                    action="authorize",
                ),
                context=owner,
            )
            protected = service.call_tool(
                "context.idea.correction.protect",
                {
                    "schema_version": "context.idea-correction-protection-request/v1alpha1",
                    "request_id": "protect-with-pending-effect",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 10,
                    "idea_id": "idea-first",
                    "protection_id": "protection-with-pending-effect",
                    "affected_work_ids": ["work-active"],
                    "affected_scope_refs": ["capability:idea/active"],
                    "reason": "Freeze new effects while preserving completion receipts.",
                    "evidence_ids": [],
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                },
                context=owner,
            )
            completed = service.call_tool(
                "context.state.effect",
                effect_request(
                    request_id="effect-before-protection-complete",
                    revision=11,
                    action="complete",
                    result_ref="artifact://m3-07/completed-before-protection",
                ),
                context=owner,
            )
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(authorized["ok"], authorized["error"])
        self.assertTrue(protected["ok"], protected["error"])
        self.assertTrue(completed["ok"], completed["error"])
        self.assertEqual(stored["effects"][0]["status"], "succeeded")

    def test_active_correction_protection_denies_intersecting_route_correction(self):
        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="6" * 64,
                clock=lambda: "2026-08-14T09:05:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            target = copy.deepcopy(
                next(work for work in snapshot["works"] if work["work_id"] == "work-target")
            )
            target["title"] = "Seed target state"
            seed = service.call_tool(
                "context.state.commit",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "seed-target",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 9,
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                    "supersedes_event_id": None,
                    "changes": [
                        {
                            "collection": "works",
                            "object_id": "work-target",
                            "value": target,
                        }
                    ],
                },
                context=context,
            )
            protected = service.call_tool(
                "context.idea.correction.protect",
                {
                    "schema_version": "context.idea-correction-protection-request/v1alpha1",
                    "request_id": "protect-target",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 10,
                    "idea_id": "idea-first",
                    "protection_id": "protection-target",
                    "affected_work_ids": ["work-target"],
                    "affected_scope_refs": ["capability:work-target"],
                    "reason": "Target state needs verified correction.",
                    "evidence_ids": [],
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                },
                context=context,
            )
            current = store.read_project(snapshot["project"]["project_id"])
            active = next(
                work
                for work in current["works"]
                if work["work_id"] == current["project"]["primary_work_id"]
            )
            route_request = {
                "schema_version": "context.task-route-request/v1alpha1",
                "request_id": "route-protected-correction",
                "project_id": current["project"]["project_id"],
                "expected_project_revision": current["project"]["revision"],
                "active_work_id": active["work_id"],
                "expected_active_work_revision": active["revision"],
                "input_ref": "opaque://route/protected-correction",
                "input_sha256": "7" * 64,
                "input_kind": "correction",
                "classifier_confidence_millionths": 1_000_000,
                "classifier_provenance_ref": "artifact://classifier/m3-07",
                "target_work_id": None,
                "user_authorization_candidate": False,
                "authorization_candidate_ref": None,
                "evidence_refs": [],
            }
            decision = route_task_input(route_request, current)
            corrected = copy.deepcopy(
                next(work for work in current["works"] if work["work_id"] == "work-target")
            )
            corrected["title"] = "Forbidden route correction"
            corrected["revision"] += 1
            apply_request = {
                "schema_version": "context.task-route-apply-request/v1alpha1",
                "request_id": route_request["request_id"],
                "project_id": current["project"]["project_id"],
                "proposal_sha256": hashlib.sha256(
                    canonical_route_decision_bytes(decision)
                ).hexdigest(),
                "operation": "correction",
                "expected_project_revision": current["project"]["revision"],
                "expected_active_work_id": active["work_id"],
                "expected_active_work_revision": active["revision"],
                "target_work_id": None,
                "target_work_revision": None,
                "authorization_ref": "authorization-owner",
                "checkpoint_ref": None,
                "checkpoint_binding": None,
                "child_work": None,
                "correction_changes": [
                    {
                        "collection": "works",
                        "object_id": "work-target",
                        "value": corrected,
                    }
                ],
                "supersedes_event_id": "event-seed-target",
                "causation_ref": "work:M3-07",
                "correlation_ref": "campaign:M3",
            }
            self.assertTrue(seed["ok"], seed["error"])
            self.assertTrue(protected["ok"], protected["error"])
            with self.assertRaisesRegex(RouteApplyError, "correction protection"):
                apply_route(
                    store,
                    apply_request,
                    decision=decision,
                    context={
                        "subject_ref": "actor-owner",
                        "authorization_ref": "authorization-owner",
                    },
                    authorizer=_AllowAuthorizer(),
                    clock=lambda: "2026-08-14T09:10:00+08:00",
                    event_id_factory=lambda request_id: f"event-{request_id}",
                )

            self.assertEqual(len(store.read_events(snapshot["project"]["project_id"])), 2)

    def test_unprotected_v4_child_route_commits_a_v4_event(self):
        snapshot = self.snapshot_with_idea()
        active = next(
            work for work in snapshot["works"] if work["work_id"] == "work-active"
        )
        route_request = {
            "schema_version": "context.task-route-request/v1alpha1",
            "request_id": "route-v4-child",
            "project_id": snapshot["project"]["project_id"],
            "expected_project_revision": snapshot["project"]["revision"],
            "active_work_id": active["work_id"],
            "expected_active_work_revision": active["revision"],
            "input_ref": "opaque://route/v4-child",
            "input_sha256": "9" * 64,
            "input_kind": "child_work",
            "classifier_confidence_millionths": 1_000_000,
            "classifier_provenance_ref": "artifact://classifier/m3-07",
            "target_work_id": None,
            "user_authorization_candidate": False,
            "authorization_candidate_ref": None,
            "evidence_refs": [],
        }
        decision = route_task_input(route_request, snapshot)
        child = copy.deepcopy(
            next(work for work in snapshot["works"] if work["work_id"] == "work-target")
        )
        child.update(
            {
                "work_id": "work-v4-child",
                "title": "V4 child proposal",
                "status": "proposed",
                "parent_work_id": "work-active",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "idea/child"}
                ],
            }
        )
        apply_request = {
            "schema_version": "context.task-route-apply-request/v1alpha1",
            "request_id": route_request["request_id"],
            "project_id": snapshot["project"]["project_id"],
            "proposal_sha256": hashlib.sha256(
                canonical_route_decision_bytes(decision)
            ).hexdigest(),
            "operation": "child",
            "expected_project_revision": snapshot["project"]["revision"],
            "expected_active_work_id": active["work_id"],
            "expected_active_work_revision": active["revision"],
            "target_work_id": None,
            "target_work_revision": None,
            "authorization_ref": None,
            "checkpoint_ref": None,
            "checkpoint_binding": None,
            "child_work": child,
            "correction_changes": None,
            "supersedes_event_id": None,
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = apply_route(
                store,
                apply_request,
                decision=decision,
                context={
                    "subject_ref": "actor-owner",
                    "authorization_ref": "authorization-owner",
                },
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-14T09:10:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            events = store.read_events(snapshot["project"]["project_id"])

        self.assertEqual(response["status"], "applied")
        self.assertEqual(events[0]["schema_version"], "context.state-event/v4alpha1")
        self.assertEqual(response["snapshot"]["project"]["primary_work_id"], "work-active")

    def test_dedupe_key_is_stable_for_unicode_case_and_whitespace(self):
        first = compute_idea_dedupe_key(
            parent_work_id="work-active",
            scope_ref="work://work-active",
            summary="  Improve  Context\u00a0Recovery ",
        )
        second = compute_idea_dedupe_key(
            parent_work_id="work-active",
            scope_ref="work://work-active",
            summary="improve context recovery",
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_same_dedupe_key_converges_to_one_idea_and_keeps_occurrences(self):
        snapshot = self.v4_snapshot()
        first = upsert_idea_observation(
            snapshot,
            idea_id="idea-first",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_abcdefghijklmnopqrstuvwxyz",
            summary="A repeated candidate.",
            scope_ref="work://work-active",
            urgency="next",
            review_at=None,
            occurrence_id="occ-first",
            observed_at="2026-08-14T09:01:00+08:00",
        )
        second = upsert_idea_observation(
            first,
            idea_id="idea-different-request",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_bcdefghijklmnopqrstuvwxyza",
            summary=" a repeated   candidate ",
            scope_ref="work://work-active",
            urgency="next",
            review_at=None,
            occurrence_id="occ-second",
            observed_at="2026-08-14T09:02:00+08:00",
        )
        self.assertEqual(len(second["ideas"]), 1)
        self.assertEqual(second["ideas"][0]["idea_id"], "idea-first")
        self.assertEqual(len(second["idea_occurrences"]), 2)
        validate_typed_state(second)

    def test_relationship_rejects_unknown_self_duplicate_and_cycle(self):
        snapshot = self.snapshot_with_idea()
        snapshot = upsert_idea_observation(
            snapshot,
            idea_id="idea-second",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_cdefghijklmnopqrstuvwxyzab",
            summary="Another candidate.",
            scope_ref="work://work-active",
            urgency="later",
            review_at=None,
            occurrence_id="occ-second",
            observed_at="2026-08-14T09:02:00+08:00",
        )
        kwargs = {
            "relationship_id": "rel-first",
            "source_idea_id": "idea-first",
            "target_idea_id": "idea-second",
            "relationship_kind": "depends-on",
            "evidence_ids": [],
            "created_at": "2026-08-14T09:03:00+08:00",
        }
        linked = add_idea_relationship(snapshot, **kwargs)
        with self.assertRaisesRegex(IdeaReviewError, "duplicate"):
            add_idea_relationship(linked, **kwargs)
        with self.assertRaisesRegex(IdeaReviewError, "self"):
            add_idea_relationship(
                linked,
                **{**kwargs, "relationship_id": "rel-self", "target_idea_id": "idea-first"},
            )
        with self.assertRaisesRegex(IdeaReviewError, "cycle"):
            add_idea_relationship(
                linked,
                **{
                    **kwargs,
                    "relationship_id": "rel-cycle",
                    "source_idea_id": "idea-second",
                    "target_idea_id": "idea-first",
                },
            )
        with self.assertRaisesRegex(IdeaReviewError, "unknown"):
            add_idea_relationship(
                snapshot,
                **{**kwargs, "relationship_id": "rel-unknown", "target_idea_id": "missing"},
            )

    def test_occurrences_are_immutable_and_allow_multiple_source_ranges(self):
        snapshot = self.snapshot_with_idea()
        second = append_idea_occurrence(
            snapshot,
            occurrence_id="occ-second",
            idea_id="idea-first",
            source_ref="rng_bcdefghijklmnopqrstuvwxyza",
            observed_at="2026-08-14T09:04:00+08:00",
        )
        self.assertEqual(len(second["idea_occurrences"]), 2)
        with self.assertRaisesRegex(IdeaReviewError, "immutable"):
            append_idea_occurrence(
                second,
                occurrence_id="occ-first",
                idea_id="idea-first",
                source_ref="rng_cdefghijklmnopqrstuvwxyzab",
                observed_at="2026-08-14T09:05:00+08:00",
            )

    def test_review_updates_urgency_without_route_claim_or_effect_authority(self):
        snapshot = self.snapshot_with_idea()
        before = {
            key: copy.deepcopy(snapshot[key])
            for key in ("project", "works", "claims", "effects")
        }
        reviewed = apply_idea_review(
            snapshot,
            idea_id="idea-first",
            review_id="review-first",
            reviewer_ref="reviewer-independent",
            decision="keep",
            urgency="immediate",
            impact="high",
            review_at=None,
            evidence_ids=[],
            reviewed_at="2026-08-14T09:06:00+08:00",
        )
        self.assertEqual(reviewed["ideas"][0]["urgency"], "immediate")
        self.assertEqual(reviewed["idea_reviews"][0]["impact"], "high")
        for key, value in before.items():
            self.assertEqual(reviewed[key], value)

    def test_review_date_requires_a_review_deadline_and_urgency_is_bounded(self):
        snapshot = self.snapshot_with_idea()
        with self.assertRaisesRegex(IdeaReviewError, "review_at"):
            apply_idea_review(
                snapshot,
                idea_id="idea-first",
                review_id="review-date-missing",
                reviewer_ref="reviewer-independent",
                decision="keep",
                urgency="review-date",
                impact="low",
                review_at=None,
                evidence_ids=[],
                reviewed_at="2026-08-14T09:06:00+08:00",
            )
        with self.assertRaisesRegex(IdeaReviewError, "urgency"):
            apply_idea_review(
                snapshot,
                idea_id="idea-first",
                review_id="review-invalid-urgency",
                reviewer_ref="reviewer-independent",
                decision="keep",
                urgency="now",
                impact="low",
                review_at=None,
                evidence_ids=[],
                reviewed_at="2026-08-14T09:06:00+08:00",
            )

    def test_terminal_idea_cannot_be_reopened_by_review(self):
        for status in ("rejected", "superseded", "expired"):
            for decision in ("keep", "park", "reject", "supersede", "approve"):
                with self.subTest(status=status, decision=decision):
                    snapshot = self.snapshot_with_idea()
                    snapshot["ideas"][0]["status"] = status
                    validate_typed_state(snapshot)
                    with self.assertRaisesRegex(IdeaReviewError, "terminal"):
                        apply_idea_review(
                            snapshot,
                            idea_id="idea-first",
                            review_id=f"review-{decision}-{status}",
                            reviewer_ref="reviewer-independent",
                            decision=decision,
                            urgency="next",
                            impact="medium",
                            review_at=None,
                            evidence_ids=[],
                            reviewed_at="2026-08-14T09:06:00+08:00",
                        )

    def test_v2_same_request_identity_rejects_different_payload_after_restart(self):
        snapshot = self.v4_snapshot()
        context = RequestContext("actor-owner", "authorization-owner")
        request = {
            "schema_version": "context.idea-capture-request/v2alpha1",
            "request_id": "capture-restart-conflict",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": "idea-restart-conflict",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_abcdefghijklmnopqrstuvwxyz",
            "summary": "Original durable capture payload.",
            "scope_ref": "work://work-active",
            "urgency": "later",
            "review_at": None,
            "occurrence_id": "occ-restart-conflict",
            "action": "capture-and-continue",
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database)
            store.initialize()
            store.create_project(snapshot)
            first = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="8" * 64,
                clock=lambda: "2026-08-14T09:01:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool("context.idea.capture", request, context=context)
            conflicting = copy.deepcopy(request)
            conflicting["summary"] = "Conflicting payload under the same request identity."
            second = StateMCPService(
                SQLiteStateStore(database),
                authorizer=_AllowAuthorizer(),
                registry_digest="8" * 64,
                clock=lambda: "2026-08-14T09:02:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool("context.idea.capture", conflicting, context=context)
            stored = store.read_project(snapshot["project"]["project_id"])

        self.assertTrue(first["ok"], first["error"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["code"], "conflict")
        self.assertEqual(len(stored["ideas"]), 1)
        self.assertEqual(len(stored["idea_occurrences"]), 1)

    def test_correction_protection_blocks_only_affected_writes(self):
        snapshot = self.snapshot_with_idea()
        protected = open_correction_protection(
            snapshot,
            protection_id="protection-first",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Current evidence may invalidate the Idea assumption.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        self.assertEqual(
            evaluate_correction_write_gate(
                protected,
                [{"collection": "works", "object_id": "work-active"}],
            )["decision"],
            "deny",
        )
        self.assertEqual(
            evaluate_correction_write_gate(
                protected,
                [{"collection": "works", "object_id": "work-target"}],
            )["decision"],
            "allow",
        )

    def test_correction_protection_covers_work_references_and_canonical_scopes(self):
        protected = open_correction_protection(
            self.snapshot_with_idea(),
            protection_id="protection-reference-matrix",
            idea_id="idea-first",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Freeze every write that can mutate protected authority.",
            evidence_ids=[],
            opened_at="2026-08-14T09:07:00+08:00",
        )
        intersecting = (
            {
                "collection": "constraints",
                "object_id": "constraint-bypass",
                "value": {"scope_work_ids": ["work-active"]},
            },
            {
                "collection": "blockers",
                "object_id": "blocker-bypass",
                "value": {"blocked_work_ids": ["work-active"]},
            },
            {
                "collection": "decisions",
                "object_id": "decision-bypass",
                "value": {"work_id": "work-active"},
            },
            {
                "collection": "works",
                "object_id": "work-child",
                "value": {"parent_work_id": "work-active"},
            },
            {
                "collection": "effects",
                "object_id": "effect-scope-bypass",
                "value": {
                    "work_id": "work-target",
                    "scope_ref": {
                        "scope_kind": "capability",
                        "scope_ref": "idea/active",
                    },
                },
            },
        )
        for change in intersecting:
            with self.subTest(collection=change["collection"]):
                self.assertEqual(
                    evaluate_correction_write_gate(protected, [change])["decision"],
                    "deny",
                )

    def test_parked_expired_rejected_and_superseded_ideas_are_not_packet_eligible(self):
        snapshot = self.snapshot_with_idea()
        for index, status in enumerate(("parked", "rejected", "superseded"), start=1):
            snapshot["ideas"].append(
                {
                    **copy.deepcopy(snapshot["ideas"][0]),
                    "idea_id": f"idea-{status}",
                    "status": status,
                    "dedupe_key": f"sha256:{index:064x}",
                }
            )
        snapshot["ideas"][0]["expiry"] = "2026-08-14T08:59:00+08:00"
        snapshot["ideas"][0]["status"] = "expired"
        validate_typed_state(snapshot)
        self.assertEqual(
            packet_eligible_ideas(snapshot, now="2026-08-14T09:10:00+08:00"), []
        )

    def test_v4_schema_and_v2_idea_wires_are_strict_registered_contracts(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load((root / "schemas/registry.yaml").read_text())
        entries = {
            item["schema_id"]: item
            for item in registry["schemas"]
            if item["schema_id"]
            in {
                "context.typed-state",
                "context.idea-event",
                "context.idea-capture",
                "context.idea-review",
                "context.idea-correction-protection",
                "context.idea-correction-release",
                "context.typed-state-migration-receipt",
            }
        }
        self.assertEqual(
            entries["context.typed-state"]["current_wire_version"],
            "context.typed-state/v5alpha1",
        )
        self.assertIn(
            "context.typed-state/v4alpha1",
            entries["context.typed-state"]["supported_wire_versions"],
        )
        self.assertEqual(
            entries["context.idea-event"]["current_wire_version"],
            "context.idea-event/v2alpha1",
        )
        self.assertEqual(
            entries["context.idea-capture"]["current_wire_version"],
            "context.idea-capture-request/v2alpha1",
        )
        self.assertEqual(
            entries["context.idea-review"]["current_wire_version"],
            "context.idea-review-request/v1alpha1",
        )
        self.assertEqual(
            entries["context.idea-correction-protection"]["current_wire_version"],
            "context.idea-correction-protection-request/v1alpha1",
        )
        self.assertEqual(
            entries["context.idea-correction-release"]["current_wire_version"],
            "context.idea-correction-release-request/v1alpha1",
        )
        self.assertEqual(
            entries["context.typed-state-migration-receipt"]["current_wire_version"],
            "context.typed-state-migration-receipt/v1alpha1",
        )
        schemas = {}
        for schema_id, entry in entries.items():
            schema_path = root / entry["artifact_path"]
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(schema_path.read_bytes()).hexdigest()
            )
            schemas[schema_id] = json.loads(schema_path.read_text())
            Draft202012Validator.check_schema(schemas[schema_id])
            self.assertFalse(schemas[schema_id]["additionalProperties"])
        from context_control_plane.durable_state_migration import (
            migrate_typed_state_v4_to_v5,
        )

        Draft202012Validator(schemas["context.typed-state"]).validate(
            migrate_typed_state_v4_to_v5(self.v4_snapshot())
        )

    def test_persisted_v2_event_resolves_and_validates_against_v4_schema(self):
        root = Path(__file__).parents[1]
        snapshot = self.snapshot_with_idea()
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T09:09:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.idea.review",
                {
                    "schema_version": "context.idea-review-request/v1alpha1",
                    "request_id": "review-schema-resolution",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 9,
                    "idea_id": "idea-first",
                    "review_id": "review-schema-resolution",
                    "decision": "keep",
                    "urgency": "next",
                    "impact": "medium",
                    "review_at": None,
                    "evidence_ids": [],
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                },
                context=RequestContext("actor-owner", "authorization-owner"),
            )

        self.assertTrue(response["ok"], response["error"])
        event_schema = json.loads(
            (root / "schemas/m3-07/idea-event-v2alpha1.schema.json").read_text()
        )
        state_schema = json.loads(
            (root / "schemas/m3-07/typed-state-v4alpha1.schema.json").read_text()
        )
        registry = Registry().with_resource(
            state_schema["$id"], Resource.from_contents(state_schema)
        )
        Draft202012Validator(event_schema, registry=registry).validate(
            response["result"]["event"]
        )

    def test_persisted_v2_review_event_with_pending_effect_validates_against_schema(self):
        root = Path(__file__).parents[1]
        snapshot = self.snapshot_with_idea()
        context = RequestContext("actor-owner", "authorization-owner")
        effect_request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "review-pending-effect-authorize",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "action": "authorize",
            "effect_id": "review-pending-effect",
            "effect_key": "review-pending-effect-key",
            "work_id": "work-active",
            "claim_id": "claim-active",
            "operation": "record-correction",
            "scope_ref": {"scope_kind": "capability", "scope_ref": "idea/active"},
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        review_request = {
            "schema_version": "context.idea-review-request/v1alpha1",
            "request_id": "review-with-pending-effect",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 10,
            "idea_id": "idea-first",
            "review_id": "review-with-pending-effect",
            "decision": "keep",
            "urgency": "next",
            "impact": "medium",
            "review_at": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-07",
            "correlation_ref": "campaign:M3",
        }
        with TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T09:09:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            authorized = service.call_tool(
                "context.state.effect", effect_request, context=context
            )
            reviewed = service.call_tool(
                "context.idea.review", review_request, context=context
            )

        self.assertTrue(authorized["ok"], authorized["error"])
        self.assertTrue(reviewed["ok"], reviewed["error"])
        event_schema = json.loads(
            (root / "schemas/m3-07/idea-event-v2alpha1.schema.json").read_text()
        )
        state_schema = json.loads(
            (root / "schemas/m3-07/typed-state-v4alpha1.schema.json").read_text()
        )
        registry = Registry().with_resource(
            state_schema["$id"], Resource.from_contents(state_schema)
        )
        Draft202012Validator(event_schema, registry=registry).validate(
            reviewed["result"]["event"]
        )


if __name__ == "__main__":
    unittest.main()
