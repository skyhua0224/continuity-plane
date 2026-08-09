"""Repeatable latency and footprint benchmark for the embedded SQLite store."""

from __future__ import annotations

import copy
import hashlib
import math
import platform
import shlex
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .sqlite_state_store import SQLiteStateStore
from .state_events import build_state_event


_RECEIPT_SCHEMA_VERSION = "context.sqlite-state-store-results/v1alpha1"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "observed_at",
        "provenance",
        "environment",
        "latency_ms",
        "footprint_bytes",
        "stream_scaling",
        "live_platform_gates",
        "authority_boundary",
        "comparison_to_postgresql",
        "limitations",
        "references",
        "generation",
    }
)
_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/sqlite_state_store.py",
    "benchmark_sha256": "context_control_plane/sqlite_benchmark.py",
    "runner_sha256": "tools/run_sqlite_benchmark.py",
    "fixture_sha256": "experiments/state/m2-01-core-fixtures.yaml",
    "postgres_evidence_sha256": "experiments/state/m2-03-postgres-cas-results.yaml",
    "fault_evidence_sha256": "tests/test_m2_09_sqlite_state_store.py",
}
_REFERENCE_METADATA = [
    {
        "source_id": "sqlite-wal",
        "url": "https://www.sqlite.org/wal.html",
        "retrieved_at": "2026-08-10T03:35:16+08:00",
        "content_sha256": "29315aaa998c5e2acf29e3ac161e9e8bf786d0c82a85ac83eb4f4091234acb7b",
    },
    {
        "source_id": "sqlite-transactions",
        "url": "https://www.sqlite.org/lang_transaction.html",
        "retrieved_at": "2026-08-10T03:35:16+08:00",
        "content_sha256": "b65dc308fd9e0ce471844c97366a4f5ad3a1f42833a3b19486ad7d6555a8e24e",
    },
    {
        "source_id": "sqlite-pragmas",
        "url": "https://www.sqlite.org/pragma.html",
        "retrieved_at": "2026-08-10T03:35:16+08:00",
        "content_sha256": "b9bcb335ae818497f3fa05114a10492f64f35503f275da2264f2d5d436db3f5d",
    },
    {
        "source_id": "sqlite-corruption",
        "url": "https://www.sqlite.org/howtocorrupt.html",
        "retrieved_at": "2026-08-10T03:35:16+08:00",
        "content_sha256": "e00709e3f37e95332d8e6df243510665e9cab1d7938d4fd85cbf5f47648a50bf",
    },
]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(root: Path) -> dict[str, str]:
    return {
        field: _file_sha256(root / relative_path)
        for field, relative_path in _PROVENANCE_PATHS.items()
    }


