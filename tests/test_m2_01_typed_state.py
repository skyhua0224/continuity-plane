import copy
import json
import unittest
from pathlib import Path

import yaml

from context_control_plane.typed_state import (
    TypedStateError,
    canonical_state_bytes,
    round_trip_typed_state,
    validate_typed_state,
)


class M201TypedStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture_path = cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml"
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cls.cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}

    def test_schema_is_versioned_registered_and_has_all_core_objects(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in registry["schemas"] if item["schema_id"] == "context.typed-state"
        )
        schema = json.loads((self.root / entry["artifact_path"]).read_text(encoding="utf-8"))

        self.assertEqual(entry["current_wire_version"], "context.typed-state/v2alpha1")
        self.assertIn("context.typed-state/v1alpha1", entry["supported_wire_versions"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], entry["current_wire_version"])
        self.assertFalse(schema["additionalProperties"])
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
        ):
            with self.subTest(object_name=object_name):
                self.assertIn(object_name, schema["$defs"])
                definition = schema["$defs"][object_name]
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(
                    set(definition["required"]),
                    set(definition["properties"]),
                )

    def test_all_fixture_states_round_trip_canonically(self):
        self.assertEqual(
            set(self.cases),
            {
                "solo-active-work",
                "multi-worker-disjoint-scopes",
                "completed-work-overlap-blocked",
                "superseded-decision-and-constraint",
            },
        )

        for case_id, document in self.cases.items():
            with self.subTest(case_id=case_id):
                validate_typed_state(document)
                encoded = canonical_state_bytes(document)
                restored = round_trip_typed_state(document)
                self.assertEqual(restored, document)
                self.assertEqual(canonical_state_bytes(restored), encoded)

    def test_active_work_projection_must_match_work_status(self):
        broken = copy.deepcopy(self.cases["solo-active-work"])
        broken["project"]["active_work_ids"] = []

        with self.assertRaisesRegex(TypedStateError, "active_work_ids"):
            validate_typed_state(broken)

    def test_reverted_decision_cannot_be_current(self):
        broken = copy.deepcopy(self.cases["superseded-decision-and-constraint"])
        broken["project"]["current_decision_ids"].append("decision-old")

        with self.assertRaisesRegex(TypedStateError, "current_decision_ids"):
            validate_typed_state(broken)

    def test_supersedes_graph_must_be_acyclic(self):
        broken = copy.deepcopy(self.cases["superseded-decision-and-constraint"])
        old = next(
            item for item in broken["decisions"] if item["decision_id"] == "decision-old"
        )
        old["supersedes_decision_id"] = "decision-current"

        with self.assertRaisesRegex(TypedStateError, "decision supersedes cycle"):
            validate_typed_state(broken)

    def test_uncoordinated_overlap_cannot_become_active(self):
        broken = copy.deepcopy(self.cases["completed-work-overlap-blocked"])
        repeated = next(item for item in broken["works"] if item["work_id"] == "work-repeat")
        repeated["status"] = "active"
        broken["project"]["active_work_ids"] = ["work-repeat"]
        broken["project"]["primary_work_id"] = "work-repeat"

        with self.assertRaisesRegex(TypedStateError, "dedupe"):
            validate_typed_state(broken)

    def test_active_claims_cannot_own_the_same_scope(self):
        broken = copy.deepcopy(self.cases["multi-worker-disjoint-scopes"])
        broken["works"][1]["scope_refs"] = copy.deepcopy(
            broken["works"][0]["scope_refs"]
        )
        broken["claims"][1]["scope_owners"] = copy.deepcopy(
            broken["claims"][0]["scope_owners"]
        )

        with self.assertRaisesRegex(TypedStateError, "scope ownership conflict"):
            validate_typed_state(broken)

    def test_claim_scope_must_be_declared_by_its_work(self):
        broken = copy.deepcopy(self.cases["solo-active-work"])
        broken["claims"][0]["scope_owners"] = [
            {"scope_kind": "file", "scope_ref": "src/unrelated.py"}
        ]

        with self.assertRaisesRegex(TypedStateError, "scope must belong to its work"):
            validate_typed_state(broken)

    def test_active_claim_lease_must_cover_the_snapshot_time(self):
        broken = copy.deepcopy(self.cases["solo-active-work"])
        broken["claims"][0]["lease_expires_at"] = "2026-08-09T09:14:00+08:00"

        with self.assertRaisesRegex(TypedStateError, "active claim lease has expired"):
            validate_typed_state(broken)

    def test_authorized_effect_requires_a_current_active_claim(self):
        broken = copy.deepcopy(self.cases["solo-active-work"])
        broken["claims"][0]["expected_project_revision"] -= 1

        with self.assertRaisesRegex(TypedStateError, "expected project revision"):
            validate_typed_state(broken)

    def test_unknown_fields_are_rejected(self):
        broken = copy.deepcopy(self.cases["solo-active-work"])
        broken["provider_session_id"] = "provider-local-session"

        with self.assertRaisesRegex(TypedStateError, "document fields"):
            validate_typed_state(broken)

    def test_completed_work_requires_verified_evidence(self):
        broken = copy.deepcopy(self.cases["completed-work-overlap-blocked"])
        evidence = next(item for item in broken["evidence"] if item["evidence_id"] == "evidence-done")
        evidence["validity"] = "candidate"
        evidence["verified_at"] = None

        with self.assertRaisesRegex(TypedStateError, "completed work requires verified evidence"):
            validate_typed_state(broken)


if __name__ == "__main__":
    unittest.main()
