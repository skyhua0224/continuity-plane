"""M8-10 realtime collaboration and long-campaign scale acceptance."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.collaboration_notification_benchmark import (
    benchmark_collaboration_notifications,
    validate_collaboration_notification_benchmark,
)


def _resign(receipt: dict) -> None:
    receipt.pop("receipt_sha256", None)
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(payload).hexdigest()


class M810CollaborationNotificationBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_dual_session_benchmark_meets_delivery_replay_and_authority_gates(self) -> None:
        receipt = benchmark_collaboration_notifications(
            root=self.root,
            event_count=20,
            campaign_steps=16,
            generated_at="2026-08-17T13:30:00+08:00",
        )

        validate_collaboration_notification_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        self.assertEqual(receipt["rates"]["dual_session_match_rate"], 1.0)
        self.assertEqual(receipt["rates"]["offline_catch_up_rate"], 1.0)
        self.assertEqual(receipt["rates"]["duplicate_suppression_rate"], 1.0)
        self.assertEqual(receipt["rates"]["signed_event_validation_rate"], 1.0)
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["external_services"], 0)
        self.assertEqual(receipt["campaign_scale"]["observed_complexity"], "quadratic")
        self.assertEqual(
            receipt["campaign_scale"]["adoption_decision"],
            "adopt-append-only-step-index",
        )

    def test_validator_rejects_forged_delivery_success(self) -> None:
        receipt = benchmark_collaboration_notifications(
            root=self.root,
            event_count=4,
            campaign_steps=4,
            generated_at="2026-08-17T13:30:00+08:00",
        )
        forged = copy.deepcopy(receipt)
        forged["consistent_dual_session_events"] -= 1
        forged["rates"]["dual_session_match_rate"] = 1.0
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "dual session|receipt_sha256"):
            validate_collaboration_notification_benchmark(forged, root=self.root)

    def test_validator_rejects_false_scale_conclusion(self) -> None:
        receipt = benchmark_collaboration_notifications(
            root=self.root,
            event_count=4,
            campaign_steps=4,
            generated_at="2026-08-17T13:30:00+08:00",
        )
        forged = copy.deepcopy(receipt)
        forged["campaign_scale"]["validation_attempts"] = 4
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "campaign scale"):
            validate_collaboration_notification_benchmark(forged, root=self.root)

    def test_validator_rejects_resigned_veto_and_structural_forgery(self) -> None:
        receipt = benchmark_collaboration_notifications(
            root=self.root,
            event_count=4,
            campaign_steps=4,
            generated_at="2026-08-17T13:30:00+08:00",
        )
        mutations = {
            "sse": lambda item: item.__setitem__("deterministic_sse_matches", 0),
            "provider": lambda item: item.__setitem__("provider_invocations", 1),
            "external": lambda item: item.__setitem__("external_services", 1),
            "count": lambda item: item.__setitem__("published_events", 5),
            "latency": lambda item: item["latency_ms"]["publish"].__setitem__(
                "p95_ms", -1.0
            ),
            "identity": lambda item: item.__setitem__(
                "benchmark_id", "benchmark-m8-10-" + "0" * 24
            ),
            "timestamp": lambda item: item.__setitem__("generated_at", "invalid"),
        }
        for mutation, mutate in mutations.items():
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _resign(forged)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                ValueError,
                "benchmark",
            ):
                validate_collaboration_notification_benchmark(forged, root=self.root)

    def test_cli_writes_a_reproducible_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        self.root
                        / "tools/run_collaboration_notification_benchmark.py"
                    ),
                    "--root",
                    str(self.root),
                    "--event-count",
                    "4",
                    "--campaign-steps",
                    "4",
                    "--generated-at",
                    "2026-08-17T13:30:00+08:00",
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_collaboration_notification_benchmark(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
