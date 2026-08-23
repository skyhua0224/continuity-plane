"""Codex rollout metadata adapter for real M10-11 provider evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.codex_rollout_adapter import (
    CodexRolloutAdapterError,
    inspect_codex_rollout,
)


class M1011CodexRolloutAdapterTests(unittest.TestCase):
    def _write(self, path: Path, events: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
        )

    def _token(self, *, input_tokens: int, output_tokens: int) -> dict:
        return {
            "timestamp": "2026-08-20T12:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": input_tokens // 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": output_tokens // 2,
                        "total_tokens": input_tokens + output_tokens,
                    },
                    "last_token_usage": {
                        "input_tokens": 1,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 2,
                    },
                    "model_context_window": 1_000_000,
                },
            },
        }

    def test_explicit_compaction_and_cumulative_usage_are_derived_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            events = [
                self._token(input_tokens=1_000, output_tokens=100),
                {
                    "timestamp": "2026-08-20T12:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "private question"}],
                    },
                },
                {
                    "timestamp": "2026-08-20T12:02:00Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
                {
                    "timestamp": "2026-08-20T12:03:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "private answer"}],
                    },
                },
                self._token(input_tokens=61_000, output_tokens=4_100),
            ]
            self._write(path, events)

            receipt = inspect_codex_rollout(path)

        self.assertEqual(receipt["compaction_count"], 1)
        self.assertEqual(receipt["provider_usage"]["input_tokens"], 60_000)
        self.assertEqual(receipt["provider_usage"]["output_tokens"], 4_000)
        self.assertEqual(receipt["provider_request_usage"]["status"], "measured")
        self.assertEqual(receipt["provider_request_usage"]["sample_count"], 2)
        self.assertEqual(receipt["provider_request_usage"]["input_tokens"], 2)
        self.assertEqual(receipt["provider_request_usage"]["cached_input_tokens"], 0)
        self.assertEqual(receipt["provider_request_usage"]["last_input_tokens"], 1)
        self.assertEqual(receipt["provider_request_usage"]["peak_input_tokens"], 1)
        self.assertEqual(receipt["context_window_tokens"], 1_000_000)
        self.assertEqual(receipt["user_message_count"], 1)
        self.assertEqual(receipt["assistant_message_count"], 1)
        encoded = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("private question", encoded)
        self.assertNotIn("private answer", encoded)

    def test_error_text_with_compact_is_not_a_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            self._write(
                path,
                [
                    self._token(input_tokens=100, output_tokens=10),
                    {
                        "timestamp": "2026-08-20T12:01:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "error",
                            "message": "auto compact failed",
                        },
                    },
                    self._token(input_tokens=200, output_tokens=20),
                ],
            )
            receipt = inspect_codex_rollout(path)
        self.assertEqual(receipt["compaction_count"], 0)

    def test_counter_reset_mixed_windows_and_malformed_lines_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            reset = [
                self._token(input_tokens=1_000, output_tokens=100),
                self._token(input_tokens=900, output_tokens=110),
            ]
            self._write(path, reset)
            with self.assertRaises(CodexRolloutAdapterError):
                inspect_codex_rollout(path)

            mixed = [
                self._token(input_tokens=1_000, output_tokens=100),
                self._token(input_tokens=2_000, output_tokens=200),
            ]
            mixed[-1]["payload"]["info"]["model_context_window"] = 950_000
            self._write(path, mixed)
            with self.assertRaises(CodexRolloutAdapterError):
                inspect_codex_rollout(path)

            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(CodexRolloutAdapterError):
                inspect_codex_rollout(path)

    def test_legacy_total_only_usage_is_unavailable_not_zero_measured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.jsonl"
            first = self._token(input_tokens=0, output_tokens=0)
            second = self._token(input_tokens=0, output_tokens=0)
            first["payload"]["info"]["model_context_window"] = None
            second["payload"]["info"]["model_context_window"] = None
            first["payload"]["info"]["total_token_usage"]["total_tokens"] = 1000
            second["payload"]["info"]["total_token_usage"]["total_tokens"] = 2000
            self._write(path, [first, second])
            receipt = inspect_codex_rollout(path)
        self.assertEqual(receipt["provider_usage"]["status"], "unavailable")
        self.assertIsNone(receipt["provider_usage"]["input_tokens"])
        self.assertIn("legacy", receipt["provider_usage"]["unavailable_reason"])
        self.assertIsNone(receipt["context_window_tokens"])

    def test_interval_uses_prior_cumulative_sample_and_only_interval_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interval.jsonl"
            before = self._token(input_tokens=1_000, output_tokens=100)
            before["timestamp"] = "2026-08-20T11:59:00Z"
            inside = self._token(input_tokens=1_600, output_tokens=150)
            inside["timestamp"] = "2026-08-20T12:01:00Z"
            inside["payload"]["info"]["last_token_usage"]["input_tokens"] = 600
            after = self._token(input_tokens=2_500, output_tokens=200)
            after["timestamp"] = "2026-08-20T13:01:00Z"
            after["payload"]["info"]["last_token_usage"]["input_tokens"] = 900
            self._write(path, [before, inside, after])
            receipt = inspect_codex_rollout(
                path,
                started_at="2026-08-20T12:00:00Z",
                ended_at="2026-08-20T13:00:00Z",
            )
        self.assertEqual(receipt["provider_usage"]["input_tokens"], 600)
        self.assertEqual(receipt["provider_usage"]["output_tokens"], 50)
        self.assertEqual(receipt["provider_request_usage"]["sample_count"], 1)
        self.assertEqual(receipt["provider_request_usage"]["input_tokens"], 600)
        self.assertEqual(
            receipt["observation_interval"],
            {
                "started_at": "2026-08-20T12:00:00Z",
                "ended_at": "2026-08-20T13:00:00Z",
            },
        )

    def test_interval_is_half_open_and_rejects_missing_time_or_counter_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interval-errors.jsonl"
            before = self._token(input_tokens=1_000, output_tokens=100)
            before["timestamp"] = "2026-08-20T11:59:00Z"
            boundary = self._token(input_tokens=1_600, output_tokens=150)
            boundary["timestamp"] = "2026-08-20T13:00:00Z"
            self._write(path, [before, boundary])
            with self.assertRaisesRegex(CodexRolloutAdapterError, "no token_count"):
                inspect_codex_rollout(
                    path,
                    started_at="2026-08-20T12:00:00Z",
                    ended_at="2026-08-20T13:00:00Z",
                )

            missing = self._token(input_tokens=1_600, output_tokens=150)
            missing.pop("timestamp")
            self._write(path, [before, missing])
            with self.assertRaisesRegex(CodexRolloutAdapterError, "timestamp"):
                inspect_codex_rollout(
                    path,
                    started_at="2026-08-20T12:00:00Z",
                    ended_at="2026-08-20T13:00:00Z",
                )

            reset = self._token(input_tokens=900, output_tokens=150)
            reset["timestamp"] = "2026-08-20T12:01:00Z"
            self._write(path, [before, reset])
            with self.assertRaisesRegex(CodexRolloutAdapterError, "counter reset"):
                inspect_codex_rollout(
                    path,
                    started_at="2026-08-20T12:00:00Z",
                    ended_at="2026-08-20T13:00:00Z",
                )


if __name__ == "__main__":
    unittest.main()
