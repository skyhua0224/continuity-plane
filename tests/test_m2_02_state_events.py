import copy
import json
import unittest
from pathlib import Path

import yaml

from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    canonical_event_bytes,
    replay_state_events,
    validate_state_event,
)
from context_control_plane.typed_state import canonical_state_bytes, validate_typed_state


class M202StateEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture_path = cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml"
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}
        cls.initial = copy.deepcopy(cases["completed-work-overlap-blocked"])

    def setUp(self):
        self.after_first = copy.deepcopy(self.initial)
        self.after_first["project"]["revision"] = 15
        self.after_first["project"]["current_decision_ids"] = ["decision-old"]
        self.after_first["project"]["updated_at"] = "2026-08-10T00:01:00+08:00"
        decision_old = {
            "decision_id": "decision-old",
            "work_id": "work-existing",
            "status": "accepted",
            "statement": "Use the first event interpretation.",
            "decided_at": "2026-08-10T00:00:30+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": ["evidence-done"],
        }
        self.after_first["decisions"] = [decision_old]
        self.first = build_state_event(
            event_id="event-decision-old",
            event_type="state-transition",
            project_id=self.initial["project"]["project_id"],
            sequence_no=1,
            revision_before=14,
            occurred_at="2026-08-10T00:01:00+08:00",
            actor_ref="actor-first",
            causation_ref="work:work-existing",
            correlation_ref="replay:m2-02",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "decisions",
                    "object_id": "decision-old",
                    "value": decision_old,
                }
            ],
            project_after=self.after_first["project"],
        )

        self.expected = copy.deepcopy(self.after_first)
        self.expected["project"]["revision"] = 16
        self.expected["project"]["current_decision_ids"] = ["decision-current"]
        self.expected["project"]["updated_at"] = "2026-08-10T00:02:00+08:00"
        reverted = copy.deepcopy(decision_old)
        reverted["status"] = "reverted"
        decision_current = {
            "decision_id": "decision-current",
            "work_id": "work-existing",
            "status": "accepted",
            "statement": "Use deterministic event replay.",
            "decided_at": "2026-08-10T00:01:30+08:00",
            "supersedes_decision_id": "decision-old",
            "evidence_ids": ["evidence-done"],
        }
        self.expected["decisions"] = [reverted, decision_current]
        self.second = build_state_event(
            event_id="event-decision-current",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=2,
            revision_before=15,
            occurred_at="2026-08-10T00:02:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:decision-old",
            correlation_ref="replay:m2-02",
            previous_event_sha256=self.first["event_sha256"],
            supersedes_event_id=self.first["event_id"],
            changes=[
                {
                    "collection": "decisions",
                    "object_id": "decision-old",
                    "value": reverted,
                },
                {
                    "collection": "decisions",
                    "object_id": "decision-current",
                    "value": decision_current,
                },
            ],
            project_after=self.expected["project"],
        )

    def test_event_schema_is_registered_and_strict(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in registry["schemas"] if item["schema_id"] == "context.state-event"
        )
        schema = json.loads((self.root / entry["artifact_path"]).read_text(encoding="utf-8"))

        self.assertEqual(entry["current_wire_version"], "context.state-event/v4alpha1")
        self.assertIn("context.state-event/v1alpha1", entry["supported_wire_versions"])
        self.assertIn("context.state-event/v2alpha1", entry["supported_wire_versions"])
        self.assertIn("context.state-event/v3alpha1", entry["supported_wire_versions"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], entry["current_wire_version"])
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["change"]["additionalProperties"])

    def test_versioned_replay_fixtures_are_byte_equivalent(self):
        fixture_path = self.root / "experiments" / "state" / "m2-02-replay-fixtures.yaml"
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(
            fixture_set["schema_version"],
            "context.state-event-fixtures/v1alpha1",
        )
        self.assertEqual(
            {case["case_id"] for case in fixture_set["cases"]},
            {"correction-supersedes-byte-equivalent"},
        )
        for case in fixture_set["cases"]:
            with self.subTest(case_id=case["case_id"]):
                validate_typed_state(case["initial_snapshot"])
                validate_typed_state(case["expected_snapshot"])
                for event in case["events"]:
                    validate_state_event(event)
                restored = replay_state_events(
                    case["initial_snapshot"],
                    case["events"],
                )
                self.assertEqual(
                    canonical_state_bytes(restored),
                    canonical_state_bytes(case["expected_snapshot"]),
                )

    def test_replay_is_byte_equivalent_to_expected_snapshot(self):
        restored = replay_state_events(self.initial, [self.first, self.second])

        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(self.expected))

    def test_event_hash_and_chain_are_deterministic(self):
        rebuilt = build_state_event(
            event_id="event-decision-old",
            event_type="state-transition",
            project_id=self.initial["project"]["project_id"],
            sequence_no=1,
            revision_before=14,
            occurred_at="2026-08-10T00:01:00+08:00",
            actor_ref="actor-first",
            causation_ref="work:work-existing",
            correlation_ref="replay:m2-02",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=copy.deepcopy(self.first["changes"]),
            project_after=copy.deepcopy(self.after_first["project"]),
        )

        self.assertEqual(canonical_event_bytes(rebuilt), canonical_event_bytes(self.first))
        self.assertEqual(len(self.first["event_sha256"]), 64)
        self.assertEqual(self.second["previous_event_sha256"], self.first["event_sha256"])

    def test_tampered_payload_is_rejected_by_event_hash(self):
        tampered = copy.deepcopy(self.first)
        tampered["changes"][0]["value"]["statement"] = "Tampered interpretation."

        with self.assertRaisesRegex(StateEventError, "event_sha256"):
            replay_state_events(self.initial, [tampered])

    def test_out_of_order_sequence_is_rejected(self):
        out_of_order = build_state_event(
            event_id="event-out-of-order",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=3,
            revision_before=15,
            occurred_at="2026-08-10T00:02:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:decision-old",
            correlation_ref="replay:m2-02",
            previous_event_sha256=self.first["event_sha256"],
            supersedes_event_id=self.first["event_id"],
            changes=copy.deepcopy(self.second["changes"]),
            project_after=copy.deepcopy(self.expected["project"]),
        )

        with self.assertRaisesRegex(StateEventError, "sequence"):
            replay_state_events(self.initial, [self.first, out_of_order])

    def test_revision_gap_is_rejected(self):
        project_after_gap = copy.deepcopy(self.expected["project"])
        project_after_gap["revision"] = 17
        revision_gap = build_state_event(
            event_id="event-revision-gap",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=2,
            revision_before=16,
            occurred_at="2026-08-10T00:02:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:decision-old",
            correlation_ref="replay:m2-02",
            previous_event_sha256=self.first["event_sha256"],
            supersedes_event_id=self.first["event_id"],
            changes=copy.deepcopy(self.second["changes"]),
            project_after=project_after_gap,
        )

        with self.assertRaisesRegex(StateEventError, "revision"):
            replay_state_events(self.initial, [self.first, revision_gap])

    def test_broken_event_hash_chain_is_rejected(self):
        broken_chain = build_state_event(
            event_id="event-broken-chain",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=2,
            revision_before=15,
            occurred_at="2026-08-10T00:02:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:decision-old",
            correlation_ref="replay:m2-02",
            previous_event_sha256="f" * 64,
            supersedes_event_id=self.first["event_id"],
            changes=copy.deepcopy(self.second["changes"]),
            project_after=copy.deepcopy(self.expected["project"]),
        )

        with self.assertRaisesRegex(StateEventError, "hash chain"):
            replay_state_events(self.initial, [self.first, broken_chain])

    def test_unknown_supersedes_event_is_rejected(self):
        unknown_supersedes = build_state_event(
            event_id="event-unknown-supersedes",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=1,
            revision_before=14,
            occurred_at="2026-08-10T00:01:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:missing-event",
            correlation_ref="replay:m2-02",
            previous_event_sha256=None,
            supersedes_event_id="event-missing",
            changes=copy.deepcopy(self.first["changes"]),
            project_after=copy.deepcopy(self.after_first["project"]),
        )

        with self.assertRaisesRegex(StateEventError, "earlier event"):
            replay_state_events(self.initial, [unknown_supersedes])

    def test_replay_does_not_mutate_snapshot_or_events(self):
        initial = copy.deepcopy(self.initial)
        events = copy.deepcopy([self.first, self.second])
        initial_before = copy.deepcopy(initial)
        events_before = copy.deepcopy(events)

        restored = replay_state_events(initial, events)

        self.assertEqual(initial, initial_before)
        self.assertEqual(events, events_before)
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(self.expected))

    def test_duplicate_event_id_is_rejected(self):
        duplicate = build_state_event(
            event_id=self.first["event_id"],
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=2,
            revision_before=15,
            occurred_at="2026-08-10T00:02:00+08:00",
            actor_ref="actor-verifier",
            causation_ref="correction:decision-old",
            correlation_ref="replay:m2-02",
            previous_event_sha256=self.first["event_sha256"],
            supersedes_event_id=self.first["event_id"],
            changes=copy.deepcopy(self.second["changes"]),
            project_after=copy.deepcopy(self.expected["project"]),
        )

        with self.assertRaisesRegex(StateEventError, "event_id"):
            replay_state_events(self.initial, [self.first, duplicate])

    def test_unknown_event_version_is_rejected(self):
        unknown = copy.deepcopy(self.first)
        unknown["schema_version"] = "context.state-event/v9"

        with self.assertRaisesRegex(StateEventError, "schema_version"):
            replay_state_events(self.initial, [unknown])

    def test_reverted_decision_cannot_be_revived(self):
        revived = copy.deepcopy(self.expected)
        revived["project"]["revision"] = 17
        revived["project"]["current_decision_ids"] = ["decision-old"]
        revived["project"]["updated_at"] = "2026-08-10T00:03:00+08:00"
        old = next(item for item in revived["decisions"] if item["decision_id"] == "decision-old")
        current = next(
            item for item in revived["decisions"] if item["decision_id"] == "decision-current"
        )
        old["status"] = "accepted"
        current["status"] = "reverted"
        revival = build_state_event(
            event_id="event-revive-old-decision",
            event_type="correction",
            project_id=self.initial["project"]["project_id"],
            sequence_no=3,
            revision_before=16,
            occurred_at="2026-08-10T00:03:00+08:00",
            actor_ref="actor-stale",
            causation_ref="memory:stale-decision",
            correlation_ref="replay:m2-02",
            previous_event_sha256=self.second["event_sha256"],
            supersedes_event_id=self.second["event_id"],
            changes=[
                {"collection": "decisions", "object_id": "decision-old", "value": old},
                {
                    "collection": "decisions",
                    "object_id": "decision-current",
                    "value": current,
                },
            ],
            project_after=revived["project"],
        )

        with self.assertRaisesRegex(StateEventError, "terminal Decision"):
            replay_state_events(self.initial, [self.first, self.second, revival])

    def test_completed_work_cannot_be_reopened(self):
        reopened = copy.deepcopy(self.initial)
        reopened["project"]["revision"] = 15
        reopened["project"]["updated_at"] = "2026-08-10T00:01:00+08:00"
        work = next(item for item in reopened["works"] if item["work_id"] == "work-existing")
        work["status"] = "proposed"
        event = build_state_event(
            event_id="event-reopen-completed-work",
            event_type="state-transition",
            project_id=self.initial["project"]["project_id"],
            sequence_no=1,
            revision_before=14,
            occurred_at="2026-08-10T00:01:00+08:00",
            actor_ref="actor-stale",
            causation_ref="memory:stale-work",
            correlation_ref="replay:m2-02-work",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {"collection": "works", "object_id": "work-existing", "value": work}
            ],
            project_after=reopened["project"],
        )

        with self.assertRaisesRegex(StateEventError, "terminal Work"):
            replay_state_events(self.initial, [event])


if __name__ == "__main__":
    unittest.main()