def _platform_gates(current_platform: str) -> dict[str, str]:
    current = {
        "linux": "linux",
        "darwin": "macos",
        "win32": "windows",
    }.get(current_platform)
    return {
        platform_id: "passed" if platform_id == current else "blocked"
        for platform_id in ("linux", "windows", "macos")
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _candidate(
    initial: dict[str, Any],
    sample: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = copy.deepcopy(initial)
    expected["project"]["revision"] = initial["project"]["revision"] + 1
    expected["project"]["updated_at"] = "2026-08-10T03:30:00+08:00"
    idea = {
        "idea_id": f"idea-sqlite-benchmark-{sample}",
        "parent_work_id": "work-repeat",
        "source_ref": f"opaque://sqlite-benchmark/{sample}",
        "summary": f"SQLite benchmark candidate {sample}.",
        "status": "parked",
        "return_work_id": "work-repeat",
        "expiry": None,
        "attempt_budget": None,
        "promotion_target": "M2-09",
        "evidence_ids": [],
    }
    expected["ideas"].append(idea)
    event = build_state_event(
        event_id=f"event-sqlite-benchmark-{sample}",
        event_type="state-transition",
        project_id=initial["project"]["project_id"],
        sequence_no=1,
        revision_before=initial["project"]["revision"],
        occurred_at="2026-08-10T03:30:00+08:00",
        actor_ref="actor-sqlite-benchmark",
        causation_ref="work:M2-09",
        correlation_ref="benchmark:m2-09",
        previous_event_sha256=None,
        supersedes_event_id=None,
        changes=[
            {
                "collection": "ideas",
                "object_id": idea["idea_id"],
                "value": idea,
            }
        ],
        project_after=expected["project"],
    )
    return event, expected


def _stream_candidate(
    current: dict[str, Any],
    sequence_no: int,
    previous_event_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = copy.deepcopy(current)
    expected["project"]["revision"] = current["project"]["revision"] + 1
    expected["project"]["updated_at"] = "2026-08-10T03:50:00+08:00"
    idea = {
        "idea_id": f"idea-sqlite-stream-{sequence_no}",
        "parent_work_id": "work-repeat",
        "source_ref": f"opaque://sqlite-stream/{sequence_no}",
        "summary": f"SQLite stream candidate {sequence_no}.",
        "status": "parked",
        "return_work_id": "work-repeat",
        "expiry": None,
        "attempt_budget": None,
        "promotion_target": "M2-09",
        "evidence_ids": [],
    }
    expected["ideas"].append(idea)
    event = build_state_event(
        event_id=f"event-sqlite-stream-{sequence_no}",
        event_type="state-transition",
        project_id=current["project"]["project_id"],
        sequence_no=sequence_no,
        revision_before=current["project"]["revision"],
        occurred_at="2026-08-10T03:50:00+08:00",
        actor_ref="actor-sqlite-stream-benchmark",
        causation_ref="work:M2-09",
        correlation_ref="benchmark:m2-09-stream",
        previous_event_sha256=previous_event_sha256,
        supersedes_event_id=None,
        changes=[
            {
                "collection": "ideas",
                "object_id": idea["idea_id"],
                "value": idea,
            }
        ],
        project_after=expected["project"],
    )
    return event, expected


def run_sqlite_benchmark(
    database_path: str | Path,
    base_snapshot: dict[str, Any],
    *,
    samples: int = 40,
) -> dict[str, Any]:
    """Measure the full validated commit/read path using independent projects."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    database_path = Path(database_path)
    if database_path.exists():
        raise ValueError("benchmark database path must not already exist")

    store = SQLiteStateStore(database_path)
    store.initialize()
    commit_latencies: list[float] = []
    read_latencies: list[float] = []

    for sample in range(samples):
        initial = copy.deepcopy(base_snapshot)
        initial["project"]["project_id"] = f"project-sqlite-benchmark-{sample}"
        event, expected = _candidate(initial, sample)
        store.create_project(initial)

        started = time.perf_counter_ns()
        store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=initial["project"]["revision"],
            event=event,
            expected_snapshot=expected,
        )
        commit_latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        started = time.perf_counter_ns()
        restored = store.read_project(initial["project"]["project_id"])
        read_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        if restored != expected:
            raise RuntimeError("benchmark read does not match committed state")

    with store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    sidecars = {
        "wal": Path(f"{database_path}-wal"),
        "shared_memory": Path(f"{database_path}-shm"),
    }
    commit_p95 = _percentile(commit_latencies, 0.95)
    read_p95 = _percentile(read_latencies, 0.95)
    return {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "sqlite_version": sqlite3.sqlite_version,
            "fixture": "completed-work-overlap-blocked",
            "connection_strategy": "one-new-connection-per-operation",
            "journal_mode": "wal",
            "synchronous": "full",
            "external_services": 0,
        },
        "latency_ms": {
            "samples": samples,
            "commit_p50": _percentile(commit_latencies, 0.50),
            "commit_p95": commit_p95,
            "commit_max": round(max(commit_latencies), 4),
            "read_p50": _percentile(read_latencies, 0.50),
            "read_p95": read_p95,
            "read_max": round(max(read_latencies), 4),
            "state_only_restore_p95": read_p95,
        },
        "footprint_bytes": {
            "database": database_path.stat().st_size,
            "wal": sidecars["wal"].stat().st_size if sidecars["wal"].exists() else 0,
            "shared_memory": (
                sidecars["shared_memory"].stat().st_size
                if sidecars["shared_memory"].exists()
                else 0
            ),
        },
        "authority_boundary": {
            "typed_state_authority": True,
            "append_only_event_authority": True,
            "shared_authority": False,
            "vector_index_authority": False,
        },
    }


def run_sqlite_stream_benchmark(
    database_path: str | Path,
    base_snapshot: dict[str, Any],
    *,
    events: int,
) -> dict[str, Any]:
    """Measure repeated commits and final restore for one growing Event stream."""
    if type(events) is not int or events <= 0:
        raise ValueError("events must be a positive integer")
    database_path = Path(database_path)
    if database_path.exists():
        raise ValueError("benchmark database path must not already exist")

    current = copy.deepcopy(base_snapshot)
    current["project"]["project_id"] = "project-sqlite-stream-benchmark"
    store = SQLiteStateStore(database_path)
    store.initialize()
    store.create_project(current)
    commit_latencies: list[float] = []
    milestones: dict[str, dict[str, float]] = {}
    previous_event_sha256: str | None = None
    total_started = time.perf_counter_ns()

    for sequence_no in range(1, events + 1):
        event, expected = _stream_candidate(
            current,
            sequence_no,
            previous_event_sha256,
        )
        started = time.perf_counter_ns()
        store.commit_event(
            project_id=event["project_id"],
            expected_revision=event["revision_before"],
            event=event,
            expected_snapshot=expected,
        )
        latency = (time.perf_counter_ns() - started) / 1_000_000
        commit_latencies.append(latency)
        current = expected
        previous_event_sha256 = event["event_sha256"]
        if sequence_no in {1, 100, events}:
            milestones[str(sequence_no)] = {
                "last_commit_ms": round(latency, 4),
                "cumulative_commit_ms": round(sum(commit_latencies), 4),
            }

    total_wall_ms = (time.perf_counter_ns() - total_started) / 1_000_000
    started = time.perf_counter_ns()
    restored = store.read_project(current["project"]["project_id"])
    final_restore_ms = (time.perf_counter_ns() - started) / 1_000_000
    if restored != current:
        raise RuntimeError("stream benchmark restore does not match committed state")
    with store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return {
        "schema_version": "context.sqlite-stream-results/v1alpha1",
        "events": events,
        "initial_revision": base_snapshot["project"]["revision"],
        "final_revision": current["project"]["revision"],
        "latency_ms": {
            "commit_p50": _percentile(commit_latencies, 0.50),
            "commit_p95": _percentile(commit_latencies, 0.95),
            "commit_max": round(max(commit_latencies), 4),
            "total_commit": round(sum(commit_latencies), 4),
            "total_wall": round(total_wall_ms, 4),
            "final_restore": round(final_restore_ms, 4),
        },
        "milestones": milestones,
        "footprint_bytes": {
            "database": database_path.stat().st_size,
        },
    }


def build_sqlite_benchmark_receipt(
    *,
    root: Path,
    benchmark: dict[str, Any],
    stream: dict[str, Any],
    postgres: dict[str, Any],
    observed_at: str,
    arguments: list[str],
) -> dict[str, Any]:
    """Build the complete canonical receipt emitted by the benchmark CLI."""
    root = root.resolve()
    postgres_latency = postgres["latency_ms"]
    sqlite_latency = benchmark["latency_ms"]
    platform_gates = _platform_gates(benchmark["environment"]["platform"])
    authority = dict(benchmark["authority_boundary"])
    authority.update(
        {
            "unique_claim_authority": False,
            "checkpoint_canary_authority": False,
            "cross_backend_migration_authority": False,
        }
    )
    receipt = {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "observed_at": observed_at,
        "provenance": _provenance(root),
        "environment": benchmark["environment"],
        "latency_ms": benchmark["latency_ms"],
        "footprint_bytes": benchmark["footprint_bytes"],
        "stream_scaling": stream,
        "live_platform_gates": platform_gates,
        "authority_boundary": authority,
        "comparison_to_postgresql": {
            "postgres_evidence": "experiments/state/m2-03-postgres-cas-results.yaml",
            "same_fixture": benchmark["environment"]["fixture"]
            == postgres["environment"]["fixture"],
            "same_samples": sqlite_latency["samples"]
            == postgres_latency["samples"],
            "postgres_samples": postgres_latency["samples"],
            "commit_p95_reduction_percent": round(
                (postgres_latency["commit_p95"] - sqlite_latency["commit_p95"])
                / postgres_latency["commit_p95"]
                * 100,
                4,
            ),
            "read_p95_reduction_percent": round(
                (postgres_latency["read_p95"] - sqlite_latency["read_p95"])
                / postgres_latency["read_p95"]
                * 100,
                4,
            ),
        },
        "limitations": [
            "Results describe one host and do not predict other devices.",
            "Native platform gates require one receipt from each operating system.",
            "The PostgreSQL comparison uses one Event per project.",
            "The stream benchmark has no PostgreSQL counterpart.",
            "SQLite local authority does not provide shared claims or leases.",
            "Checkpoint canary and cross-backend migration remain separate work.",
        ],
        "references": copy.deepcopy(_REFERENCE_METADATA),
        "generation": {
            "command": shlex.join(
                [
                    ".venv/bin/python",
                    "tools/run_sqlite_benchmark.py",
                    *arguments,
                ]
            ),
            "arguments": list(arguments),
            "writes_runtime_state_to_repository": False,
        },
    }
    validate_sqlite_benchmark_receipt(receipt, root=root)
    return receipt


def validate_sqlite_benchmark_receipt(
    receipt: dict[str, Any],
    *,
    root: Path,
) -> None:
    """Reject stale provenance, structural drift and invalid acceptance claims."""
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_FIELDS:
        raise ValueError("SQLite benchmark receipt fields do not match the canonical schema")
    if receipt.get("schema_version") != _RECEIPT_SCHEMA_VERSION:
        raise ValueError("SQLite benchmark receipt schema version is unsupported")
    if not isinstance(receipt.get("observed_at"), str) or not receipt["observed_at"]:
        raise ValueError("SQLite benchmark receipt observed_at is missing")
    if receipt.get("provenance") != _provenance(root.resolve()):
        raise ValueError("SQLite benchmark receipt provenance is stale")
    latency = receipt.get("latency_ms")
    if not isinstance(latency, dict) or latency.get("samples", 0) <= 0:
        raise ValueError("SQLite benchmark receipt has invalid sample metadata")
    if latency.get("state_only_restore_p95", 2_000) >= 2_000:
        raise ValueError("SQLite benchmark receipt violates the restore latency gate")
    stream = receipt.get("stream_scaling")
    if not isinstance(stream, dict) or stream.get("events", 0) <= 0:
        raise ValueError("SQLite benchmark receipt has invalid stream metadata")
    if stream.get("schema_version") != "context.sqlite-stream-results/v1alpha1":
        raise ValueError("SQLite stream benchmark schema version is unsupported")
    if stream.get("final_revision") != stream.get("initial_revision") + stream["events"]:
        raise ValueError("SQLite stream benchmark revision range is invalid")
    gates = receipt.get("live_platform_gates")
    if not isinstance(gates, dict) or set(gates) != {"linux", "windows", "macos"}:
        raise ValueError("SQLite benchmark receipt has invalid platform gates")
    if set(gates.values()) - {"passed", "blocked"}:
        raise ValueError("SQLite benchmark receipt has an invalid platform status")
    references = receipt.get("references")
    if references != _REFERENCE_METADATA:
        raise ValueError("SQLite benchmark receipt references are stale")
    authority = receipt.get("authority_boundary")
    for forbidden in (
        "shared_authority",
        "unique_claim_authority",
        "vector_index_authority",
        "checkpoint_canary_authority",
        "cross_backend_migration_authority",
    ):
        if not isinstance(authority, dict) or authority.get(forbidden) is not False:
            raise ValueError(f"SQLite benchmark receipt overclaims {forbidden}")
    generation = receipt.get("generation")
    if (
        not isinstance(generation, dict)
        or not isinstance(generation.get("arguments"), list)
        or generation.get("writes_runtime_state_to_repository") is not False
    ):
        raise ValueError("SQLite benchmark receipt has invalid generation metadata")
    expected_command = shlex.join(
        [
            ".venv/bin/python",
            "tools/run_sqlite_benchmark.py",
            *generation["arguments"],
        ]
    )
    if generation.get("command") != expected_command:
        raise ValueError("SQLite benchmark receipt command does not match its arguments")

    import yaml

    postgres = yaml.safe_load(
        (root / "experiments/state/m2-03-postgres-cas-results.yaml").read_text(
            encoding="utf-8"
        )
    )
    comparison = receipt.get("comparison_to_postgresql")
    postgres_latency = postgres["latency_ms"]
    expected_comparison = {
        "postgres_evidence": "experiments/state/m2-03-postgres-cas-results.yaml",
        "same_fixture": receipt["environment"]["fixture"]
        == postgres["environment"]["fixture"],
        "same_samples": latency["samples"] == postgres_latency["samples"],
        "postgres_samples": postgres_latency["samples"],
        "commit_p95_reduction_percent": round(
            (postgres_latency["commit_p95"] - latency["commit_p95"])
            / postgres_latency["commit_p95"]
            * 100,
            4,
        ),
        "read_p95_reduction_percent": round(
            (postgres_latency["read_p95"] - latency["read_p95"])
            / postgres_latency["read_p95"]
            * 100,
            4,
        ),
    }
    if comparison != expected_comparison:
        raise ValueError("SQLite benchmark receipt comparison is inconsistent")
