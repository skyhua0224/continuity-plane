"""M5-05 provider-neutral context accounting contract."""

from __future__ import annotations

import copy
import hashlib
import unittest

from context_control_plane.context_accounting import (
    ContextAccountingError,
    canonical_context_accounting_bytes,
    compose_context_accounting,
    measured_metric,
    unavailable_metric,
    validate_context_accounting,
)


class M505ContextAccountingTests(unittest.TestCase):
    def _local_metrics(self, *, cut_point: int | None) -> dict:
        return {
            "input_tokens": unavailable_metric("tokens", "provider trace was not exported"),
            "output_tokens": unavailable_metric("tokens", "provider trace was not exported"),
            "cache_read_tokens": unavailable_metric("tokens", "provider trace was not exported"),
            "cache_write_tokens": unavailable_metric("tokens", "provider trace was not exported"),
            "billing_usd_micros": unavailable_metric(
                "usd_micros", "provider billing trace was not exported"
            ),
            "retrieval_queries": measured_metric(
                1, "count", source_kind="retrieval_receipt", evidence_ref="artifact://receipt/retrieval"
            ),
            "retrieval_read_bytes": measured_metric(
                4096, "bytes", source_kind="retrieval_receipt", evidence_ref="artifact://receipt/retrieval"
            ),
            "retrieval_output_bytes": measured_metric(
                768, "bytes", source_kind="retrieval_receipt", evidence_ref="artifact://receipt/retrieval"
            ),
            "retrieval_latency_ms": measured_metric(
                0.25, "milliseconds", source_kind="monotonic_clock", evidence_ref="run://m5-05/local"
            ),
            "compaction_latency_ms": measured_metric(
                0.5, "milliseconds", source_kind="monotonic_clock", evidence_ref="run://m5-05/local"
            ),
            "cut_point_tokens": (
                measured_metric(
                    cut_point,
                    "tokens",
                    source_kind="host_hook",
                    evidence_ref="hook://pi/compaction",
                )
                if cut_point is not None
                else unavailable_metric("tokens", "host exposes a semantic checkpoint without a token cut point")
            ),
            "cache_invalidated": unavailable_metric(
                "boolean", "provider cache trace was not exported"
            ),
        }

    def _routes(self) -> list[dict]:
        return [
            {
                "route_id": "route/pi",
                "provider_id": "pi",
                "route_threshold_tokens": 3072,
                "model_free_pruning": True,
                "metrics": self._local_metrics(cut_point=2048),
            },
            {
                "route_id": "route/deepseek",
                "provider_id": "deepseek",
                "route_threshold_tokens": 3584,
                "model_free_pruning": False,
                "metrics": self._local_metrics(cut_point=None),
            },
        ]

    def _receipt(self, *, routes: list[dict] | None = None, claim: bool = False) -> dict:
        return compose_context_accounting(
            accounting_id="accounting/m5-05/unit",
            corpus_id="corpus/m5-05/shared",
            corpus_sha256=hashlib.sha256(b"same corpus").hexdigest(),
            budget_tokens=4096,
            routes=routes or self._routes(),
            observed_at="2026-08-15T02:00:00+08:00",
            real_provider_improvement_claimed=claim,
        )

    def test_offline_accounting_keeps_provider_metrics_unavailable_without_false_claims(self):
        receipt = self._receipt()
        validate_context_accounting(receipt)
        self.assertEqual(receipt["provider_comparison_status"], "unavailable")
        self.assertFalse(receipt["real_provider_improvement_claimed"])
        self.assertFalse(receipt["byte_token_proxy_used"])
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["provider_native_authority"])
        self.assertEqual(
            {route["provider_id"] for route in receipt["routes"]}, {"pi", "deepseek"}
        )
        self.assertEqual(receipt["routes"][0]["route_threshold_tokens"], 3072)
        self.assertTrue(receipt["routes"][0]["model_free_pruning"])
        self.assertEqual(receipt["routes"][0]["metrics"]["input_tokens"]["status"], "unavailable")
        self.assertEqual(receipt["routes"][0]["metrics"]["retrieval_read_bytes"]["status"], "measured")

    def test_provider_tokens_cache_and_billing_require_provider_trace(self):
        source_cases = (
            ("input_tokens", 100, "tokens", "retrieval_receipt"),
            ("cache_read_tokens", 50, "tokens", "host_hook"),
            ("billing_usd_micros", 12, "usd_micros", "monotonic_clock"),
            ("cache_invalidated", True, "boolean", "host_hook"),
        )
        for field, value, unit, source_kind in source_cases:
            routes = self._routes()
            routes[0]["metrics"][field] = measured_metric(
                value,
                unit,
                source_kind=source_kind,
                evidence_ref="artifact://bytes/estimate",
            )
            with self.subTest(field=field), self.assertRaisesRegex(
                ContextAccountingError, "provider_trace"
            ):
                self._receipt(routes=routes)

    def test_unavailable_metric_cannot_carry_a_value_or_fake_evidence(self):
        receipt = self._receipt()
        changed = copy.deepcopy(receipt)
        metric = changed["routes"][0]["metrics"]["input_tokens"]
        metric["value"] = 1024
        with self.assertRaises(ContextAccountingError):
            validate_context_accounting(changed)
        changed = copy.deepcopy(receipt)
        metric = changed["routes"][0]["metrics"]["input_tokens"]
        metric["source_kind"] = "provider_trace"
        metric["evidence_ref"] = "trace://provider/fake"
        with self.assertRaises(ContextAccountingError):
            validate_context_accounting(changed)

    def test_real_provider_claim_requires_complete_trace_backed_metrics_for_every_route(self):
        with self.assertRaisesRegex(ContextAccountingError, "provider improvement"):
            self._receipt(claim=True)

        routes = self._routes()
        provider_values = {
            "input_tokens": (1000, "tokens"),
            "output_tokens": (200, "tokens"),
            "cache_read_tokens": (700, "tokens"),
            "cache_write_tokens": (300, "tokens"),
            "billing_usd_micros": (1200, "usd_micros"),
            "cache_invalidated": (False, "boolean"),
        }
        for route in routes:
            for field, (value, unit) in provider_values.items():
                route["metrics"][field] = measured_metric(
                    value,
                    unit,
                    source_kind="provider_trace",
                    evidence_ref=f"trace://{route['provider_id']}/run-1",
                )
        receipt = self._receipt(routes=routes, claim=True)
        self.assertEqual(receipt["provider_comparison_status"], "measured")
        self.assertTrue(receipt["real_provider_improvement_claimed"])

    def test_cut_point_and_cache_invalidation_are_independently_accounted(self):
        receipt = self._receipt()
        pi = receipt["routes"][0]["metrics"]
        deepseek = receipt["routes"][1]["metrics"]
        self.assertEqual(pi["cut_point_tokens"]["value"], 2048)
        self.assertEqual(pi["cut_point_tokens"]["source_kind"], "host_hook")
        self.assertEqual(deepseek["cut_point_tokens"]["status"], "unavailable")
        self.assertEqual(pi["cache_invalidated"]["status"], "unavailable")

    def test_receipt_is_strict_deterministic_and_rejects_incomparable_routes(self):
        first = self._receipt()
        second = self._receipt()
        self.assertEqual(canonical_context_accounting_bytes(first), canonical_context_accounting_bytes(second))

        changed = copy.deepcopy(first)
        changed["byte_token_proxy_used"] = True
        with self.assertRaisesRegex(ContextAccountingError, "byte"):
            validate_context_accounting(changed)
        duplicate = self._routes()
        duplicate[1]["route_id"] = duplicate[0]["route_id"]
        with self.assertRaisesRegex(ContextAccountingError, "route_id"):
            self._receipt(routes=duplicate)
        over_budget = self._routes()
        over_budget[0]["route_threshold_tokens"] = 4097
        with self.assertRaisesRegex(ContextAccountingError, "threshold"):
            self._receipt(routes=over_budget)

    def test_numeric_latency_must_be_finite(self):
        routes = self._routes()
        routes[0]["metrics"]["retrieval_latency_ms"]["value"] = float("nan")
        with self.assertRaisesRegex(ContextAccountingError, "value"):
            self._receipt(routes=routes)

    def test_malformed_route_fails_as_accounting_error(self):
        with self.assertRaises(ContextAccountingError):
            self._receipt(routes=[{"provider_id": "pi"}, {"provider_id": "deepseek"}])


if __name__ == "__main__":
    unittest.main()
