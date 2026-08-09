import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_control_plane.claude_archive_extraction import (
    inspect_claude_archive,
    read_claude_candidate_event,
    read_claude_candidate_events,
)


class ClaudeArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "synthetic-claude.jsonl"

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

    def test_inventory_and_read_normalize_human_text_and_compaction_only(self):
        self.write_lines(
            {
                "type": "user",
                "sessionId": "provider-secret",
                "cwd": "/home/alice/private",
                "message": {"role": "user", "content": "measure ECN behavior"},
            },
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "active task and return point"},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "hidden chain"}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "large output"}],
                },
            },
        )

        inventory = inspect_claude_archive(self.path)
        events = tuple(
            read_claude_candidate_event(
                self.path,
                candidate,
                expected_archive_sha256=inventory.archive_sha256,
            )
            for candidate in inventory.candidates
        )

        self.assertEqual(
            [event.event_kind for event in events],
            ["user_message", "compaction_checkpoint"],
        )
        self.assertEqual(
            [event.text for event in events],
            ["measure ECN behavior", "active task and return point"],
        )
        self.assertEqual(inventory.quarantined_count, 2)
        self.assertNotIn("provider-secret", repr(inventory))
        self.assertNotIn("hidden chain", repr(inventory))
        self.assertNotIn("large output", repr(inventory))

    def test_batch_read_verifies_archive_once_and_returns_archive_order(self):
        self.write_lines(
            {"type": "user", "message": {"role": "user", "content": "first"}},
            {"type": "user", "message": {"role": "user", "content": "second"}},
        )
        inventory = inspect_claude_archive(self.path)

        with patch(
            "context_control_plane.claude_archive_extraction._sha256_file",
            wraps=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        ) as digest:
            events = read_claude_candidate_events(
                self.path,
                tuple(reversed(inventory.candidates)),
                expected_archive_sha256=inventory.archive_sha256,
                max_total_bytes=4096,
            )

        self.assertEqual([event.text for event in events], ["first", "second"])
        digest.assert_called_once()

    def test_runtime_injected_user_envelopes_are_quarantined(self):
        self.write_lines(
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "promptSource": "system",
                "message": {"role": "user", "content": "background command completed"},
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Another Claude session sent a message: <teammate-message>payload</teammate-message>",
                },
            },
            {
                "type": "user",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": "continue the active ALTP task"},
            },
        )

        inventory = inspect_claude_archive(self.path)

        self.assertEqual(len(inventory.candidates), 1)
        self.assertEqual(inventory.quarantined_count, 2)


if __name__ == "__main__":
    unittest.main()
