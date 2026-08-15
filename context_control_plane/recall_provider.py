"""Provider-neutral candidate recall with fail-open state isolation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Protocol

SCHEMA_VERSION = "context.recall-receipt/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REQUEST_FIELDS = {
    "request_id",
    "task_id",
    "query",
    "max_candidates",
    "max_returned_bytes",
}
_RECORD_FIELDS = {
    "record_id",
    "content",
    "tags",
    "observed_at",
    "valid_until",
    "source_ref",
    "source_sha256",
}
_CANDIDATE_FIELDS = {
    "candidate_id",
    "content",
    "content_sha256",
    "observed_at",
    "valid_until",
    "freshness",
    "source_ref",
    "source_sha256",
    "active_state_authority",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "request_id",
    "task_id",
    "query_sha256",
    "max_candidates",
    "max_returned_bytes",
    "request_sha256",
    "provider_id",
    "status",
    "provider_error_code",
    "observed_at",
    "candidates",
    "returned_bytes",
    "state_write_authority",
    "provider_authority",
    "receipt_sha256",
}


class RecallContractError(ValueError):
    """Raised when recall input or output violates the candidate-only contract."""


class RecallProviderUnavailable(RuntimeError):
    """Raised by adapters when candidate recall is unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecallProvider(Protocol):
    provider_id: str

    def recall(self, query: str) -> list[dict[str, Any]]:
        """Return bounded historical records; authority is added only as false."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(receipt: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _request_digest(request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(request)).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise RecallContractError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise RecallContractError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise RecallContractError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecallContractError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise RecallContractError(f"{field} requires a timezone")
    return parsed


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RecallContractError(f"{field} is invalid")
    return value


def _validate_request(request: Any) -> tuple[str, str, str, int, int]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise RecallContractError("recall request fields are invalid")
    request_id = _safe_id(request["request_id"], "request_id")
    task_id = _safe_id(request["task_id"], "task_id")
    query = _text(request["query"], "query", 4096)
    max_candidates = request["max_candidates"]
    max_returned_bytes = request["max_returned_bytes"]
    if type(max_candidates) is not int or not 0 < max_candidates <= 256:
        raise RecallContractError("max_candidates is invalid")
    if (
        type(max_returned_bytes) is not int
        or not 0 < max_returned_bytes <= 1_048_576
    ):
        raise RecallContractError("max_returned_bytes is invalid")
    return request_id, task_id, query, max_candidates, max_returned_bytes


def _validate_record(record: Any, index: int) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise RecallContractError(f"record {index} fields are invalid")
    _safe_id(record["record_id"], f"records[{index}].record_id")
    _text(record["content"], f"records[{index}].content", 16_384)
    tags = record["tags"]
    if (
        not isinstance(tags, list)
        or not tags
        or len(tags) > 64
        or len(set(tags)) != len(tags)
    ):
        raise RecallContractError("record tags are invalid")
    for tag in tags:
        _safe_id(tag, "record tag")
    observed = _timestamp(record["observed_at"], "observed_at")
    valid_until = _timestamp(record["valid_until"], "valid_until")
    if valid_until < observed:
        raise RecallContractError("valid_until precedes observed_at")
    _text(record["source_ref"], "source_ref", 2048)
    _sha256(record["source_sha256"], "source_sha256")


class InMemoryRecallProvider:
    """Deterministic reference provider for local fixtures and conformance tests."""

    provider_id = "fixture-memory/v1"

    def __init__(self, records: list[dict[str, Any]]) -> None:
        if not isinstance(records, list) or len(records) > 10_000:
            raise RecallContractError("records are invalid")
        for index, record in enumerate(records):
            _validate_record(record, index)
        ids = [record["record_id"] for record in records]
        if len(set(ids)) != len(ids):
            raise RecallContractError("record IDs must be unique")
        self._records = copy.deepcopy(records)

    def recall(self, query: str) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in re.findall(r"[A-Za-z0-9_-]+", query)}
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for record in self._records:
            haystack = f"{record['content']} {' '.join(record['tags'])}".casefold()
            score = sum(term in haystack for term in terms)
            if score:
                scored.append((-score, record["record_id"], copy.deepcopy(record)))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored]


class UnavailableRecallProvider:
    """Reference failure adapter used to prove state independence."""

    provider_id = "unavailable/v1"

    def __init__(self, code: str = "503") -> None:
        self._code = _safe_id(code, "provider error code")

    def recall(self, query: str) -> list[dict[str, Any]]:
        raise RecallProviderUnavailable(self._code)


class RecallCoordinator:
    """Apply candidate and byte bounds while isolating provider failure."""

    def __init__(self, provider: RecallProvider) -> None:
        self._provider = provider
        _safe_id(provider.provider_id, "provider_id")

    def retrieve(self, request: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
        (
            request_id,
            task_id,
            query,
            max_candidates,
            max_returned_bytes,
        ) = _validate_request(request)
        observed = _timestamp(observed_at, "observed_at")
        status = "completed"
        error_code: str | None = None
        try:
            records = self._provider.recall(query)
        except RecallProviderUnavailable as exc:
            records = []
            status = "degraded"
            error_code = _safe_id(exc.code, "provider_error_code")
        if not isinstance(records, list) or len(records) > 10_000:
            raise RecallContractError("provider returned an invalid record set")

        candidates: list[dict[str, Any]] = []
        returned_bytes = 0
        for index, record in enumerate(records):
            _validate_record(record, index)
            content_bytes = record["content"].encode("utf-8")
            if len(candidates) >= max_candidates or returned_bytes + len(content_bytes) > max_returned_bytes:
                break
            freshness = (
                "current"
                if observed <= _timestamp(record["valid_until"], "valid_until")
                else "stale"
            )
            candidates.append(
                {
                    "candidate_id": record["record_id"],
                    "content": record["content"],
                    "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
                    "observed_at": record["observed_at"],
                    "valid_until": record["valid_until"],
                    "freshness": freshness,
                    "source_ref": record["source_ref"],
                    "source_sha256": record["source_sha256"],
                    "active_state_authority": False,
                }
            )
            returned_bytes += len(content_bytes)

        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": f"receipt/{request_id}",
            "request_id": request_id,
            "task_id": task_id,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "max_candidates": max_candidates,
            "max_returned_bytes": max_returned_bytes,
            "request_sha256": _request_digest(request),
            "provider_id": self._provider.provider_id,
            "status": status,
            "provider_error_code": error_code,
            "observed_at": observed_at,
            "candidates": candidates,
            "returned_bytes": returned_bytes,
            "state_write_authority": False,
            "provider_authority": False,
            "receipt_sha256": "",
        }
        receipt["receipt_sha256"] = _digest(receipt)
        validate_recall_receipt(receipt, expected_request=request)
        return receipt


def validate_recall_receipt(
    receipt: Any, *, expected_request: dict[str, Any] | None = None
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise RecallContractError("recall receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise RecallContractError("recall receipt schema_version is invalid")
    for field in ("receipt_id", "request_id", "task_id", "provider_id"):
        _safe_id(receipt[field], field)
    query_sha256 = _sha256(receipt["query_sha256"], "query_sha256")
    max_candidates = receipt["max_candidates"]
    max_returned_bytes = receipt["max_returned_bytes"]
    if type(max_candidates) is not int or not 0 < max_candidates <= 256:
        raise RecallContractError("max_candidates is invalid")
    if (
        type(max_returned_bytes) is not int
        or not 0 < max_returned_bytes <= 1_048_576
    ):
        raise RecallContractError("max_returned_bytes is invalid")
    request_sha256 = _sha256(receipt["request_sha256"], "request_sha256")
    if expected_request is not None:
        (
            expected_request_id,
            expected_task_id,
            expected_query,
            expected_max_candidates,
            expected_max_returned_bytes,
        ) = _validate_request(expected_request)
        expected_binding = (
            expected_request_id,
            expected_task_id,
            hashlib.sha256(expected_query.encode("utf-8")).hexdigest(),
            expected_max_candidates,
            expected_max_returned_bytes,
            _request_digest(expected_request),
        )
        receipt_binding = (
            receipt["request_id"],
            receipt["task_id"],
            query_sha256,
            max_candidates,
            max_returned_bytes,
            request_sha256,
        )
        if receipt_binding != expected_binding:
            raise RecallContractError("receipt does not match expected request")
    observed = _timestamp(receipt["observed_at"], "observed_at")
    status = receipt["status"]
    if status not in {"completed", "degraded"}:
        raise RecallContractError("recall status is invalid")
    error_code = receipt["provider_error_code"]
    if status == "degraded":
        _safe_id(error_code, "provider_error_code")
        if receipt["candidates"]:
            raise RecallContractError("degraded recall cannot carry candidates")
    elif error_code is not None:
        raise RecallContractError("completed recall cannot carry an error")
    candidates = receipt["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 256:
        raise RecallContractError("candidates are invalid")
    if len(candidates) > max_candidates:
        raise RecallContractError("candidates exceed max_candidates")
    returned_bytes = 0
    candidate_ids: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
            raise RecallContractError(f"candidate {index} fields are invalid")
        candidate_ids.append(_safe_id(candidate["candidate_id"], "candidate_id"))
        content = _text(candidate["content"], "candidate.content", 16_384)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != candidate["content_sha256"]:
            raise RecallContractError("candidate content digest mismatch")
        candidate_observed = _timestamp(
            candidate["observed_at"], "candidate.observed_at"
        )
        valid_until = _timestamp(candidate["valid_until"], "candidate.valid_until")
        if candidate_observed > observed:
            raise RecallContractError("candidate observed_at postdates receipt observed_at")
        if valid_until < candidate_observed:
            raise RecallContractError("candidate valid_until precedes candidate observed_at")
        if candidate["freshness"] not in {"current", "stale"}:
            raise RecallContractError("candidate freshness is invalid")
        expected_freshness = "current" if observed <= valid_until else "stale"
        if candidate["freshness"] != expected_freshness:
            raise RecallContractError("candidate freshness is inaccurate")
        _text(candidate["source_ref"], "candidate.source_ref", 2048)
        _sha256(candidate["source_sha256"], "candidate.source_sha256")
        if candidate["active_state_authority"] is not False:
            raise RecallContractError("recall candidate cannot have active state authority")
        returned_bytes += len(content.encode("utf-8"))
    if len(set(candidate_ids)) != len(candidate_ids):
        raise RecallContractError("candidate IDs must be unique")
    if receipt["returned_bytes"] != returned_bytes:
        raise RecallContractError("returned_bytes is inaccurate")
    if returned_bytes > max_returned_bytes:
        raise RecallContractError("returned_bytes exceeds max_returned_bytes")
    if receipt["state_write_authority"] is not False or receipt["provider_authority"] is not False:
        raise RecallContractError("recall cannot grant authority")
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _digest(receipt):
        raise RecallContractError("recall receipt digest mismatch")


def canonical_recall_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    validate_recall_receipt(receipt)
    return _canonical(receipt)
