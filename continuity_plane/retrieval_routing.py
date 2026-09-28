"""Deterministic, bounded retrieval planning and evidence receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

PLAN_SCHEMA_VERSION = "context.retrieval-plan/v1alpha1"
RECEIPT_SCHEMA_VERSION = "context.retrieval-receipt/v1alpha1"
PLANNER_VERSION = "context.retrieval-router/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_RECEIPT_REF_RE = re.compile(r"^receipt://sha256/[0-9a-f]{64}$")
_ARTIFACT_REF_RE = re.compile(r"^artifact://sha256/([0-9a-f]{64})$")
_REPO_BYTES_REF_RE = re.compile(
    r"^repo://(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]{0,255})/"
    r"(?P<path>[^#]+)#bytes=(?P<offset>[0-9]+):(?P<length>[1-9][0-9]*)$"
)
_TOOLS = ("rg", "zoekt", "lsp", "scip", "rtfm")
_QUESTION_ROUTES = {
    "exact_text": ("rg",),
    "large_corpus_text": ("rg", "zoekt"),
    "symbol_definition": ("rg", "lsp"),
    "cross_repository_impact": ("rg", "lsp", "scip"),
    "official_reference": ("rtfm",),
}
_REQUIRED_TOOLS = {
    "symbol_definition": {"rg", "lsp"},
    "cross_repository_impact": {"rg", "lsp", "scip"},
    "official_reference": {"rtfm"},
}
_PURPOSES = {
    "rg": "exact current-code evidence",
    "zoekt": "large-corpus candidate narrowing",
    "lsp": "current symbol definition and reference verification",
    "scip": "cross-language and cross-repository relationship verification",
    "rtfm": "versioned official reference evidence",
}
_PLAN_FIELDS = {
    "schema_version",
    "planner_version",
    "plan_id",
    "question_id",
    "question_kind",
    "query_sha256",
    "repositories",
    "steps",
    "budgets",
    "freshness",
    "degraded_reasons",
    "state_write_authority",
    "memory_authority",
    "plan_sha256",
}
_STEP_FIELDS = {
    "tool",
    "purpose",
    "max_queries",
    "max_scanned_bytes",
    "max_returned_bytes",
    "requires_index",
}
_BUDGET_FIELDS = {"max_queries", "max_scanned_bytes", "max_returned_bytes"}
_FRESHNESS_FIELDS = {"max_index_age_seconds", "require_content_hash"}
_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "plan_id",
    "plan_sha256",
    "query_sha256",
    "executed_at",
    "cache_status",
    "prior_receipt_ref",
    "step_results",
    "max_index_age_seconds",
    "evidence",
    "evidence_count",
    "totals",
    "state_write_authority",
    "memory_authority",
    "receipt_sha256",
}
_RESULT_FIELDS = {
    "tool",
    "queries",
    "scanned_bytes",
    "returned_bytes",
    "index_revision",
    "index_sha256",
    "index_age_seconds",
}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "source_kind",
    "source_ref",
    "revision",
    "sha256",
    "range",
    "retrieved_at",
    "valid_at",
}
_RANGE_FIELDS = {"offset_bytes", "length_bytes"}
_TOTAL_FIELDS = {"queries", "scanned_bytes", "returned_bytes"}


class RetrievalContractError(ValueError):
    """Raised when a retrieval plan or receipt violates the contract."""


class RetrievalBudgetError(RetrievalContractError):
    """Raised when execution exceeds an admitted retrieval budget."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: dict[str, Any], digest_field: str) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop(digest_field, None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise RetrievalContractError(f"{field} is invalid")
    return value


