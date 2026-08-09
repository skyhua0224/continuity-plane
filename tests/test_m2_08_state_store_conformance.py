import copy
import os
import unittest
import uuid
from pathlib import Path

import yaml

from context_control_plane.postgres_state_store import PostgresStateStore
from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
    validate_state_event,
)
from context_control_plane.state_store import (
    StateStoreCapabilityManifest,
    StateStoreConflict,
    StateStoreIntegrityError,
    StateStoreNotFound,
)
from context_control_plane.testing.state_store_conformance import (
    AuthoritativeStateStoreConformanceMixin,
)
from context_control_plane.typed_state import (
    TypedStateError,
    canonical_state_bytes,
    validate_typed_state,
)


class _FixtureMixin:
    @classmethod
    def setUpClass(cls):
        fixture_path = (
            Path(__file__).parents[1]
            / "experiments"
            / "state"
            / "m2-01-core-fixtures.yaml"
        )
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}
        cls.base_initial = cases["completed-work-overlap-blocked"]

    def make_initial_snapshot(self):
        snapshot = copy.deepcopy(self.base_initial)
        snapshot["project"]["project_id"] = f"project-conformance-{uuid.uuid4().hex}"
        return snapshot

    def make_candidate(
        self,
        initial,
        suffix,
        *,
        sequence_no=1,
        previous_event_sha256=None,
        event_id=None,
        supersedes_event_id=None,
        event_type="state-transition",
    ):
        expected = copy.deepcopy(initial)
        expected["project"]["revision"] = initial["project"]["revision"] + 1
        expected["project"]["updated_at"] = "2026-08-10T02:00:00+08:00"
        idea = {
            "idea_id": f"idea-{suffix}-{uuid.uuid4().hex}",
            "parent_work_id": "work-repeat",
            "source_ref": f"opaque://conformance/{suffix}",
            "summary": f"Conformance candidate {suffix}.",
            "status": "parked",
            "return_work_id": "work-repeat",
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": "M2-09",
            "evidence_ids": [],
        }
        expected["ideas"].append(idea)
        event = build_state_event(
            event_id=event_id or f"event-{suffix}-{uuid.uuid4().hex}",
            event_type=event_type,
            project_id=initial["project"]["project_id"],
            sequence_no=sequence_no,
            revision_before=initial["project"]["revision"],
            occurred_at="2026-08-10T02:00:00+08:00",
            actor_ref="actor-conformance",
            causation_ref=f"work:{suffix}",
            correlation_ref="conformance:m2-08",
            previous_event_sha256=previous_event_sha256,
            supersedes_event_id=supersedes_event_id,
            changes=[
                {
                    "collection": "ideas",
                    "object_id": idea["idea_id"],
                    "value": idea,
                }
            ],
            project_after=expected["project"],
        )
        return event, expected


class _MemoryStateStore:
    capability_manifest = StateStoreCapabilityManifest(
        schema_version="context.state-store-capabilities/v1alpha1",
        adapter_id="context.test-memory",
        adapter_version="1.0.0-alpha.1",
        authority_mode="local",
        operations=("create_project", "read_project", "read_events", "commit_event"),
        shared_authority=False,
        offline_write=True,
        unique_claim=False,
        multi_writer=False,
        lease_clock="none",
        artifact_scope="none",
        expected_revision=True,
        migration_source=False,
        migration_target=False,
    )

    def __init__(self):
        self._snapshots = {}
        self._events = {}

    def initialize(self):
        pass

    def create_project(self, snapshot):
        snapshot = copy.deepcopy(snapshot)
        try:
            validate_typed_state(snapshot)
        except TypedStateError as exc:
            raise StateStoreIntegrityError("typed state validation failed") from exc
        project_id = snapshot["project"]["project_id"]
        if project_id in self._snapshots:
            raise StateStoreConflict(f"project already exists: {project_id}")
        self._snapshots[project_id] = snapshot
        self._events[project_id] = []

    def read_project(self, project_id):
        if project_id not in self._snapshots:
            raise StateStoreNotFound(f"project does not exist: {project_id}")
        return copy.deepcopy(self._snapshots[project_id])

    def read_events(self, project_id):
        return copy.deepcopy(self._events.get(project_id, []))

    def commit_event(
        self,
        *,
        project_id,
        expected_revision,
        event,
        expected_snapshot,
    ):
        if project_id not in self._snapshots:
            raise StateStoreNotFound(f"project does not exist: {project_id}")
        current = self._snapshots[project_id]
        if current["project"]["revision"] != expected_revision:
            raise StateStoreConflict("expected revision is stale")
        event = copy.deepcopy(event)
        expected_snapshot = copy.deepcopy(expected_snapshot)
        try:
            validate_state_event(event)
            validate_typed_state(expected_snapshot)
        except (StateEventError, TypedStateError) as exc:
            raise StateStoreIntegrityError("candidate validation failed") from exc
        existing = self._events[project_id]
        if any(item["event_id"] == event["event_id"] for item in existing):
            raise StateStoreConflict("event identity already exists")
        previous_hash = existing[-1]["event_sha256"] if existing else None
        sequence = len(existing) + 1
        if event["sequence_no"] != sequence:
            raise StateStoreConflict("event sequence is stale")
        if event["previous_event_sha256"] != previous_hash:
            raise StateStoreConflict("event head is stale")
        try:
            restored = replay_state_events(
                current,
                [event],
                starting_sequence_no=sequence,
                previous_event_sha256=previous_hash,
                known_event_ids={item["event_id"] for item in existing},
            )
        except (StateEventError, TypedStateError) as exc:
            raise StateStoreIntegrityError("state Event replay failed") from exc
        if canonical_state_bytes(restored) != canonical_state_bytes(expected_snapshot):
            raise StateStoreIntegrityError("event replay mismatch")
        self._events[project_id].append(event)
        self._snapshots[project_id] = expected_snapshot


class M208MemoryStateStoreConformanceTests(
    _FixtureMixin,
    AuthoritativeStateStoreConformanceMixin,
    unittest.TestCase,
):
    def make_store(self):
        store = _MemoryStateStore()
        store.initialize()
        return store


@unittest.skipUnless(
    os.environ.get("CONTEXT_TEST_POSTGRES_DSN"),
    "CONTEXT_TEST_POSTGRES_DSN is required for PostgreSQL conformance tests",
)
class M208PostgresStateStoreConformanceTests(
    _FixtureMixin,
    AuthoritativeStateStoreConformanceMixin,
    unittest.TestCase,
):
    def make_store(self):
        store = PostgresStateStore(os.environ["CONTEXT_TEST_POSTGRES_DSN"])
        store.initialize()
        return store


if __name__ == "__main__":
    unittest.main()
