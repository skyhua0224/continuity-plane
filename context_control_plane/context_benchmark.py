"""Deterministic E0/E1 context composition benchmark.

This harness measures a provider-independent token proxy and state recovery
under an explicit lossy compaction model. It does not claim to model a
provider tokenizer or model reasoning; live provider A/B belongs to E0-E3.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


CRITICAL_FIELDS = (
    "active_task",
    "latest_decision",
    "constraint",
    "return_point",
    "blocker",
    "next_action",
)


@dataclass(frozen=True)
class Scenario:
    case_id: str
    active_task: str
    latest_decision: str
    constraint: str
    return_point: str
    blocker: str
    next_action: str
    stale_decision: str
    history_events: tuple[str, ...]
    skill_rules: tuple[str, ...]
    relevant_rules: tuple[str, ...]


def synthetic_scenarios() -> tuple[Scenario, ...]:
    """Return sanitized, synthetic cases for local E0/E1 measurements."""
    common_events = tuple(
        f"historical observation {index}: retained only as background candidate; verify current evidence before use"
        for index in range(1, 13)
    )
    common_rules = tuple(
        f"rule-{index}: provider-neutral workflow guidance with bounded evidence request and replay logging"
        for index in range(1, 9)
    )
    return (
        Scenario(
            case_id="synthetic-altp-ecn",
            active_task="M1-04-build-ecn-replay-fixtures",
            latest_decision="retain ECN result as candidate until current packet evidence is verified",
            constraint="do not import transcript正文 or create fixture without sanitizer and provenance",
            return_point="M1-04.extract.case-01",
            blocker="controlled archive byte range is not yet approved",
            next_action="prepare synthetic fixture envelope and validator inputs",
            stale_decision="rejected decision: import the complete chat transcript into Git",
            history_events=common_events,
            skill_rules=common_rules,
            relevant_rules=(common_rules[0], common_rules[2]),
        ),
        Scenario(
            case_id="synthetic-task-switch",
            active_task="M3-03-restore-parent-after-idea",
            latest_decision="park the new graph idea and continue the active restore leaf",
            constraint="a new idea cannot change active state without a checkpoint and switch proposal",
            return_point="M3-03.resume.parent-leaf",
            blocker="none; waiting for deterministic canary implementation",
            next_action="record idea candidate and emit capture-and-continue event",
            stale_decision="rejected decision: switch immediately to the graph experiment",
            history_events=common_events,
            skill_rules=common_rules,
            relevant_rules=(common_rules[1], common_rules[4]),
        ),
        Scenario(
            case_id="synthetic-skill-drift",
            active_task="M4-03-quarantine-stale-skill",
            latest_decision="quarantine the skill when content hash or expiry changes",
            constraint="stale rule activation must remain zero even when memory ranks it highly",
            return_point="M4-03.replay.hash-mismatch",
            blocker="the replacement manifest lacks an approved provenance ref",
            next_action="emit quarantine event and request explicit migration",
            stale_decision="rejected decision: keep using the cached skill without replay",
            history_events=common_events,
            skill_rules=common_rules,
            relevant_rules=(common_rules[3], common_rules[6]),
        ),
        Scenario(
            case_id="synthetic-collaboration-cas",
            active_task="M8-02-claim-path-owner",
            latest_decision="reject the write when expected revision no longer matches",
            constraint="all external effects require a valid claim and path owner",
            return_point="M8-02.retry.after-cas-conflict",
            blocker="second collaborator still holds the prior lease",
            next_action="refresh state and re-claim only after lease validation",
            stale_decision="rejected decision: overwrite the branch after a CAS conflict",
            history_events=common_events,
            skill_rules=common_rules,
            relevant_rules=(common_rules[5], common_rules[7]),
        ),
    )


def replay_fixture_scenarios(fixtures: Iterable[dict[str, Any]]) -> tuple[Scenario, ...]:
    """Convert validated replay fixtures into provider-independent benchmark cases."""
    skill_rules = (
        "state-restore: recover current typed fields before considering historical context",
        "evidence-gate: current evidence is required for a load-bearing assertion",
        "stale-decision: rejected and reverted decisions cannot become active work",
        "task-routing: preserve the active leaf and return point without switch authority",
        "experiment-gate: experimental findings require explicit promotion approval",
        "scope-gate: side effects require a valid claim and approved path ownership",
        "sanitizer-gate: raw archive content cannot enter Git or the execution packet",
        "verification-gate: completion requires the project Verification Profile evidence",
    )
    scenario_rule_indexes = {
        "compaction-recovery": (0, 2),
        "task-routing": (0, 3),
        "stale-decision": (0, 2),
        "evidence-gap": (1, 7),
        "scope-drift": (3, 5),
        "experiment-promotion": (4, 5),
    }
    scenarios: list[Scenario] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("replay fixture must be an object")
        try:
            expected = fixture["expected_state"]
            initial = fixture["initial_state"]
            input_event = fixture["input_event"]
            scenario_class = fixture["scenario_class"]
            rule_indexes = scenario_rule_indexes[scenario_class]
            constraints = expected["constraints"]
        except (KeyError, TypeError) as exc:
            raise ValueError("replay fixture cannot form a benchmark scenario") from exc
        if not isinstance(constraints, list) or not constraints:
            raise ValueError("expected_state.constraints must be non-empty")
        scenarios.append(
            Scenario(
                case_id=fixture["fixture_id"],
                active_task=expected["active_task"],
                latest_decision=expected["latest_decision"],
                constraint=" | ".join(constraints),
                return_point=expected["return_point"],
                blocker=expected["blocker"] or "none",
                next_action=expected["next_action"],
                stale_decision=initial["latest_decision"],
                history_events=(
                    f"source_event={input_event['content']}",
                    f"historical_task={initial['active_task']}",
                    f"historical_next_action={initial['next_action']}",
                ),
                skill_rules=skill_rules,
                relevant_rules=tuple(skill_rules[index] for index in rule_indexes),
            )
        )
    return tuple(scenarios)


def estimate_token_proxy(text: str) -> int:
    """Estimate tokens as ceil(UTF-8 bytes / 4); provider billing is future work."""
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / 4)


def build_baseline_context(scenario: Scenario) -> str:
    """Build the full-history/free-summary input used as the E0 baseline."""
    lines = [
        "BASELINE_CONTEXT_BEGIN",
        f"active_task={scenario.active_task}",
        f"latest_decision={scenario.latest_decision}",
        f"constraint={scenario.constraint}",
        f"return_point={scenario.return_point}",
        f"blocker={scenario.blocker}",
        f"next_action={scenario.next_action}",
    ]
    lines.extend(f"history={event}" for event in scenario.history_events)
    lines.extend(f"skill={rule}" for rule in scenario.skill_rules)
    lines.append(f"stale={scenario.stale_decision}")
    lines.append("BASELINE_CONTEXT_END")
    return "\n".join(lines)


def build_execution_packet(scenario: Scenario) -> str:
    """Build the bounded E1 packet from typed current state and relevant rules."""
    lines = [
        "EXECUTION_PACKET_BEGIN",
        f"active_task={scenario.active_task}",
        f"latest_decision={scenario.latest_decision}",
        f"constraint={scenario.constraint}",
        f"return_point={scenario.return_point}",
        f"blocker={scenario.blocker}",
        f"next_action={scenario.next_action}",
    ]
    lines.extend(f"rule={rule}" for rule in scenario.relevant_rules)
    lines.append("EXECUTION_PACKET_END")
    return "\n".join(lines)


def compact_context(text: str, *, budget_chars: int) -> str:
    """Model a lossy compaction pass by retaining only the tail of the input."""
    if not isinstance(budget_chars, int) or budget_chars <= 0:
        raise ValueError("budget_chars must be a positive integer")
    if len(text) <= budget_chars:
        return text
    return text[-budget_chars:]


def recover_fields(text: str, scenario: Scenario) -> dict[str, bool]:
    """Check exact recovery of the six critical state fields."""
    expected = {
        "active_task": scenario.active_task,
        "latest_decision": scenario.latest_decision,
        "constraint": scenario.constraint,
        "return_point": scenario.return_point,
        "blocker": scenario.blocker,
        "next_action": scenario.next_action,
    }
    return {field: value in text for field, value in expected.items()}


def _percentage(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 4)


def run_benchmark(
    scenarios: Iterable[Scenario], *, budgets_chars: Iterable[int] = (512,)
) -> dict[str, object]:
    """Run deterministic baseline versus execution-packet measurements."""
    cases = tuple(scenarios)
    budgets = tuple(budgets_chars)
    if not cases:
        raise ValueError("at least one scenario is required")
    if not budgets:
        raise ValueError("at least one compaction budget is required")

    results: list[dict[str, object]] = []
    aggregates: dict[str, dict[str, object]] = {}
    for budget in budgets:
        budget_results: list[dict[str, object]] = []
        for scenario in cases:
            baseline = compact_context(
                build_baseline_context(scenario), budget_chars=budget
            )
            enhanced = compact_context(
                build_execution_packet(scenario), budget_chars=budget
            )
            baseline_recovery = recover_fields(baseline, scenario)
            enhanced_recovery = recover_fields(enhanced, scenario)
            baseline_tokens = estimate_token_proxy(baseline)
            enhanced_tokens = estimate_token_proxy(enhanced)
            baseline_skill_tokens = estimate_token_proxy(
                "\n".join(scenario.skill_rules)
            )
            enhanced_skill_tokens = estimate_token_proxy(
                "\n".join(scenario.relevant_rules)
            )
            row = {
                "case_id": scenario.case_id,
                "budget_chars": budget,
                "baseline_tokens": baseline_tokens,
                "enhanced_tokens": enhanced_tokens,
                "token_reduction_pct": _percentage(
                    baseline_tokens - enhanced_tokens, baseline_tokens
                ),
                "baseline_skill_tokens": baseline_skill_tokens,
                "enhanced_skill_tokens": enhanced_skill_tokens,
                "skill_input_reduction_pct": _percentage(
                    baseline_skill_tokens - enhanced_skill_tokens, baseline_skill_tokens
                ),
                "baseline_recovery_rate": round(
                    sum(baseline_recovery.values()) / len(CRITICAL_FIELDS), 4
                ),
                "enhanced_recovery_rate": round(
                    sum(enhanced_recovery.values()) / len(CRITICAL_FIELDS), 4
                ),
                "baseline_stale_decision_revived": int(
                    scenario.stale_decision in baseline
                    and scenario.latest_decision not in baseline
                ),
                "enhanced_stale_decision_revived": int(
                    scenario.stale_decision in enhanced
                    and scenario.latest_decision not in enhanced
                ),
            }
            budget_results.append(row)
            results.append(row)

        def average(key: str) -> float:
            return round(sum(float(row[key]) for row in budget_results) / len(budget_results), 4)

        aggregates[str(budget)] = {
            "case_count": len(budget_results),
            "baseline_recovery_rate": average("baseline_recovery_rate"),
            "enhanced_recovery_rate": average("enhanced_recovery_rate"),
            "token_reduction_pct": average("token_reduction_pct"),
            "skill_input_reduction_pct": average("skill_input_reduction_pct"),
            "baseline_stale_decision_revived": sum(
                int(row["baseline_stale_decision_revived"]) for row in budget_results
            ),
            "enhanced_stale_decision_revived": sum(
                int(row["enhanced_stale_decision_revived"]) for row in budget_results
            ),
        }

    return {
        "schema_version": "e0-e1.context-benchmark.v1",
        "token_estimator": "ceil(utf8_bytes/4)",
        "compaction_model": "tail-retention",
        "budgets_chars": list(budgets),
        "cases": results,
        "aggregate": aggregates,
    }
