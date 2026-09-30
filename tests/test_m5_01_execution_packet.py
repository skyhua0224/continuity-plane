"""M5-01 bounded Execution Packet composition contract."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.compiled_skill_packet import compiled_skill_packet_digest
from context_control_plane.skill_resolver import (
    canonical_skill_resolution_request_bytes,
)
from context_control_plane.typed_state import canonical_state_bytes


class M501ExecutionPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixtures = yaml.safe_load(
            (cls.root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixtures["cases"]
                if case["case_id"] == "solo-active-work"
            )
        )
        cls.skill_fixture = json.loads(
            (
                cls.root
                / "experiments/skills/m4-09-skill-resolution-replay-v1alpha1.json"
            ).read_text(encoding="utf-8")
        )

    @staticmethod
    def _cursor():
        return {
            "last_durable_action": "m4-09-commit",
            "in_flight_phase": "ready-to-execute",
            "confirmed_input_refs": ["opaque://input/m4-09-commit"],
            "reserved_effect_ids": [],
            "replay_policy": "verify-before-effect",
        }

    def _skill_inputs(self):
        request = copy.deepcopy(self.skill_fixture["request"])
        decision = copy.deepcopy(self.skill_fixture["decision"])
        request["request_id"] = "resolve-packet"
        request["project_ref"] = "project://project-solo"
        request["task_ref"] = "task://work-solo"
        decision["request_id"] = request["request_id"]
        decision["request_sha256"] = hashlib.sha256(
            canonical_skill_resolution_request_bytes(request)
        ).hexdigest()
        packet = copy.deepcopy(self.skill_fixture["packet"])
        return request, decision, packet

    def _compose(self, **overrides):
        from context_control_plane.execution_packet import compose_execution_packet

        request, decision, skill_packet = self._skill_inputs()
        arguments = {
            "snapshot": copy.deepcopy(self.snapshot),
            "skill_request": request,
            "skill_decision": decision,
            "compiled_skill_packet": skill_packet,
            "next_action": "run the M5-01 bounded packet canary",
            "continuation_cursor": self._cursor(),
            "canonical_plan_sha256": "c" * 64,
            "observed_at": "2026-08-14T20:00:00+08:00",
        }
        arguments.update(overrides)
        return compose_execution_packet(**arguments)

    def test_schema_is_strict_registered_and_packet_binds_current_state(self):
        schema_path = self.root / "schemas/m5-01/execution-packet.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.execution-packet"
        )
        self.assertEqual(
            entry["content_sha256"], hashlib.sha256(schema_path.read_bytes()).hexdigest()
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

        packet = self._compose()
        Draft202012Validator(schema).validate(packet)
        self.assertEqual(packet["project_id"], "project-solo")
        self.assertEqual(packet["project_revision"], 7)
        self.assertEqual(packet["active_leaf"]["work_id"], "work-solo")
        self.assertEqual(
            packet["skill_lock"]["compiled_packet_sha256"],
            compiled_skill_packet_digest(self.skill_fixture["packet"]),
        )
        self.assertFalse(packet["state_write_authority"])

    def test_composer_excludes_history_unrelated_ideas_and_full_skill_body(self):
        packet = self._compose()

        self.assertEqual(packet["next_action"], "run the M5-01 bounded packet canary")
        self.assertEqual(
            [item["idea_id"] for item in packet["idea_refs"]], ["idea-later"]
        )
        self.assertEqual(
            [item["decision_id"] for item in packet["decisions"]], ["decision-solo"]
        )
        self.assertEqual(packet["evidence_refs"][0]["evidence_id"], "evidence-solo")
        self.assertNotIn("manifest_set", packet)
        self.assertNotIn("history", json.dumps(packet, sort_keys=True))

    def test_composer_is_deterministic_and_does_not_mutate_inputs(self):
        snapshot = copy.deepcopy(self.snapshot)
        request, decision, skill_packet = self._skill_inputs()
        first = self._compose()
        second = self._compose()
        self.assertEqual(first, second)
        self.assertEqual(first["packet_sha256"], second["packet_sha256"])
        self.assertEqual(self.snapshot, snapshot)
        self.assertEqual(request, self._skill_inputs()[0])
        self.assertEqual(decision, self._skill_inputs()[1])
        self.assertEqual(skill_packet, self._skill_inputs()[2])
        canonical_state_bytes(self.snapshot)

    def test_stale_skill_binding_and_unverified_evidence_fail_closed(self):
        _request, _decision, skill_packet = self._skill_inputs()
        changed = copy.deepcopy(skill_packet)
        changed["selections"][0]["content_sha256"] = "e" * 64
        from context_control_plane.execution_packet import ExecutionPacketError

        with self.assertRaisesRegex(ExecutionPacketError, "compiled Skill"):
            self._compose(compiled_skill_packet=changed)

        unverified = copy.deepcopy(self.snapshot)
        unverified["evidence"][0]["validity"] = "unverified"
        with self.assertRaisesRegex(ExecutionPacketError, "evidence"):
            self._compose(snapshot=unverified)

    def test_duplicate_next_action_and_oversized_packet_are_rejected(self):
        from context_control_plane.execution_packet import ExecutionPacketError

        with self.assertRaisesRegex(ExecutionPacketError, "next_action"):
            self._compose(next_action="run\nrun")

        oversized = copy.deepcopy(self.snapshot)
        oversized["works"][0]["title"] = "x" * (16 * 1024)
        with self.assertRaisesRegex(ExecutionPacketError, "size"):
            self._compose(snapshot=oversized)


if __name__ == "__main__":
    unittest.main()
