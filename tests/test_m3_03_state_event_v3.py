import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from context_control_plane.state_events import (
    EVENT_SCHEMA_VERSION_V3,
    StateEventError,
    build_state_event,
    canonical_event_bytes,
    replay_state_events,
    validate_state_event,
)
from context_control_plane.typed_state import canonical_state_bytes, validate_typed_state


class M303StateEventV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]

    def setUp(self):
        self.initial = self._state()
        self.after_child = copy.deepcopy(self.initial)
        self.after_child["project"]["revision"] = 2
        self.after_child["project"]["updated_at"] = "2026-08-13T16:01:00+08:00"
        self.after_child["claims"][0]["expected_project_revision"] = 2
        self.child = self._event(
            event_id="event-child",
            sequence_no=1,
            revision_before=1,
            project_after=self.after_child["project"],
            task_transition={
                "route_decision_sha256": "a" * 64,
                "route_apply_request_sha256": "1" * 64,
                "route_kind": "child",
                "task_events": [
                    {
                        "task_event_id": "task-child-proposed",
                        "event_kind": "child_proposed",
                        "work_id": "work-active",
                        "work_revision": 1,
                        "return_work_id": "work-active",
                        "checkpoint_ref": self._artifact("b"),
                        "related_event_id": None,
                    }
                ],
            },
        )

    @staticmethod
    def _artifact(letter: str = "a"):
        digest = letter * 64
        return {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": digest,
            "size_bytes": 12,
            "artifact_uri": f"artifact://sha256/{digest}",
        }

    @staticmethod
    def _work(work_id, kind, parent, status, *, revision=1):
        return {
            "work_id": work_id,
            "kind": kind,
            "title": work_id,
            "status": status,
            "parent_work_id": parent,
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [{"scope_kind": "capability", "scope_ref": f"scope/{work_id}"}],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": revision,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }

    @classmethod
    def _state(cls):
        return {
            "schema_version": "context.typed-state/v2alpha1",
            "project": {
                "project_id": "project-m303",
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
            "claims": [
                {
                    "claim_id": "claim-active",
                    "work_id": "work-active",
                    "actor_ref": "actor-owner",
                    "status": "active",
                    "expected_project_revision": 1,
                    "claimed_at": "2026-08-13T15:00:00+08:00",
                    "lease_expires_at": "2026-08-13T18:00:00+08:00",
                    "released_at": None,
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "scope/work-active"}
                    ],
                }
            ],
            "ideas": [],
            "decisions": [],
            "constraints": [],
            "evidence": [],
            "blockers": [],
            "effects": [],
        }

    def _event(self, *, event_id, sequence_no, revision_before, project_after, task_transition=None, **overrides):
        project = project_after["project"] if "project" in project_after else project_after
        return build_state_event(
            event_id=event_id,
            event_type=overrides.pop("event_type", "state-transition"),
            project_id=self.initial["project"]["project_id"],
            sequence_no=sequence_no,
            revision_before=revision_before,
            occurred_at=project["updated_at"],
            actor_ref="actor-owner",
            causation_ref="route:proposal",
            correlation_ref="campaign:m303",
            previous_event_sha256=overrides.pop("previous_event_sha256", None),
            supersedes_event_id=overrides.pop("supersedes_event_id", None),
            changes=overrides.pop("changes", [{"collection": "claims", "object_id": "claim-active", "value": copy.deepcopy(self.after_child["claims"][0])}]),
            project_after=project,
            task_transition=task_transition,
            schema_version=overrides.pop("schema_version", None),
        )

    def test_v3_schema_is_strict_registered_and_hashed(self):
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text(encoding="utf-8"))
        entry = next(item for item in registry["schemas"] if item["schema_id"] == "context.state-event")
        schema_path = self.root / entry["artifact_path"]
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(entry["current_wire_version"], EVENT_SCHEMA_VERSION_V3)
        self.assertIn("context.state-event/v1alpha1", entry["supported_wire_versions"])
        self.assertIn("context.state-event/v2alpha1", entry["supported_wire_versions"])
        self.assertEqual(hashlib.sha256(schema_path.read_bytes()).hexdigest(), entry["content_sha256"])
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["taskTransition"]["additionalProperties"])
        typed_schema = json.loads((self.root / "schemas/m3-01/typed-state-v2alpha1.schema.json").read_text(encoding="utf-8"))
        registry = Registry().with_resource(
            "https://context-control-plane.dev/schema/context.typed-state/v2alpha1",
            Resource.from_contents(typed_schema),
        )
        Draft202012Validator(schema, registry=registry).validate(self.child)

        invalid = copy.deepcopy(self.child)
        invalid["task_transition"]["task_events"][0]["unexpected"] = True
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema, registry=registry).validate(invalid)

    def test_v1_and_v2_events_cannot_carry_task_transition(self):
        for version in ("context.state-event/v1alpha1", "context.state-event/v2alpha1"):
            event = self._event(
                event_id=f"event-{version[-8:]}",
                sequence_no=1,
                revision_before=1,
                project_after=self.after_child["project"],
                task_transition=None,
                schema_version=version,
            )
            event["task_transition"] = None
            with self.assertRaisesRegex(StateEventError, "fields"):
                validate_state_event(event)

    def test_v3_schema_and_runtime_agree_on_nullable_and_route_kind_constraints(self):
        schema = json.loads(
            (self.root / "schemas/m3-03/state-event-v3alpha1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        typed_schema = json.loads(
            (self.root / "schemas/m3-01/typed-state-v2alpha1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        registry = Registry().with_resource(
            "https://context-control-plane.dev/schema/context.typed-state/v2alpha1",
            Resource.from_contents(typed_schema),
        )
        validator = Draft202012Validator(schema, registry=registry)

        nullable = self._event(
            event_id="event-null-transition",
            sequence_no=1,
            revision_before=1,
            project_after=self.after_child["project"],
            task_transition=None,
            schema_version=EVENT_SCHEMA_VERSION_V3,
        )
        validate_state_event(nullable)
        validator.validate(nullable)

        invalid = copy.deepcopy(self.child)
        invalid["event_type"] = "correction"
        invalid["supersedes_event_id"] = "event-prior"
        invalid["event_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in invalid.items() if key != "event_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(StateEventError, "child route"):
            validate_state_event(invalid)
        with self.assertRaises(ValidationError):
            validator.validate(invalid)

    def test_route_kinds_require_exact_ordered_task_events(self):
        for route_kind, task_events in (
            ("child", ["child_proposed"]),
            ("interrupt", ["task_suspended", "task_activated"]),
            ("switch", ["task_suspended", "task_activated"]),
            ("correction", ["correction_applied"]),
        ):
            transition = {
                "route_decision_sha256": "c" * 64,
                "route_apply_request_sha256": "2" * 64,
                "route_kind": route_kind,
                "task_events": [
                    {
                        "task_event_id": f"task-{index}",
                        "event_kind": kind,
                        "work_id": "work-active" if index == 0 else "work-target",
                        "work_revision": 1,
                        "return_work_id": None,
                        "checkpoint_ref": None,
                        "related_event_id": None,
                    }
                    for index, kind in enumerate(task_events)
                ],
            }
            after = copy.deepcopy(self.after_child)
            after["project"]["revision"] = 3 if route_kind == "correction" else 2
            after["project"]["updated_at"] = "2026-08-13T16:02:00+08:00"
            event_type = "correction" if route_kind == "correction" else "state-transition"
            kwargs = {"event_type": event_type}
            if route_kind == "correction":
                kwargs["supersedes_event_id"] = self.child["event_id"]
            event = self._event(
                event_id=f"event-{route_kind}",
                sequence_no=2 if route_kind == "correction" else 1,
                revision_before=2 if route_kind == "correction" else 1,
                project_after=after["project"],
                task_transition=transition,
                **kwargs,
            )
            validate_state_event(event)

        invalid = copy.deepcopy(self.child)
        invalid["task_transition"]["route_kind"] = "switch"
        with self.assertRaisesRegex(StateEventError, "task_events"):
            validate_state_event(invalid)

    def test_switch_replay_accepts_v3_only_for_typed_state_v2_and_is_deterministic(self):
        after = copy.deepcopy(self.initial)
        after["project"]["revision"] = 2
        after["project"]["updated_at"] = "2026-08-13T16:02:00+08:00"
        old_work = next(item for item in after["works"] if item["work_id"] == "work-active")
        target_work = next(item for item in after["works"] if item["work_id"] == "work-target")
        old_work["status"] = "ready"
        target_work["status"] = "active"
        after["project"]["active_work_ids"] = ["work-target"]
        after["project"]["primary_work_id"] = "work-target"
        after["claims"][0]["work_id"] = "work-target"
        after["claims"][0]["scope_owners"] = [{"scope_kind": "capability", "scope_ref": "scope/work-target"}]
        after["claims"][0]["expected_project_revision"] = 2
        event = self._event(
            event_id="event-switch",
            sequence_no=1,
            revision_before=1,
            project_after=after,
            changes=[
                {"collection": "works", "object_id": old_work["work_id"], "value": old_work},
                {"collection": "works", "object_id": target_work["work_id"], "value": target_work},
                {"collection": "claims", "object_id": "claim-active", "value": after["claims"][0]},
            ],
            task_transition={
                "route_decision_sha256": "d" * 64,
                "route_apply_request_sha256": "3" * 64,
                "route_kind": "switch",
                "task_events": [
                    {
                        "task_event_id": "task-suspended",
                        "event_kind": "task_suspended",
                        "work_id": "work-active",
                        "work_revision": 1,
                        "return_work_id": "work-active",
                        "checkpoint_ref": self._artifact("e"),
                        "related_event_id": None,
                    },
                    {
                        "task_event_id": "task-activated",
                        "event_kind": "task_activated",
                        "work_id": "work-target",
                        "work_revision": 1,
                        "return_work_id": None,
                        "checkpoint_ref": None,
                        "related_event_id": "task-suspended",
                    },
                ],
            },
        )
        restored = replay_state_events(self.initial, [event])
        self.assertEqual(canonical_state_bytes(restored), canonical_state_bytes(after))
        self.assertEqual(replay_state_events(self.initial, [event]), restored)

        v1 = copy.deepcopy(self.initial)
        v1["schema_version"] = "context.typed-state/v1alpha1"
        for work in v1["works"]:
            for field in (
                "return_point_work_id",
                "exit_criteria",
                "attempt_budget",
                "expires_at",
                "promotion_target_work_id",
                "mainline_authority",
            ):
                work.pop(field)
        with self.assertRaisesRegex(StateEventError, "wire version"):
            replay_state_events(v1, [event])

    def test_correction_requires_changed_target_keys_and_rejects_forked_lineage(self):
        correction_after = copy.deepcopy(self.after_child)
        correction_after["project"]["revision"] = 3
        correction_after["project"]["updated_at"] = "2026-08-13T16:03:00+08:00"
        corrected_claim = copy.deepcopy(self.after_child["claims"][0])
        corrected_claim["expected_project_revision"] = 3
        correction = self._event(
            event_id="event-correction",
            event_type="correction",
            sequence_no=2,
            revision_before=2,
            previous_event_sha256=self.child["event_sha256"],
            supersedes_event_id=self.child["event_id"],
            project_after=correction_after["project"],
            changes=[{"collection": "claims", "object_id": "claim-active", "value": corrected_claim}],
            task_transition={
                "route_decision_sha256": "f" * 64,
                "route_apply_request_sha256": "4" * 64,
                "route_kind": "correction",
                "task_events": [
                    {
                        "task_event_id": "task-correction",
                        "event_kind": "correction_applied",
                        "work_id": "work-active",
                        "work_revision": 1,
                        "return_work_id": None,
                        "checkpoint_ref": None,
                        "related_event_id": self.child["event_id"],
                    }
                ],
            },
        )
        replay_state_events(self.initial, [self.child, correction])

        fork = copy.deepcopy(correction)
        fork["event_id"] = "event-correction-fork"
        fork["sequence_no"] = 3
        fork["revision_before"] = 3
        fork["revision_after"] = 4
        fork["project_after"]["revision"] = 4
        fork["project_after"]["updated_at"] = "2026-08-13T16:04:00+08:00"
        fork["previous_event_sha256"] = correction["event_sha256"]
        fork["event_sha256"] = "0" * 64
        fork["event_sha256"] = hashlib.sha256(
            json.dumps({key: value for key, value in fork.items() if key != "event_sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(StateEventError, "fork"):
            replay_state_events(self.initial, [self.child, correction, fork])

        unrelated = copy.deepcopy(correction)
        unrelated["event_id"] = "event-correction-unrelated"
        unrelated["changes"] = [{"collection": "works", "object_id": "work-target", "value": copy.deepcopy(self.initial["works"][3])}]
        unrelated["event_sha256"] = "0" * 64
        unrelated["event_sha256"] = hashlib.sha256(
            json.dumps({key: value for key, value in unrelated.items() if key != "event_sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(StateEventError, "changed keys"):
            replay_state_events(self.initial, [self.child, unrelated])


if __name__ == "__main__":
    unittest.main()