def _bounded_text(value: Any, field: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise RetrievalContractError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RetrievalContractError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise RetrievalContractError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrievalContractError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise RetrievalContractError(f"{field} requires a timezone")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise RetrievalContractError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise RetrievalContractError(f"{field} must be a non-negative integer")
    return value


def _allocate(total: int, parts: int) -> list[int]:
    if total < parts:
        raise RetrievalBudgetError("budget cannot allocate a positive amount to every step")
    base, remainder = divmod(total, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _validate_question(question: Any) -> tuple[str, str, str, list[str], bool]:
    fields = {"question_id", "kind", "query", "repositories", "freshness_required"}
    if not isinstance(question, dict) or set(question) != fields:
        raise RetrievalContractError("question fields are invalid")
    question_id = _safe_id(question["question_id"], "question_id")
    kind = question["kind"]
    if kind not in _QUESTION_ROUTES:
        raise RetrievalContractError("question kind is unsupported")
    query = _bounded_text(question["query"], "query", 4096)
    repositories = question["repositories"]
    if (
        not isinstance(repositories, list)
        or not repositories
        or len(repositories) > 32
        or len(set(repositories)) != len(repositories)
    ):
        raise RetrievalContractError("repositories are invalid")
    for index, repository in enumerate(repositories):
        _safe_id(repository, f"repositories[{index}]")
    if type(question["freshness_required"]) is not bool:
        raise RetrievalContractError("freshness_required is invalid")
    return question_id, kind, query, repositories, question["freshness_required"]


def plan_retrieval(
    question: dict[str, Any],
    *,
    available_tools: set[str],
    max_queries: int,
    max_scanned_bytes: int,
    max_returned_bytes: int,
    max_index_age_seconds: int,
) -> dict[str, Any]:
    """Select the smallest admitted route and split its explicit budget."""
    question_id, kind, query, repositories, freshness_required = _validate_question(
        question
    )
    if not isinstance(available_tools, set) or not available_tools <= set(_TOOLS):
        raise RetrievalContractError("available_tools are invalid")
    max_queries = _positive_int(max_queries, "max_queries")
    max_scanned_bytes = _positive_int(max_scanned_bytes, "max_scanned_bytes")
    max_returned_bytes = _positive_int(max_returned_bytes, "max_returned_bytes")
    max_index_age_seconds = _positive_int(
        max_index_age_seconds, "max_index_age_seconds"
    )

    route = list(_QUESTION_ROUTES[kind])
    degraded_reasons: list[str] = []
    if kind == "large_corpus_text" and "zoekt" not in available_tools:
        route.remove("zoekt")
        degraded_reasons.append("zoekt_unavailable")
    required = _REQUIRED_TOOLS.get(kind, set())
    for tool in sorted(required):
        if tool not in available_tools:
            raise RetrievalContractError(f"required tool {tool} is unavailable")
    if route[0] not in available_tools:
        raise RetrievalContractError(f"required tool {route[0]} is unavailable")

    query_budgets = _allocate(max_queries, len(route))
    scanned_budgets = _allocate(max_scanned_bytes, len(route))
    returned_budgets = _allocate(max_returned_bytes, len(route))
    steps = [
        {
            "tool": tool,
            "purpose": _PURPOSES[tool],
            "max_queries": query_budgets[index],
            "max_scanned_bytes": scanned_budgets[index],
            "max_returned_bytes": returned_budgets[index],
            "requires_index": tool != "rg",
        }
        for index, tool in enumerate(route)
    ]
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": f"plan/{question_id}",
        "question_id": question_id,
        "question_kind": kind,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "repositories": list(repositories),
        "steps": steps,
        "budgets": {
            "max_queries": max_queries,
            "max_scanned_bytes": max_scanned_bytes,
            "max_returned_bytes": max_returned_bytes,
        },
        "freshness": {
            "max_index_age_seconds": max_index_age_seconds,
            "require_content_hash": freshness_required,
        },
        "degraded_reasons": degraded_reasons,
        "state_write_authority": False,
        "memory_authority": False,
        "plan_sha256": "",
    }
    plan["plan_sha256"] = _digest(plan, "plan_sha256")
    validate_retrieval_plan(plan)
    return plan


def validate_retrieval_plan(plan: Any) -> None:
    """Validate a strict retrieval plan and its deterministic digest."""
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise RetrievalContractError("retrieval plan fields are invalid")
    if plan["schema_version"] != PLAN_SCHEMA_VERSION:
        raise RetrievalContractError("retrieval plan schema_version is invalid")
    if plan["planner_version"] != PLANNER_VERSION:
        raise RetrievalContractError("retrieval planner_version is invalid")
    _safe_id(plan["plan_id"], "plan_id")
    _safe_id(plan["question_id"], "question_id")
    if plan["plan_id"] != f"plan/{plan['question_id']}":
        raise RetrievalContractError("plan_id does not bind question_id")
    if plan["question_kind"] not in _QUESTION_ROUTES:
        raise RetrievalContractError("question_kind is invalid")
    _sha256(plan["query_sha256"], "query_sha256")
    repositories = plan["repositories"]
    if not isinstance(repositories, list) or not repositories or len(set(repositories)) != len(repositories):
        raise RetrievalContractError("repositories are invalid")
    for index, repository in enumerate(repositories):
        _safe_id(repository, f"repositories[{index}]")

    budgets = plan["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != _BUDGET_FIELDS:
        raise RetrievalContractError("budgets are invalid")
    for field, value in budgets.items():
        _positive_int(value, f"budgets.{field}")
    freshness = plan["freshness"]
    if not isinstance(freshness, dict) or set(freshness) != _FRESHNESS_FIELDS:
        raise RetrievalContractError("freshness is invalid")
    _positive_int(freshness["max_index_age_seconds"], "max_index_age_seconds")
    if type(freshness["require_content_hash"]) is not bool:
        raise RetrievalContractError("require_content_hash is invalid")
    reasons = plan["degraded_reasons"]
    if not isinstance(reasons, list) or len(set(reasons)) != len(reasons):
        raise RetrievalContractError("degraded_reasons are invalid")
    for index, reason in enumerate(reasons):
        _safe_id(reason, f"degraded_reasons[{index}]")

    steps = plan["steps"]
    if not isinstance(steps, list) or not steps or len(steps) > len(_TOOLS):
        raise RetrievalContractError("retrieval steps are invalid")
    tools: list[str] = []
    sums = {field: 0 for field in _BUDGET_FIELDS}
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != _STEP_FIELDS:
            raise RetrievalContractError(f"retrieval step {index} fields are invalid")
        tool = step["tool"]
        if tool not in _TOOLS or tool in tools:
            raise RetrievalContractError("retrieval tools are invalid")
        tools.append(tool)
        purpose = _bounded_text(step["purpose"], f"steps[{index}].purpose")
        if purpose != _PURPOSES[tool]:
            raise RetrievalContractError("retrieval step purpose does not match its tool")
        for field in _BUDGET_FIELDS:
            value = _positive_int(step[field], f"steps[{index}].{field}")
            sums[field] += value
        if step["requires_index"] is not (tool != "rg"):
            raise RetrievalContractError("requires_index does not match its tool")
    if tools != sorted(tools, key=_TOOLS.index):
        raise RetrievalContractError("retrieval tools violate escalation order")
    expected_route = list(_QUESTION_ROUTES[plan["question_kind"]])
    expected_reasons: list[str] = []
    if plan["question_kind"] == "large_corpus_text" and tools == ["rg"]:
        expected_route = ["rg"]
        expected_reasons = ["zoekt_unavailable"]
    if tools != expected_route:
        raise RetrievalContractError("retrieval route does not match question_kind")
    if reasons != expected_reasons:
        raise RetrievalContractError("degraded reasons do not match retrieval route")
    if sums != budgets:
        raise RetrievalContractError("step budgets do not equal plan budgets")
    if plan["state_write_authority"] is not False or plan["memory_authority"] is not False:
        raise RetrievalContractError("retrieval plans cannot grant authority")
    _sha256(plan["plan_sha256"], "plan_sha256")
    if plan["plan_sha256"] != _digest(plan, "plan_sha256"):
        raise RetrievalContractError("retrieval plan digest mismatch")


def _validate_evidence(item: Any, index: int) -> None:
    if not isinstance(item, dict) or set(item) != _EVIDENCE_FIELDS:
        raise RetrievalContractError(f"evidence {index} fields are invalid")
    _safe_id(item["evidence_id"], f"evidence[{index}].evidence_id")
    if item["source_kind"] not in {"current_code", "official_reference", "current_index"}:
        raise RetrievalContractError("evidence source_kind is invalid")
    _bounded_text(item["source_ref"], f"evidence[{index}].source_ref", 2048)
    _bounded_text(item["revision"], f"evidence[{index}].revision", 512)
    _sha256(item["sha256"], f"evidence[{index}].sha256")
    byte_range = item["range"]
    if not isinstance(byte_range, dict) or set(byte_range) != _RANGE_FIELDS:
        raise RetrievalContractError("evidence range is invalid")
    _nonnegative_int(byte_range["offset_bytes"], "range.offset_bytes")
    _positive_int(byte_range["length_bytes"], "range.length_bytes")
    _timestamp(item["retrieved_at"], "retrieved_at")
    _timestamp(item["valid_at"], "valid_at")


def _validate_evidence_payload(
    item: dict[str, Any],
    payload: bytes,
    *,
    expected_offset: int | None = None,
    expected_length: int | None = None,
    source: str,
) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    if item["sha256"] != digest:
        raise RetrievalContractError(f"{source} evidence does not match current file hash")
    byte_range = item["range"]
    offset = byte_range["offset_bytes"]
    length = byte_range["length_bytes"]
    if expected_offset is not None and (
        offset != expected_offset or length != expected_length
    ):
        raise RetrievalContractError(f"{source} evidence range does not match source_ref")
    if offset > len(payload) or length > len(payload) - offset:
        raise RetrievalContractError(f"{source} evidence range is outside the payload")


def _validate_current_evidence(
    item: dict[str, Any],
    *,
    root: str | Path | None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None] | None,
) -> None:
    source_ref = item["source_ref"]
    if root is not None and item["source_kind"] == "current_code":
        match = _REPO_BYTES_REF_RE.fullmatch(source_ref)
        if match is None:
            raise RetrievalContractError(
                "current_code evidence requires a repo byte-range source_ref"
            )
        repository_root = Path(root).resolve()
        if match.group("repository") != repository_root.name:
            raise RetrievalContractError("current_code evidence repository is invalid")
        relative = Path(match.group("path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise RetrievalContractError("current_code evidence path is invalid")
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root)
        except ValueError as exc:
            raise RetrievalContractError("current_code evidence path escapes root") from exc
        if not path.is_file():
            raise RetrievalContractError("current_code evidence file is missing")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RetrievalContractError("current_code evidence file is unreadable") from exc
        _validate_evidence_payload(
            item,
            payload,
            expected_offset=int(match.group("offset")),
            expected_length=int(match.group("length")),
            source="current file",
        )
        return

    artifact_match = _ARTIFACT_REF_RE.fullmatch(source_ref)
    if artifact_resolver is None or artifact_match is None:
        return
    try:
        payload = artifact_resolver(source_ref)
    except Exception as exc:
        raise RetrievalContractError("artifact evidence resolver failed") from exc
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise RetrievalContractError("artifact evidence is missing or invalid")
    payload = bytes(payload)
    if hashlib.sha256(payload).hexdigest() != artifact_match.group(1):
        raise RetrievalContractError("artifact evidence digest does not match its URI")
    _validate_evidence_payload(item, payload, source="artifact")


def _validate_trusted_plan(receipt: dict[str, Any], plan: dict[str, Any]) -> None:
    validate_retrieval_plan(plan)
    expected = {
        "receipt_id": f"receipt/{plan['question_id']}",
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "query_sha256": plan["query_sha256"],
        "max_index_age_seconds": plan["freshness"]["max_index_age_seconds"],
    }
    if any(receipt[field] != value for field, value in expected.items()):
        raise RetrievalContractError("receipt does not match the trusted plan identity")
    results = receipt["step_results"]
    if receipt["cache_status"] == "miss":
        if [result["tool"] for result in results] != [
            step["tool"] for step in plan["steps"]
        ]:
            raise RetrievalContractError("receipt route does not match the trusted plan")
        for result, step in zip(results, plan["steps"]):
            if (
                result["queries"] > step["max_queries"]
                or result["scanned_bytes"] > step["max_scanned_bytes"]
                or result["returned_bytes"] > step["max_returned_bytes"]
            ):
                raise RetrievalContractError("receipt exceeds a trusted plan step budget")
            has_index = (
                result["index_revision"] is not None
                and result["index_sha256"] is not None
            )
            if has_index is not step["requires_index"]:
                raise RetrievalContractError(
                    "receipt index metadata does not match the trusted plan"
                )
    for total_field, budget_field in (
        ("queries", "max_queries"),
        ("scanned_bytes", "max_scanned_bytes"),
        ("returned_bytes", "max_returned_bytes"),
    ):
        if receipt["totals"][total_field] > plan["budgets"][budget_field]:
            raise RetrievalContractError("receipt exceeds a trusted plan total budget")


def _validate_cache_lineage(
    receipt: dict[str, Any],
    *,
    trusted_plan: dict[str, Any] | None,
    prior_receipt_resolver: Callable[[str], dict[str, Any] | None] | None,
    root: str | Path | None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None,
) -> None:
    if receipt["cache_status"] != "hit":
        return
    if prior_receipt_resolver is None:
        raise RetrievalContractError("cache hit requires a prior receipt resolver")
    prior_ref = receipt["prior_receipt_ref"]
    try:
        prior = prior_receipt_resolver(prior_ref)
    except Exception as exc:
        raise RetrievalContractError("prior receipt resolver failed") from exc
    if not isinstance(prior, dict):
        raise RetrievalContractError("prior receipt is missing or invalid")
    if prior.get("cache_status") != "miss":
        raise RetrievalContractError("cache lineage must resolve to a cache miss")
    validate_retrieval_receipt(
        prior,
        trusted_plan=trusted_plan,
        root=root,
        artifact_resolver=artifact_resolver,
    )
    expected_ref = f"receipt://sha256/{prior['receipt_sha256']}"
    if prior_ref != expected_ref:
        raise RetrievalContractError("cache lineage reference does not match prior receipt")
    identity_fields = (
        "receipt_id",
        "plan_id",
        "plan_sha256",
        "query_sha256",
        "max_index_age_seconds",
        "evidence",
        "evidence_count",
    )
    if any(receipt[field] != prior[field] for field in identity_fields):
        raise RetrievalContractError("cache lineage identity or evidence does not match")
    if _timestamp(prior["executed_at"], "prior.executed_at") > _timestamp(
        receipt["executed_at"], "executed_at"
    ):
        raise RetrievalContractError("cache lineage cannot reference a future receipt")


def compose_retrieval_receipt(
    *,
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    step_results: list[dict[str, Any]],
    executed_at: str,
    cache_status: str,
    prior_receipt_ref: str | None,
    prior_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind bounded execution metrics and current evidence to an admitted plan."""
    validate_retrieval_plan(plan)
    if not isinstance(evidence, list) or not evidence or len(evidence) > 256:
        raise RetrievalContractError("evidence must be a bounded non-empty list")
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
    if len({item["evidence_id"] for item in evidence}) != len(evidence):
        raise RetrievalContractError("evidence IDs must be unique")
    _timestamp(executed_at, "executed_at")
    if cache_status not in {"miss", "hit"}:
        raise RetrievalContractError("cache_status is invalid")
    if cache_status == "hit":
        if prior_receipt_ref is None:
            raise RetrievalContractError("cache hit requires a prior receipt")
        if (
            not isinstance(prior_receipt_ref, str)
            or _RECEIPT_REF_RE.fullmatch(prior_receipt_ref) is None
        ):
            raise RetrievalContractError(
                "prior_receipt_ref must be a content-addressed receipt"
            )
        if step_results:
            raise RetrievalContractError("cache hit cannot hide new reads")
    elif prior_receipt_ref is not None:
        raise RetrievalContractError("cache miss cannot bind a prior receipt")
    elif prior_receipt is not None:
        raise RetrievalContractError("cache miss cannot carry a prior receipt")

    plan_tools = [step["tool"] for step in plan["steps"]]
    if cache_status == "miss" and [result.get("tool") for result in step_results] != plan_tools:
        raise RetrievalContractError("step results do not match the admitted route")
    totals = {"queries": 0, "scanned_bytes": 0, "returned_bytes": 0}
    normalized_results: list[dict[str, Any]] = []
    for index, result in enumerate(step_results):
        if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
            raise RetrievalContractError(f"step result {index} fields are invalid")
        tool = result["tool"]
        if tool not in _TOOLS:
            raise RetrievalContractError("step result tool is invalid")
        queries = _positive_int(result["queries"], "result.queries")
        scanned = _nonnegative_int(result["scanned_bytes"], "result.scanned_bytes")
        returned = _nonnegative_int(result["returned_bytes"], "result.returned_bytes")
        age = _nonnegative_int(result["index_age_seconds"], "result.index_age_seconds")
        step = plan["steps"][index]
        if queries > step["max_queries"]:
            raise RetrievalBudgetError("retrieval query budget exceeded")
        if scanned > step["max_scanned_bytes"]:
            raise RetrievalBudgetError("retrieval scan budget exceeded")
        if returned > step["max_returned_bytes"]:
            raise RetrievalBudgetError("retrieval return budget exceeded")
        if step["requires_index"]:
            _bounded_text(result["index_revision"], "index_revision", 512)
            _sha256(result["index_sha256"], "index_sha256")
            if age > plan["freshness"]["max_index_age_seconds"]:
                raise RetrievalContractError("retrieval index is stale")
        elif any(value is not None for value in (result["index_revision"], result["index_sha256"])) or age != 0:
            raise RetrievalContractError("non-index step carries index metadata")
        totals["queries"] += queries
        totals["scanned_bytes"] += scanned
        totals["returned_bytes"] += returned
        normalized_results.append(copy.deepcopy(result))
    for total_field, budget_field in (
        ("queries", "max_queries"),
        ("scanned_bytes", "max_scanned_bytes"),
        ("returned_bytes", "max_returned_bytes"),
    ):
        if totals[total_field] > plan["budgets"][budget_field]:
            raise RetrievalBudgetError(f"total {total_field} budget exceeded")

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"receipt/{plan['question_id']}",
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "query_sha256": plan["query_sha256"],
        "executed_at": executed_at,
        "cache_status": cache_status,
        "prior_receipt_ref": prior_receipt_ref,
        "step_results": normalized_results,
        "max_index_age_seconds": plan["freshness"]["max_index_age_seconds"],
        "evidence": copy.deepcopy(evidence),
        "evidence_count": len(evidence),
        "totals": totals,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    prior_resolver = None
    if prior_receipt is not None:
        prior_resolver = (
            lambda ref: prior_receipt if ref == prior_receipt_ref else None
        )
    validate_retrieval_receipt(
        receipt,
        trusted_plan=plan,
        prior_receipt_resolver=prior_resolver,
    )
    return receipt


def validate_retrieval_receipt(
    receipt: Any,
    *,
    trusted_plan: dict[str, Any] | None = None,
    root: str | Path | None = None,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None = None,
    prior_receipt_resolver: Callable[
        [str], dict[str, Any] | None
    ]
    | None = None,
) -> None:
    """Validate strict receipt accounting and provenance without replaying tools."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise RetrievalContractError("retrieval receipt fields are invalid")
    if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise RetrievalContractError("retrieval receipt schema_version is invalid")
    _safe_id(receipt["receipt_id"], "receipt_id")
    _safe_id(receipt["plan_id"], "plan_id")
    _sha256(receipt["plan_sha256"], "plan_sha256")
    _sha256(receipt["query_sha256"], "query_sha256")
    executed_at = _timestamp(receipt["executed_at"], "executed_at")
    if receipt["cache_status"] not in {"miss", "hit"}:
        raise RetrievalContractError("cache_status is invalid")
    prior = receipt["prior_receipt_ref"]
    if receipt["cache_status"] == "hit":
        if not isinstance(prior, str) or _RECEIPT_REF_RE.fullmatch(prior) is None:
            raise RetrievalContractError(
                "prior_receipt_ref must be a content-addressed receipt"
            )
        if receipt["step_results"]:
            raise RetrievalContractError("cache hit cannot contain new reads")
    elif prior is not None:
        raise RetrievalContractError("cache miss cannot bind a prior receipt")
    results = receipt["step_results"]
    if not isinstance(results, list) or len(results) > len(_TOOLS):
        raise RetrievalContractError("step_results are invalid")
    max_index_age_seconds = _positive_int(
        receipt["max_index_age_seconds"], "max_index_age_seconds"
    )
    calculated = {"queries": 0, "scanned_bytes": 0, "returned_bytes": 0}
    tools: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
            raise RetrievalContractError(f"step result {index} fields are invalid")
        tool = result["tool"]
        if tool not in _TOOLS or tool in tools:
            raise RetrievalContractError("step result tools are invalid")
        tools.append(tool)
        for field in _TOTAL_FIELDS:
            value = result[{"queries": "queries", "scanned_bytes": "scanned_bytes", "returned_bytes": "returned_bytes"}[field]]
            if field == "queries":
                _positive_int(value, field)
            else:
                _nonnegative_int(value, field)
            calculated[field] += value
        age = _nonnegative_int(result["index_age_seconds"], "index_age_seconds")
        if result["index_revision"] is None or result["index_sha256"] is None:
            if result["index_revision"] is not None or result["index_sha256"] is not None or age != 0:
                raise RetrievalContractError("partial index metadata is invalid")
        else:
            _bounded_text(result["index_revision"], "index_revision", 512)
            _sha256(result["index_sha256"], "index_sha256")
            if age > max_index_age_seconds:
                raise RetrievalContractError("retrieval index is stale")
    evidence = receipt["evidence"]
    if not isinstance(evidence, list) or not evidence or len(evidence) > 256:
        raise RetrievalContractError("evidence is invalid")
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
        if (
            _timestamp(item["retrieved_at"], "retrieved_at") > executed_at
            or _timestamp(item["valid_at"], "valid_at") > executed_at
        ):
            raise RetrievalContractError("evidence timestamp is later than executed_at")
        _validate_current_evidence(
            item, root=root, artifact_resolver=artifact_resolver
        )
    evidence_ids = [item["evidence_id"] for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise RetrievalContractError("evidence IDs must be unique")
    if receipt["evidence_count"] != len(evidence):
        raise RetrievalContractError("evidence_count is inaccurate")
    totals = receipt["totals"]
    if not isinstance(totals, dict) or set(totals) != _TOTAL_FIELDS:
        raise RetrievalContractError("totals are invalid")
    if totals != calculated:
        raise RetrievalContractError("receipt totals are inaccurate")
    if receipt["state_write_authority"] is not False or receipt["memory_authority"] is not False:
        raise RetrievalContractError("retrieval receipts cannot grant authority")
    if trusted_plan is not None:
        _validate_trusted_plan(receipt, trusted_plan)
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _digest(receipt, "receipt_sha256"):
        raise RetrievalContractError("retrieval receipt digest mismatch")
    _validate_cache_lineage(
        receipt,
        trusted_plan=trusted_plan,
        prior_receipt_resolver=prior_receipt_resolver,
        root=root,
        artifact_resolver=artifact_resolver,
    )


def canonical_retrieval_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    validate_retrieval_receipt(receipt)
    return _canonical(receipt)
