"""Independent M3-02 sticky-router benchmark and counter computation."""

from __future__ import annotations

import copy
import statistics
import time
from typing import Any

from .sticky_router import canonical_route_decision_bytes, route_task_input


def benchmark_sticky_routes(
    requests: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any]:
    """Execute each request twice and return measured routing counters."""
    requests_before = copy.deepcopy(requests)
    state_before = copy.deepcopy(state)
    timings: list[float] = []
    active_leaf_preserved = 0
    deterministic_replays = 0
    unauthorized_switches = 0

    for request in requests:
        started = time.perf_counter_ns()
        first = route_task_input(request, state)
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        second = route_task_input(copy.deepcopy(request), copy.deepcopy(state))
        if first["active_work_id_after"] == request["active_work_id"]:
            active_leaf_preserved += 1
        if canonical_route_decision_bytes(first) == canonical_route_decision_bytes(second):
            deterministic_replays += 1
        if (
            request["input_kind"] in {"interrupt", "switch"}
            and not request["user_authorization_candidate"]
            and first["route"] == "propose-switch"
        ):
            unauthorized_switches += 1

    ordered = sorted(timings)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return {
        "sample_count": len(requests),
        "active_leaf_preserved": active_leaf_preserved,
        "deterministic_replays": deterministic_replays,
        "unauthorized_switches": unauthorized_switches,
        "requests_unchanged": requests == requests_before,
        "state_unchanged": state == state_before,
        "p50_route_latency_ms": round(statistics.median(timings), 6),
        "p95_route_latency_ms": round(ordered[p95_index], 6),
        "max_route_latency_ms": round(max(timings), 6),
    }
