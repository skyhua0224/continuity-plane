"""Claude Code archive/stream adapter for M10-11 provider evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.claude_rollout_adapter import (
    ClaudeRolloutAdapterError,
    inspect_claude_rollout,
)


class M1011ClaudeRolloutAdapterTests(unittest.TestCase):
    def _write(self, path: Path, events: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

    def _assistant(self, uuid: str, message_id: str) -> dict:
        return {
            "type": "assistant",
            "uuid": uuid,
            "message": {
                "id": message_id,
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 900,
                    "output_tokens": 40,
                },
                "content": [{"type": "text", "text": "private answer"}],
            },
        }

    def test_archive_deduplicates_message_usage_and_reads_typed_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            self._write(
                path,
                [
                    self._assistant("uuid-1", "message-1"),
                    self._assistant("uuid-2", "message-1"),
                    self._assistant("uuid-3", "message-1"),
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "timestamp": "2026-08-20T12:00:00Z",
                        "compactMetadata": {
                            "trigger": "auto",
                            "preTokens": 1000,
                            "postTokens": 100,
                            "cumulativeDroppedTokens": 900,
                            "durationMs": 25,
                            "preservedSegment": {
                                "headUuid": "private-head",
                                "anchorUuid": "private-anchor",
                                "tailUuid": "private-tail",
                            },
                        },
                    },
                ],
            )
            receipt = inspect_claude_rollout(path)

        self.assertEqual(receipt["compaction_count"], 1)
        self.assertEqual(receipt["provider_usage"]["input_tokens"], 1002)
        self.assertEqual(receipt["provider_usage"]["cached_input_tokens"], 900)
        self.assertEqual(receipt["provider_usage"]["cache_write_input_tokens"], 100)
        self.assertEqual(receipt["provider_usage"]["output_tokens"], 40)
        self.assertEqual(receipt["compactions"][0]["pre_tokens"], 1000)
        self.assertEqual(receipt["compactions"][0]["post_tokens"], 100)
        self.assertEqual(receipt["compactions"][0]["duration_ms"], 25)
        encoded = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("private answer", encoded)
        self.assertNotIn("private-head", encoded)

    def test_zero_usage_api_error_is_unavailable_not_measured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude-error.jsonl"
            self._write(
                path,
                [
                    {
                        "type": "result",
                        "is_error": True,
                        "api_error_status": 403,
                        "usage": {
                            "input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 0,
                        },
                        "result": "private provider error text",
                    }
                ],
            )
            receipt = inspect_claude_rollout(path)
        self.assertEqual(receipt["provider_usage"]["status"], "unavailable")
        self.assertEqual(receipt["provider_status"], "api-error-403")
        self.assertNotIn("private provider error text", json.dumps(receipt))

    def test_duplicate_message_with_changed_usage_and_malformed_compaction_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude-invalid.jsonl"
            first = self._assistant("uuid-1", "message-1")
            changed = self._assistant("uuid-2", "message-1")
            changed["message"]["usage"]["output_tokens"] = 41
            self._write(path, [first, changed])
            with self.assertRaises(ClaudeRolloutAdapterError):
                inspect_claude_rollout(path)

            compact = {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 100},
            }
            self._write(path, [first, compact])
            with self.assertRaises(ClaudeRolloutAdapterError):
                inspect_claude_rollout(path)


if __name__ == "__main__":
    unittest.main()
