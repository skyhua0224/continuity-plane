from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from context_control_plane.codex_exec_observation import (
    compare_codex_exec_observations,
    observe_codex_exec_stream,
    validate_codex_exec_comparison,
    validate_codex_exec_observation,
)


def _line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


class M1100CodexExecObservationTests(unittest.TestCase):
    def observation(
        self,
        *,
        arm: str,
        input_tokens: int,
        output_tokens: int,
        read_status: bool,
    ) -> dict:
        command = "sed -n '1,120p' STATUS.md" if read_status else "rg -n package pyproject.toml"
        stream = io.StringIO(
            _line({"type": "thread.started", "thread_id": "private-thread"})
            + _line(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": command,
                        "aggregated_output": "private tool output",
                    },
                }
            )
            + _line(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Direct answer without recovery narration.",
                    },
                }
            )
            + _line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": input_tokens // 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": 10,
                    },
                }
            )
        )
        hook_events = [
            {"event_type": "precompact", "success": True, "trigger": "auto"},
            {"event_type": "postcompact", "success": True, "trigger": "auto"},
            {"event_type": "session-start", "success": True, "trigger": "compact"},
        ]
        return observe_codex_exec_stream(
            stream,
            arm=arm,
            segment_id=f"{arm}-01",
            observed_at="2026-08-30T19:00:00+08:00",
            hook_events=hook_events,
        )

    def test_stream_is_sanitized_and_binds_compaction_chain(self) -> None:
        receipt = self.observation(
            arm="candidate",
            input_tokens=100_000,
            output_tokens=900,
            read_status=False,
        )

        validate_codex_exec_observation(receipt)
        self.assertEqual(receipt["provider_usage"]["input_tokens"], 100_000)
        self.assertEqual(receipt["context_efficiency"]["status_read_calls"], 0)
        self.assertEqual(receipt["compaction"]["complete_chains"], 1)
        self.assertEqual(receipt["compaction"]["failed_events"], 0)
        encoded = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn("private-thread", encoded)
        self.assertNotIn("private tool output", encoded)
        self.assertNotIn("Direct answer", encoded)
        self.assertFalse(receipt["raw_transcript_admission"])

    def test_comparison_uses_three_samples_and_reports_vetoes(self) -> None:
        baseline = [
            self.observation(
                arm="baseline",
                input_tokens=value,
                output_tokens=1200,
                read_status=True,
            )
            for value in (150_000, 151_000, 149_000)
        ]
        candidate = [
            self.observation(
                arm="candidate",
                input_tokens=value,
                output_tokens=900,
                read_status=False,
            )
            for value in (100_000, 101_000, 99_000)
        ]

        report = compare_codex_exec_observations(
            baseline,
            candidate,
            report_id="m11-live-pair",
            observed_at="2026-08-30T19:10:00+08:00",
        )

        self.assertEqual(report["sample_count_per_arm"], 3)
        self.assertGreaterEqual(report["improvements"]["input_median_percent"], 30)
        self.assertEqual(report["veto_failures"], [])
        self.assertEqual(report["verdict"], "qualified")
        validate_codex_exec_comparison(report)

    def test_stdin_runner_writes_only_the_sanitized_receipt(self) -> None:
        stream = (
            _line({"type": "thread.started", "thread_id": "private-thread"})
            + _line(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "private response body",
                    },
                }
            )
            + _line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 50,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 2,
                    },
                }
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook_path = root / "hooks.jsonl"
            hook_path.write_text(
                _line(
                    {
                        "event_type": "session-start",
                        "trigger": "compact",
                        "success": True,
                        "session_sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            output = root / "receipt.json"
            completed = subprocess.run(
                [
                    ".venv/bin/python",
                    "tools/sanitize_codex_exec_observation.py",
                    "--arm",
                    "candidate",
                    "--segment-id",
                    "candidate-01",
                    "--observed-at",
                    "2026-08-30T19:00:00+08:00",
                    "--hook-events",
                    str(hook_path),
                    "--output",
                    str(output),
                ],
                input=stream,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_codex_exec_observation(receipt)
            encoded = json.dumps(receipt)
            self.assertNotIn("private-thread", encoded)
            self.assertNotIn("private response body", encoded)

    def test_comparison_runner_requires_three_receipts_per_arm(self) -> None:
        baseline = [
            self.observation(
                arm="baseline",
                input_tokens=value,
                output_tokens=1200,
                read_status=True,
            )
            for value in (150_000, 151_000, 149_000)
        ]
        candidate = [
            self.observation(
                arm="candidate",
                input_tokens=value,
                output_tokens=900,
                read_status=False,
            )
            for value in (100_000, 101_000, 99_000)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_paths = []
            candidate_paths = []
            for index, receipt in enumerate(baseline):
                path = root / f"baseline-{index}.json"
                path.write_text(json.dumps(receipt), encoding="utf-8")
                baseline_paths.append(path)
            for index, receipt in enumerate(candidate):
                path = root / f"candidate-{index}.json"
                path.write_text(json.dumps(receipt), encoding="utf-8")
                candidate_paths.append(path)
            output = root / "comparison.json"
            command = [
                ".venv/bin/python",
                "tools/compare_codex_exec_observations.py",
                "--report-id",
                "m11-live-pair",
                "--observed-at",
                "2026-08-30T19:10:00+08:00",
                "--output",
                str(output),
            ]
            for path in baseline_paths:
                command.extend(["--baseline", str(path)])
            for path in candidate_paths:
                command.extend(["--candidate", str(path)])
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            validate_codex_exec_comparison(report)
            self.assertEqual(report["verdict"], "qualified")


if __name__ == "__main__":
    unittest.main()
