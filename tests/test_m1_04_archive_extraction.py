import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_control_plane.archive_extraction import (
    ArchiveExtractionError,
    inspect_codex_rollout,
    read_bounded_jsonl_range,
    read_candidate_event,
    read_candidate_events,
)


class ArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "synthetic-rollout.jsonl"

    def tearDown(self):
        self.tempdir.cleanup()

    def write_lines(self, *items):
        payload = b"".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
            for item in items
        )
        self.path.write_bytes(payload)
        return payload

    def test_inventory_contains_metadata_only_and_selects_material_events(self):
        secret = "provider-thread-secret"
        self.write_lines(
            {"type": "session_meta", "payload": {"id": secret, "cwd": "/home/alice/private"}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "measure ECN behavior"},
            },
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": ["hidden chain"]},
            },
            {
                "type": "compacted",
                "message": "active task and return point",
                "replacement_history": ["excluded history"],
                "window_number": 1,
            },
        )

        inventory = inspect_codex_rollout(self.path)

        self.assertEqual(inventory.line_count, 4)
        self.assertEqual(
            [candidate.event_kind for candidate in inventory.candidates],
            ["user_message", "compaction_checkpoint"],
        )
        self.assertEqual(inventory.event_counts["response_item:reasoning"], 1)
        self.assertNotIn(secret, repr(inventory))
        self.assertNotIn("hidden chain", repr(inventory))
        self.assertNotIn("measure ECN behavior", repr(inventory))

    def test_candidate_read_is_bounded_hash_verified_and_normalized(self):
        self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "measure ECN behavior"},
            }
        )
        inventory = inspect_codex_rollout(self.path)

        event = read_candidate_event(
            self.path,
            inventory.candidates[0],
            expected_archive_sha256=inventory.archive_sha256,
        )

        self.assertEqual(event.event_kind, "user_message")
        self.assertEqual(event.text, "measure ECN behavior")
        self.assertEqual(
            event.range_sha256,
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
        )
        self.assertNotIn("payload", event.metadata)

    def test_observed_compacted_payload_exposes_only_checkpoint_message(self):
        self.write_lines(
            {
                "timestamp": "2026-08-09T00:00:00Z",
                "type": "compacted",
                "payload": {
                    "message": "active task M1-04 and return point",
                    "replacement_history": [{"excluded": "history"}],
                    "window_number": 3,
                    "window_id": "00000000-0000-4000-8000-000000000000",
                },
            }
        )

        inventory = inspect_codex_rollout(self.path)
        event = read_candidate_event(
            self.path,
            inventory.candidates[0],
            expected_archive_sha256=inventory.archive_sha256,
        )

        self.assertEqual(event.event_kind, "compaction_checkpoint")
        self.assertEqual(event.text, "active task M1-04 and return point")
        self.assertNotIn("replacement_history", repr(event))
        self.assertNotIn("window_id", repr(event))

    def test_candidate_read_never_uses_whole_file_read_bytes(self):
        self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "bounded read"},
            }
        )
        inventory = inspect_codex_rollout(self.path)

        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            event = read_candidate_event(
                self.path,
                inventory.candidates[0],
                expected_archive_sha256=inventory.archive_sha256,
            )

        self.assertEqual(event.text, "bounded read")

    def test_batch_candidate_read_verifies_archive_once(self):
        self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "first"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "second"},
            },
        )
        inventory = inspect_codex_rollout(self.path)

        with patch(
            "context_control_plane.archive_extraction._sha256_file",
            wraps=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        ) as digest:
            events = read_candidate_events(
                self.path,
                inventory.candidates,
                expected_archive_sha256=inventory.archive_sha256,
                max_total_bytes=4096,
            )

        self.assertEqual([event.text for event in events], ["first", "second"])
        digest.assert_called_once()

    def test_range_reader_rejects_unaligned_oversized_and_changed_archive(self):
        raw = self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "first"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "second"},
            },
        )
        archive_sha256 = hashlib.sha256(raw).hexdigest()
        first_end = raw.index(b"\n") + 1

        with self.assertRaises(ArchiveExtractionError):
            read_bounded_jsonl_range(
                self.path,
                start=1,
                end=first_end,
                max_bytes=1024,
                expected_archive_sha256=archive_sha256,
            )
        with self.assertRaises(ArchiveExtractionError):
            read_bounded_jsonl_range(
                self.path,
                start=0,
                end=len(raw),
                max_bytes=len(raw) - 1,
                expected_archive_sha256=archive_sha256,
            )

        self.path.write_bytes(raw + b"{}\n")
        with self.assertRaises(ArchiveExtractionError):
            read_bounded_jsonl_range(
                self.path,
                start=0,
                end=first_end,
                max_bytes=1024,
                expected_archive_sha256=archive_sha256,
            )

    def test_reasoning_and_tool_output_cannot_be_read_as_candidates(self):
        self.write_lines(
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": ["hidden chain"]},
            },
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "output": "large output"},
            },
        )

        inventory = inspect_codex_rollout(self.path)

        self.assertEqual(inventory.candidates, ())
        self.assertEqual(inventory.quarantined_count, 2)

    def test_oversized_material_event_is_quarantined_before_content_read(self):
        self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "x" * 2048},
            }
        )

        inventory = inspect_codex_rollout(self.path, max_candidate_line_bytes=512)

        self.assertEqual(inventory.candidates, ())
        self.assertEqual(inventory.oversized_candidate_count, 1)

    def test_unknown_material_shape_is_quarantined(self):
        self.write_lines(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "content": "wrong field"},
            }
        )

        inventory = inspect_codex_rollout(self.path)

        self.assertEqual(inventory.candidates, ())
        self.assertEqual(inventory.schema_quarantine_count, 1)


if __name__ == "__main__":
    unittest.main()
