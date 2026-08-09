"""Reusable authoritative StateStore conformance tests."""

from __future__ import annotations

import copy
from typing import Any

from context_control_plane.state_store import (
    StateStoreConflict,
    StateStoreIntegrityError,
    StateStoreNotFound,
    invoke_state_store,
    validate_state_store_adapter,
)
from context_control_plane.typed_state import canonical_state_bytes


class AuthoritativeStateStoreConformanceMixin:
    """Behavior required from snapshot/Event authority adapters."""

    def make_store(self) -> Any:
        raise NotImplementedError

    def make_initial_snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def make_candidate(
        self,
        initial: dict[str, Any],
        suffix: str,
        *,
        sequence_no: int = 1,
        previous_event_sha256: str | None = None,
        event_id: str | None = None,
        supersedes_event_id: str | None = None,
        event_type: str = "state-transition",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError

    def test_conformance_create_read_and_defensive_copy(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        expected = copy.deepcopy(initial)

        validate_state_store_adapter(store)
        invoke_state_store(store, "create_project", initial)
        initial["project"]["governance_ref"] = "artifact://mutated-input"
        restored = invoke_state_store(
            store,
            "read_project",
            expected["project"]["project_id"],
        )
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(expected))

        restored["project"]["governance_ref"] = "artifact://mutated-output"
        reread = invoke_state_store(
            store,
            "read_project",
            expected["project"]["project_id"],
        )
        self.assertEqual(canonical_state_bytes(reread), canonical_state_bytes(expected))

    def test_conformance_duplicate_create_and_unknown_read_use_generic_errors(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        invoke_state_store(store, "create_project", initial)

        with self.assertRaises(StateStoreConflict):
            invoke_state_store(store, "create_project", initial)
        with self.assertRaises(StateStoreNotFound):
            invoke_state_store(store, "read_project", "project-does-not-exist")

    def test_conformance_invalid_create_is_integrity_error_and_does_not_create(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        project_id = initial["project"]["project_id"]
        initial["unexpected"] = True

        with self.assertRaises(StateStoreIntegrityError):
            invoke_state_store(store, "create_project", initial)

        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])
        with self.assertRaises(StateStoreNotFound):
            invoke_state_store(store, "read_project", project_id)

    def test_conformance_unknown_project_has_an_empty_event_stream(self):
        store = self.make_store()

        events = invoke_state_store(
            store,
            "read_events",
            "project-does-not-exist",
        )

        self.assertEqual(events, [])

    def test_conformance_commit_persists_replay_equivalent_state_and_event(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "commit")
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=event,
            expected_snapshot=expected,
        )

        restored = invoke_state_store(store, "read_project", project_id)
        events = invoke_state_store(store, "read_events", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(expected))
        self.assertEqual(events, [event])

    def test_conformance_event_input_and_output_are_defensive_copies(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "event-copy")
        durable_event = copy.deepcopy(event)
        durable_expected = copy.deepcopy(expected)
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=event,
            expected_snapshot=expected,
        )
        event["actor_ref"] = "actor-mutated-input"
        expected["project"]["governance_ref"] = "artifact://mutated-snapshot-input"

        events = invoke_state_store(store, "read_events", project_id)
        self.assertEqual(events, [durable_event])
        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(
            canonical_state_bytes(restored),
            canonical_state_bytes(durable_expected),
        )
        events[0]["actor_ref"] = "actor-mutated-output"

        reread = invoke_state_store(store, "read_events", project_id)
        self.assertEqual(reread, [durable_event])

    def test_conformance_unknown_commit_is_not_found_and_does_not_create(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "unknown-commit")
        project_id = initial["project"]["project_id"]

        with self.assertRaises(StateStoreNotFound):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=expected,
            )

        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])
        with self.assertRaises(StateStoreNotFound):
            invoke_state_store(store, "read_project", project_id)

    def test_conformance_replay_mismatch_rolls_back_snapshot_and_event(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "replay-mismatch")
        mismatched = copy.deepcopy(expected)
        mismatched["project"]["governance_ref"] = "artifact://replay-mismatch"
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        with self.assertRaises(StateStoreIntegrityError):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=mismatched,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(initial))
        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])

    def test_conformance_duplicate_event_identity_rolls_back(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "identity-first")
        duplicate, after_duplicate = self.make_candidate(
            after_first,
            "identity-duplicate",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
            event_id=first["event_id"],
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        with self.assertRaises(StateStoreConflict):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=after_first["project"]["revision"],
                event=duplicate,
                expected_snapshot=after_duplicate,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(
            canonical_state_bytes(restored),
            canonical_state_bytes(after_first),
        )
        self.assertEqual(invoke_state_store(store, "read_events", project_id), [first])

    def test_conformance_event_sequence_conflict_is_atomic(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "sequence-first")
        out_of_order, after_out_of_order = self.make_candidate(
            after_first,
            "sequence-gap",
            sequence_no=3,
            previous_event_sha256=first["event_sha256"],
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        with self.assertRaises(StateStoreConflict):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=after_first["project"]["revision"],
                event=out_of_order,
                expected_snapshot=after_out_of_order,
            )

        self.assertEqual(invoke_state_store(store, "read_events", project_id), [first])
        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(after_first))

    def test_conformance_event_head_conflict_is_atomic(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "head-first")
        wrong_head, after_wrong_head = self.make_candidate(
            after_first,
            "head-conflict",
            sequence_no=2,
            previous_event_sha256="0" * 64,
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        with self.assertRaises(StateStoreConflict):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=after_first["project"]["revision"],
                event=wrong_head,
                expected_snapshot=after_wrong_head,
            )

        self.assertEqual(invoke_state_store(store, "read_events", project_id), [first])
        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(after_first))

    def test_conformance_second_event_advances_the_chain(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "chain-first")
        second, after_second = self.make_candidate(
            after_first,
            "chain-second",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=after_first["project"]["revision"],
            event=second,
            expected_snapshot=after_second,
        )

        self.assertEqual(
            invoke_state_store(store, "read_events", project_id),
            [first, second],
        )
        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(after_second))

    def test_conformance_second_event_can_supersede_a_persisted_event(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "supersedes-first")
        second, after_second = self.make_candidate(
            after_first,
            "supersedes-second",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
            supersedes_event_id=first["event_id"],
            event_type="correction",
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=after_first["project"]["revision"],
            event=second,
            expected_snapshot=after_second,
        )

        self.assertEqual(
            invoke_state_store(store, "read_events", project_id),
            [first, second],
        )

    def test_conformance_unknown_supersedes_event_is_integrity_error_and_atomic(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(
            initial,
            "unknown-supersedes",
            supersedes_event_id="event-does-not-exist",
            event_type="correction",
        )
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        with self.assertRaises(StateStoreIntegrityError):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=expected,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(initial))
        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])

    def test_conformance_invalid_event_is_integrity_error_and_atomic(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "invalid-event")
        event["actor_ref"] = "actor-tampered-without-rehash"
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        with self.assertRaises(StateStoreIntegrityError):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=expected,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(initial))
        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])

    def test_conformance_invalid_snapshot_is_integrity_error_and_atomic(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "invalid-snapshot")
        expected["unexpected"] = True
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)

        with self.assertRaises(StateStoreIntegrityError):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=expected,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(initial))
        self.assertEqual(invoke_state_store(store, "read_events", project_id), [])

    def test_conformance_stale_commit_is_atomic_generic_conflict(self):
        store = self.make_store()
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "winner")
        stale, stale_expected = self.make_candidate(initial, "stale")
        project_id = initial["project"]["project_id"]
        invoke_state_store(store, "create_project", initial)
        invoke_state_store(
            store,
            "commit_event",
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )

        with self.assertRaises(StateStoreConflict):
            invoke_state_store(
                store,
                "commit_event",
                project_id=project_id,
                expected_revision=initial["project"]["revision"],
                event=stale,
                expected_snapshot=stale_expected,
            )

        restored = invoke_state_store(store, "read_project", project_id)
        events = invoke_state_store(store, "read_events", project_id)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(after_first))
        self.assertEqual(events, [first])
