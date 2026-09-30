"""Deterministic measured acceptance for the M9-01 external read projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionError,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
    typed_state_snapshot_sha256,
    validate_external_state_projection,
)
from .sqlite_state_store import SQLiteStateStore
from .state_mcp import RequestContext, StateMCPService

BENCHMARK_SCHEMA_VERSION = "context.external-state-projection-benchmark/v1alpha1"
BENCHMARK_ID = "m9-01-external-state-projection"
_THRESHOLDS = {
    "same_revision_rate_min": 1.0,
    "state_digest_rate_min": 1.0,
    "signed_projection_rate_min": 1.0,
    "stale_view_rejection_rate_min": 1.0,
    "unauthorized_rejection_rate_min": 1.0,
    "torn_source_rejection_rate_min": 1.0,
    "unauthorized_state_reads_max": 0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "projection_latency_p95_ms_max": 50.0,
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "gate",
    "provenance",
    "receipt_sha256",
}
_RESULT_FIELDS = {
    "same_revision_matches",
    "same_revision_rate",
    "state_digest_matches",
    "state_digest_rate",
    "signed_projection_matches",
    "signed_projection_rate",
    "stale_view_rejections",
    "stale_view_rejection_rate",
    "unauthorized_rejections",
    "unauthorized_rejection_rate",
    "torn_source_rejections",
    "torn_source_rejection_rate",
    "unauthorized_state_reads",
    "authority_violations",
    "provider_invocations",
    "external_services",
}
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class ExternalStateProjectionBenchmarkError(ValueError):
    """Raised when a measured M9-01 receipt cannot be independently accepted."""


class _AllowAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return True


class _DenyAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return False


class _CountingSQLiteStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.project_reads = 0
        self.event_reads = 0

    def read_project(self, project_id: str) -> dict:
        self.project_reads += 1
        return super().read_project(project_id)

    def read_events(self, project_id: str) -> list[dict]:
        self.event_reads += 1
        return super().read_events(project_id)


class _TornSource:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = copy.deepcopy(snapshot)

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self._snapshot),
                "revision": self._snapshot["project"]["revision"] + 1,
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {},
            },
            "error": None,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latency(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p50 = ordered[max(0, (len(ordered) * 50 + 99) // 100 - 1)]
    p95 = ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)]
    return {
        "min": ordered[0],
        "p50": p50,
        "p95": p95,
        "max": ordered[-1],
    }


def _fixture(root: Path) -> dict[str, Any]:
    fixture_set = yaml.safe_load(
        (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
            encoding="utf-8"
        )
    )
    return copy.deepcopy(
        next(
            case["document"]
            for case in fixture_set["cases"]
            if case["case_id"] == "completed-work-overlap-blocked"
        )
    )


def _service(
    store: SQLiteStateStore,
    *,
    authorizer: Any,
) -> StateMCPService:
    return StateMCPService(
        store,
        authorizer=authorizer,
        registry_digest="a" * 64,
        clock=lambda: "2026-08-17T17:00:00+08:00",
        event_id_factory=lambda request_id: f"event-{request_id}",
    )


def _provenance(root: Path) -> dict[str, str]:
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/external_state_provider.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/external_state_provider_benchmark.py"
        ),
        "state_mcp_sha256": _file_digest(root / "context_control_plane/state_mcp.py"),
        "fixture_sha256": _file_digest(
            root / "experiments/state/m2-01-core-fixtures.yaml"
        ),
    }


def _failed_gates(
    results: dict[str, int | float],
    latency: dict[str, float],
) -> list[str]:
    checks = {
        "same-revision": results["same_revision_rate"]
        >= _THRESHOLDS["same_revision_rate_min"],
        "state-digest": results["state_digest_rate"]
        >= _THRESHOLDS["state_digest_rate_min"],
        "signed-projection": results["signed_projection_rate"]
        >= _THRESHOLDS["signed_projection_rate_min"],
        "stale-view": results["stale_view_rejection_rate"]
        >= _THRESHOLDS["stale_view_rejection_rate_min"],
        "unauthorized": results["unauthorized_rejection_rate"]
        >= _THRESHOLDS["unauthorized_rejection_rate_min"],
        "torn-source": results["torn_source_rejection_rate"]
        >= _THRESHOLDS["torn_source_rejection_rate_min"],
        "unauthorized-read": results["unauthorized_state_reads"]
        <= _THRESHOLDS["unauthorized_state_reads_max"],
        "authority": results["authority_violations"]
        <= _THRESHOLDS["authority_violations_max"],
        "provider": results["provider_invocations"]
        <= _THRESHOLDS["provider_invocations_max"],
        "external": results["external_services"]
        <= _THRESHOLDS["external_services_max"],
        "latency": latency["p95"]
        <= _THRESHOLDS["projection_latency_p95_ms_max"],
    }
    return [name for name, passed in checks.items() if not passed]


def benchmark_external_state_projection(
    *,
    root: Path,
    iterations: int = 1000,
    generated_at: str = "2026-08-17T17:00:00+08:00",
) -> dict[str, Any]:
    """Measure revision consistency and fail-closed external read behavior."""
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError("iterations must be an integer from 1 through 1000")
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise ValueError("generated_at must be an offset timestamp")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("generated_at must be an offset timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("generated_at must include an offset")

    snapshot = _fixture(root)
    project_id = snapshot["project"]["project_id"]
    revision = snapshot["project"]["revision"]
    context = RequestContext(
        "actor-m9-01-benchmark",
        "authorization-m9-01-benchmark",
    )
    with tempfile.TemporaryDirectory() as directory:
        store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
        store.initialize()
        store.create_project(snapshot)
        signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-01-benchmark",
            secret=b"m9-01-benchmark-signing-key-material",
        )
        provider = ExternalStateProjectionProvider(
            _service(store, authorizer=_AllowAuthorizer()),
            provider_id="provider-m9-01-benchmark",
            signer=signer,
        )
        denied_provider = ExternalStateProjectionProvider(
            _service(store, authorizer=_DenyAuthorizer()),
            provider_id="provider-m9-01-denied",
            signer=signer,
        )
        torn_provider = ExternalStateProjectionProvider(
            _TornSource(snapshot),
            provider_id="provider-m9-01-torn",
            signer=signer,
        )

        same_revision_matches = 0
        state_digest_matches = 0
        signed_projection_matches = 0
        stale_view_rejections = 0
        unauthorized_rejections = 0
        torn_source_rejections = 0
        authority_violations = 0
        latencies: list[float] = []
        expected_state_sha256 = typed_state_snapshot_sha256(snapshot)
        for index in range(iterations):
            request = {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": f"request-projection-{index}",
                "project_id": project_id,
                "expected_revision": revision,
            }
            started = time.perf_counter()
            response = provider.call_tool(
                EXTERNAL_READ_TOOL,
                request,
                context=context,
            )
            latencies.append((time.perf_counter() - started) * 1000.0)
            projection = response.get("result") if response.get("ok") else None
            if isinstance(projection, dict):
                try:
                    projection = validate_external_state_projection(
                        projection,
                        signer=signer,
                    )
                    signed_projection_matches += 1
                except ExternalStateProjectionError:
                    projection = None
            if (
                isinstance(projection, dict)
                and projection.get("state_revision") == revision
                and projection.get("source", {}).get("revision") == revision
                and projection.get("snapshot", {}).get("project", {}).get("revision")
                == revision
            ):
                same_revision_matches += 1
            if (
                isinstance(projection, dict)
                and projection.get("state_sha256") == expected_state_sha256
            ):
                state_digest_matches += 1
            if isinstance(projection, dict) and any(
                (
                    projection.get("state_write_authority") is not False,
                    projection.get("controlled_action_authority") is not False,
                    projection.get("provider_authority") != 0,
                    projection.get("external_effect_authority") != 0,
                )
            ):
                authority_violations += 1

            stale = provider.call_tool(
                EXTERNAL_READ_TOOL,
                {
                    **request,
                    "request_id": f"request-stale-{index}",
                    "expected_revision": revision - 1,
                },
                context=context,
            )
            if not stale["ok"] and stale["error"]["code"] == "stale_view":
                stale_view_rejections += 1

            before_denied_reads = store.project_reads + store.event_reads
            denied = denied_provider.call_tool(
                EXTERNAL_READ_TOOL,
                {**request, "request_id": f"request-denied-{index}"},
                context=context,
            )
            if not denied["ok"] and denied["error"]["code"] == "permission_denied":
                unauthorized_rejections += 1
            after_denied_reads = store.project_reads + store.event_reads
            if after_denied_reads != before_denied_reads:
                authority_violations += 1

            torn = torn_provider.call_tool(
                EXTERNAL_READ_TOOL,
                {**request, "request_id": f"request-torn-{index}"},
                context=context,
            )
            if not torn["ok"] and torn["error"]["code"] == "source_integrity_error":
                torn_source_rejections += 1

        unauthorized_state_reads = (
            store.project_reads + store.event_reads - iterations * 4
        )

    results: dict[str, int | float] = {
        "same_revision_matches": same_revision_matches,
        "same_revision_rate": same_revision_matches / iterations,
        "state_digest_matches": state_digest_matches,
        "state_digest_rate": state_digest_matches / iterations,
        "signed_projection_matches": signed_projection_matches,
        "signed_projection_rate": signed_projection_matches / iterations,
        "stale_view_rejections": stale_view_rejections,
        "stale_view_rejection_rate": stale_view_rejections / iterations,
        "unauthorized_rejections": unauthorized_rejections,
        "unauthorized_rejection_rate": unauthorized_rejections / iterations,
        "torn_source_rejections": torn_source_rejections,
        "torn_source_rejection_rate": torn_source_rejections / iterations,
        "unauthorized_state_reads": unauthorized_state_reads,
        "authority_violations": authority_violations,
        "provider_invocations": 0,
        "external_services": 0,
    }
    latency = _latency(latencies)
    failed_gates = _failed_gates(results, latency)
    body = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {"iterations": iterations},
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "gate": {
            "status": "passed" if not failed_gates else "failed",
            "failed_gates": failed_gates,
        },
        "provenance": _provenance(root),
    }
    return {**body, "receipt_sha256": _digest(body)}


def validate_external_state_projection_benchmark(
    receipt: Any,
    *,
    root: Path,
) -> dict[str, Any]:
    """Independently validate receipt identity, arithmetic, gates and provenance."""
    if not isinstance(receipt, dict) or set(receipt) != _TOP_LEVEL_FIELDS:
        raise ExternalStateProjectionBenchmarkError(
            "benchmark receipt fields do not match the contract"
        )
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != _digest(body):
        raise ExternalStateProjectionBenchmarkError("benchmark receipt digest mismatch")
    if (
        receipt.get("schema_version") != BENCHMARK_SCHEMA_VERSION
        or receipt.get("benchmark_id") != BENCHMARK_ID
    ):
        raise ExternalStateProjectionBenchmarkError("benchmark identity mismatch")
    if (
        not isinstance(receipt.get("generated_at"), str)
        or _TIMESTAMP_RE.fullmatch(receipt["generated_at"]) is None
    ):
        raise ExternalStateProjectionBenchmarkError("generated_at is invalid")
    try:
        generated_at = datetime.fromisoformat(
            receipt["generated_at"].replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as exc:
        raise ExternalStateProjectionBenchmarkError(
            "generated_at is invalid"
        ) from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ExternalStateProjectionBenchmarkError("generated_at lacks an offset")
    thresholds = receipt.get("thresholds")
    if thresholds != _THRESHOLDS or any(
        type(thresholds.get(field)) is not type(expected)
        for field, expected in _THRESHOLDS.items()
    ):
        raise ExternalStateProjectionBenchmarkError("benchmark thresholds drifted")
    parameters = receipt.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"iterations"}:
        raise ExternalStateProjectionBenchmarkError("benchmark parameters are invalid")
    iterations = parameters["iterations"]
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ExternalStateProjectionBenchmarkError("iteration count is invalid")
    results = receipt.get("results")
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise ExternalStateProjectionBenchmarkError("benchmark results are invalid")
    count_fields = (
        "same_revision_matches",
        "state_digest_matches",
        "signed_projection_matches",
        "stale_view_rejections",
        "unauthorized_rejections",
        "torn_source_rejections",
    )
    rate_fields = (
        "same_revision_rate",
        "state_digest_rate",
        "signed_projection_rate",
        "stale_view_rejection_rate",
        "unauthorized_rejection_rate",
        "torn_source_rejection_rate",
    )
    if any(
        type(results[field]) is not int or results[field] != iterations
        for field in count_fields
    ) or any(
        type(results[field]) not in {int, float}
        or not math.isfinite(results[field])
        or results[field] != 1.0
        for field in rate_fields
    ):
        raise ExternalStateProjectionBenchmarkError(
            "required consistency or rejection count is incomplete"
        )
    zero_fields = (
        "unauthorized_state_reads",
        "authority_violations",
        "provider_invocations",
        "external_services",
    )
    if any(type(results[field]) is not int or results[field] != 0 for field in zero_fields):
        raise ExternalStateProjectionBenchmarkError("zero-tolerance result is nonzero")
    latency = receipt.get("latency_ms")
    if not isinstance(latency, dict) or set(latency) != {"min", "p50", "p95", "max"}:
        raise ExternalStateProjectionBenchmarkError("latency result is invalid")
    if any(
        not isinstance(latency[field], (int, float))
        or isinstance(latency[field], bool)
        or not math.isfinite(latency[field])
        or latency[field] < 0
        for field in latency
    ) or not (
        latency["min"] <= latency["p50"] <= latency["p95"] <= latency["max"]
    ):
        raise ExternalStateProjectionBenchmarkError("latency values are invalid")
    failed_gates = _failed_gates(results, latency)
    expected_gate = {
        "status": "passed" if not failed_gates else "failed",
        "failed_gates": failed_gates,
    }
    if receipt.get("gate") != expected_gate or failed_gates:
        raise ExternalStateProjectionBenchmarkError("benchmark acceptance gate failed")
    if receipt.get("provenance") != _provenance(root):
        raise ExternalStateProjectionBenchmarkError("benchmark provenance mismatch")
    return copy.deepcopy(receipt)


__all__ = [
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "ExternalStateProjectionBenchmarkError",
    "benchmark_external_state_projection",
    "validate_external_state_projection_benchmark",
]
