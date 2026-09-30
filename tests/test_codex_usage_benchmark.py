"""Provider usage receipt parsing and summary tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane.codex_usage_benchmark import (
    CodexUsageBenchmarkError,
    parse_codex_jsonl_result,
    summarize_usage_samples,
)


class CodexUsageBenchmarkTests(unittest.TestCase):
    def event_stream(self, *, input_tokens: int = 100) -> str:
        return "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": '{"verdict":"ok","next_action":"M10-00"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": 10,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        },
                    }
                ),
            ]
        )

    def test_parser_extracts_usage_and_quality(self) -> None:
        result = parse_codex_jsonl_result(self.event_stream())
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertTrue(result["quality"]["correct"])

    def test_parser_rejects_missing_usage_or_wrong_quality(self) -> None:
        with self.assertRaises(CodexUsageBenchmarkError):
            parse_codex_jsonl_result(self.event_stream(input_tokens=0))
        wrong = self.event_stream().replace("M10-00", "wrong")
        with self.assertRaises(CodexUsageBenchmarkError):
            parse_codex_jsonl_result(wrong)

    def test_summary_reports_packet_reduction_and_quality_rate(self) -> None:
        samples = [
            {
                "case": "baseline",
                "usage": {"input_tokens": 100},
                "quality": {"correct": True},
            },
            {
                "case": "baseline",
                "usage": {"input_tokens": 110},
                "quality": {"correct": True},
            },
            {
                "case": "packet",
                "usage": {"input_tokens": 60},
                "quality": {"correct": True},
            },
            {
                "case": "packet",
                "usage": {"input_tokens": 61},
                "quality": {"correct": True},
            },
        ]
        summary = summarize_usage_samples(samples)
        self.assertEqual(summary["baseline"]["count"], 2)
        self.assertEqual(summary["packet"]["count"], 2)
        self.assertAlmostEqual(
            summary["packet"]["input_reduction_percent"], 42.381, places=3
        )
        self.assertEqual(summary["packet"]["quality_rate"], 1.0)

    def test_committed_provider_receipt_matches_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/evidence/m10-00-codex-usage-ab-results.json").read_text()
        )
        schema = json.loads(
            (root / "schemas/m10-00/codex-usage-ab.schema.json").read_text()
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)


if __name__ == "__main__":
    unittest.main()
