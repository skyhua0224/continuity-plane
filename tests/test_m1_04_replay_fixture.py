import copy
import hashlib
import json
import unittest

from context_control_plane.replay_fixture import (
    ReplayFixtureError,
    build_replay_fixture,
    validate_replay_fixture,
)


class ReplayFixtureTests(unittest.TestCase):
    def setUp(self):
        self.content = "continue M1-04 and preserve the current return point"
        self.evidence_ref = "artifact://context-control-plane/MASTER.md@revision-8"
        self.fixture = build_replay_fixture(
            project_id="context-control-plane",
            scenario_class="compaction-recovery",
            source={
                "provider": "codex",
                "source_thread_ref": "thr_abcdefghijklmnopqrstuvwxyz",
                "source_range_ref": "rng_abcdefghijklmnopqrstuvwxyz",
                "archive_sha256": "a" * 64,
                "range_sha256": "b" * 64,
                "byte_start": 100,
                "byte_end": 200,
                "extractor_version": "codex-rollout-observed/v1alpha1",
            },
            input_event={
                "event_kind": "user_message",
                "content": self.content,
                "content_sha256": hashlib.sha256(self.content.encode()).hexdigest(),
            },
            extraction={
                "selection_version": "anchored-contiguous-fragment/v1",
                "normalized_event_sha256": "c" * 64,
                "source_fragment_sha256": "d" * 64,
                "content_start": 0,
                "content_end": len(self.content),
                "findings_by_category": {},
                "content_sha256": hashlib.sha256(self.content.encode()).hexdigest(),
            },
            initial_state={
                "active_task": "M1-04",
                "latest_decision": "read only the approved byte range",
                "constraints": ["raw transcript Git admission remains closed"],
                "blocker": None,
                "return_point": "M1-04.range-index",
                "next_action": "validate the selected candidate",
                "rejected_decisions": ["copy the complete rollout into Git"],
            },
            expected_state={
                "active_task": "M1-04",
                "latest_decision": "read only the approved byte range",
                "constraints": ["raw transcript Git admission remains closed"],
                "blocker": None,
                "return_point": "M1-04.range-index",
                "next_action": "validate the selected candidate",
                "rejected_decisions": ["copy the complete rollout into Git"],
            },
            expected_gate="allow",
            evidence_refs=[self.evidence_ref],
            sanitizer_version="m1-03.v1",
        )

    def test_independent_validation_returns_content_free_deterministic_receipt(self):
        first = validate_replay_fixture(
            self.fixture,
            current_evidence_refs={self.evidence_ref},
            validated_at="2026-08-09T19:00:00+08:00",
        )
        second = validate_replay_fixture(
            self.fixture,
            current_evidence_refs={self.evidence_ref},
            validated_at="2026-08-09T19:00:00+08:00",
        )

        self.assertEqual(first, second)
        self.assertEqual(first.status, "passed")
        self.assertEqual(first.validator_version, "m1-04-independent/v1alpha1")
        self.assertNotIn(self.content, repr(first))

    def test_validator_rejects_private_identity_path_and_secret_independently(self):
        cases = (
            ("provider_thread_id", "raw-id"),
            ("archive_path", "/home/alice/private/rollout.jsonl"),
        )
        for key, value in cases:
            broken = copy.deepcopy(self.fixture)
            broken["source"][key] = value
            with self.subTest(key=key), self.assertRaises(ReplayFixtureError):
                validate_replay_fixture(
                    broken,
                    current_evidence_refs={self.evidence_ref},
                    validated_at="2026-08-09T19:00:00+08:00",
                )

        broken = copy.deepcopy(self.fixture)
        secret = "Authorization: Bearer super-secret-token"
        broken["input_event"]["content"] = secret
        broken["input_event"]["content_sha256"] = hashlib.sha256(secret.encode()).hexdigest()
        with self.assertRaisesRegex(ReplayFixtureError, "secret-like"):
            validate_replay_fixture(
                broken,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )

        broken = copy.deepcopy(self.fixture)
        raw_uuid_v7 = "019fe216-111c-71e3-a1af-497306ba2391"
        broken["input_event"]["content"] = raw_uuid_v7
        content_hash = hashlib.sha256(raw_uuid_v7.encode()).hexdigest()
        broken["input_event"]["content_sha256"] = content_hash
        broken["sanitization"]["content_sha256"] = content_hash
        payload = {key: value for key, value in broken.items() if key != "fixture_sha256"}
        broken["fixture_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ReplayFixtureError, "raw provider UUID"):
            validate_replay_fixture(
                broken,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )

    def test_validator_requires_current_external_evidence(self):
        with self.assertRaises(ReplayFixtureError):
            validate_replay_fixture(
                self.fixture,
                current_evidence_refs=set(),
                validated_at="2026-08-09T19:00:00+08:00",
            )

    def test_validator_rejects_content_hash_or_fixture_hash_tampering(self):
        broken_content = copy.deepcopy(self.fixture)
        broken_content["input_event"]["content"] += " tampered"
        with self.assertRaises(ReplayFixtureError):
            validate_replay_fixture(
                broken_content,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )

        broken_fixture = copy.deepcopy(self.fixture)
        broken_fixture["fixture_sha256"] = "0" * 64
        with self.assertRaises(ReplayFixtureError):
            validate_replay_fixture(
                broken_fixture,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )

    def test_validator_rejects_reviving_a_rejected_decision(self):
        broken = copy.deepcopy(self.fixture)
        broken["expected_state"]["latest_decision"] = "copy the complete rollout into Git"
        payload = {key: value for key, value in broken.items() if key != "fixture_sha256"}
        broken["fixture_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with self.assertRaises(ReplayFixtureError):
            validate_replay_fixture(
                broken,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )

    def test_validator_rejects_oversized_fixture_input(self):
        content = "x" * 4097
        broken = copy.deepcopy(self.fixture)
        broken["input_event"]["content"] = content
        broken["input_event"]["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        payload = {key: value for key, value in broken.items() if key != "fixture_sha256"}
        broken["fixture_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with self.assertRaises(ReplayFixtureError):
            validate_replay_fixture(
                broken,
                current_evidence_refs={self.evidence_ref},
                validated_at="2026-08-09T19:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
