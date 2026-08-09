import json
import unittest
from pathlib import Path

from context_control_plane.context_benchmark import (
    CRITICAL_FIELDS,
    build_baseline_context,
    build_execution_packet,
    compact_context,
    recover_fields,
    replay_fixture_scenarios,
    run_benchmark,
    synthetic_scenarios,
)


class ContextBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.scenario = synthetic_scenarios()[0]

    def test_execution_packet_contains_current_state_and_relevant_rules_only(self):
        packet = build_execution_packet(self.scenario)

        recovered = recover_fields(packet, self.scenario)
        self.assertEqual(set(recovered), set(CRITICAL_FIELDS))
        self.assertTrue(all(recovered.values()))
        self.assertNotIn(self.scenario.stale_decision, packet)
        for irrelevant_rule in self.scenario.skill_rules:
            if irrelevant_rule not in self.scenario.relevant_rules:
                self.assertNotIn(irrelevant_rule, packet)

    def test_lossy_baseline_compaction_drops_current_state_and_can_revive_stale_decision(self):
        baseline = compact_context(build_baseline_context(self.scenario), budget_chars=512)
        recovered = recover_fields(baseline, self.scenario)

        self.assertLess(sum(recovered.values()), len(CRITICAL_FIELDS))
        self.assertTrue(self.scenario.stale_decision in baseline)
        self.assertNotIn(self.scenario.latest_decision, baseline)

    def test_benchmark_reports_quantified_recovery_and_token_reduction(self):
        report = run_benchmark(synthetic_scenarios(), budgets_chars=(768,))
        aggregate = report["aggregate"]["768"]

        self.assertEqual(aggregate["enhanced_recovery_rate"], 1.0)
        self.assertLess(aggregate["baseline_recovery_rate"], 1.0)
        self.assertGreater(aggregate["skill_input_reduction_pct"], 0.0)
        self.assertGreater(aggregate["token_reduction_pct"], 0.0)
        self.assertGreater(aggregate["baseline_stale_decision_revived"], 0)
        self.assertEqual(aggregate["enhanced_stale_decision_revived"], 0)

    def test_benchmark_report_is_json_serializable_and_deterministic(self):
        first = run_benchmark(synthetic_scenarios(), budgets_chars=(256, 768))
        second = run_benchmark(synthetic_scenarios(), budgets_chars=(256, 768))

        self.assertEqual(first, second)
        json.dumps(first, sort_keys=True)

    def test_real_replay_fixtures_form_a_forty_case_benchmark_corpus(self):
        fixture_dir = Path(__file__).parents[1] / "replay" / "fixtures"
        fixtures = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(fixture_dir.glob("fx_*.json"))
        ]

        scenarios = replay_fixture_scenarios(fixtures)
        report = run_benchmark(scenarios, budgets_chars=(2048,))
        aggregate = report["aggregate"]["2048"]

        self.assertEqual(len(scenarios), 40)
        self.assertEqual(len({scenario.case_id for scenario in scenarios}), 40)
        self.assertEqual(aggregate["enhanced_recovery_rate"], 1.0)
        self.assertLess(aggregate["baseline_recovery_rate"], 1.0)
        self.assertGreater(aggregate["baseline_stale_decision_revived"], 0)
        self.assertEqual(aggregate["enhanced_stale_decision_revived"], 0)


if __name__ == "__main__":
    unittest.main()
