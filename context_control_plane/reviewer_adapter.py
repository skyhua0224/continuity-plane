"""Provider-neutral reviewer adapters with candidate-only asynchronous fallback."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from datetime import datetime
from typing import Any, Protocol

SCHEMA_VERSION = "context.review-receipt/v1alpha1"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "request_id",
    "task_id",
    "execution_packet_ref",
    "artifact_refs",
    "max_output_bytes",
    "deadline_at",
}
_FINDING_FIELDS = {
    "finding_id",
    "severity",
    "summary",
    "evidence_refs",
    "candidate_only",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "request_id",
    "task_id",
    "execution_packet_ref",
    "artifact_refs",
    "max_output_bytes",
    "request_sha256",
    "provider_id",
    "provider_kind",
    "status",
    "operation_ref",
    "findings",
    "returned_bytes",
    "observed_at",
    "deadline_at",
    "async_degraded",
    "state_write_authority",
    "completion_authority",
    "receipt_sha256",
}


class ReviewContractError(ValueError):
    """Raised when a reviewer crosses its candidate-only boundary."""


class ReviewTimeout(RuntimeError):
    def __init__(self, operation_ref: str) -> None:
        super().__init__(operation_ref)
        self.operation_ref = operation_ref


class ReviewerAdapter(Protocol):
    provider_id: str
    provider_kind: str

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return status, operation_ref, and candidate findings."""


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


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise ReviewContractError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int = 8192) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise ReviewContractError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReviewContractError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewContractError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ReviewContractError(f"{field} requires a timezone")
    return parsed


def _artifact_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ARTIFACT_RE.fullmatch(value) is None:
        raise ReviewContractError(f"{field} is invalid")
    return value


def _validate_finding(finding: Any, index: int) -> None:
    if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
        raise ReviewContractError(f"finding {index} fields are invalid")
    _safe(finding["finding_id"], "finding_id")
    if finding["severity"] not in {"low", "medium", "high", "critical"}:
        raise ReviewContractError("finding severity is invalid")
    _text(finding["summary"], "finding.summary")
    refs = finding["evidence_refs"]
    if not isinstance(refs, list) or not refs or len(refs) > 64 or len(set(refs)) != len(refs):
        raise ReviewContractError("finding evidence_refs are invalid")
    for ref in refs:
        _artifact_ref(ref, "finding evidence_ref")
    if finding["candidate_only"] is not True:
        raise ReviewContractError("review findings must remain candidate-only")


def _validate_request(request: Any) -> None:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ReviewContractError("review request fields are invalid")
    _safe(request["request_id"], "request_id")
    _safe(request["task_id"], "task_id")
    _artifact_ref(request["execution_packet_ref"], "execution_packet_ref")
    refs = request["artifact_refs"]
    if not isinstance(refs, list) or len(refs) > 128 or len(set(refs)) != len(refs):
        raise ReviewContractError("artifact_refs are invalid")
    for ref in refs:
        _artifact_ref(ref, "artifact_ref")
    budget = request["max_output_bytes"]
    if type(budget) is not int or not 0 < budget <= 1_048_576:
        raise ReviewContractError("max_output_bytes is invalid")
    _timestamp(request["deadline_at"], "deadline_at")


class LocalReviewerAdapter:
    provider_id = "local-reviewer/fixture-v1"
    provider_kind = "local"

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        if not isinstance(findings, list) or len(findings) > 256:
            raise ReviewContractError("findings are invalid")
        for index, finding in enumerate(findings):
            _validate_finding(finding, index)
        self._findings = copy.deepcopy(findings)

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "completed",
            "operation_ref": None,
            "findings": copy.deepcopy(self._findings),
        }


class DeferredReviewerAdapter:
    provider_id = "external-reviewer/deferred-v1"
    provider_kind = "external"

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "pending",
            "operation_ref": f"review-operation/{request['request_id']}",
            "findings": [],
        }


class TimeoutReviewerAdapter:
    provider_id = "external-reviewer/timeout-v1"
    provider_kind = "external"

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        raise ReviewTimeout(f"review-operation/{request['request_id']}")


class ReviewCoordinator:
    def __init__(self, adapter: ReviewerAdapter) -> None:
        self._adapter = adapter
        _safe(adapter.provider_id, "provider_id")
        if adapter.provider_kind not in {"local", "external"}:
            raise ReviewContractError("provider_kind is invalid")

    def review(self, request: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
        _validate_request(request)
        _timestamp(observed_at, "observed_at")
        try:
            submission = self._adapter.submit(copy.deepcopy(request))
        except ReviewTimeout as exc:
            submission = {
                "status": "timed_out",
                "operation_ref": exc.operation_ref,
                "findings": [],
            }
        if not isinstance(submission, dict) or set(submission) != {
            "status",
            "operation_ref",
            "findings",
        }:
            raise ReviewContractError("review submission fields are invalid")
        status = submission["status"]
        if status not in {"completed", "pending", "timed_out"}:
            raise ReviewContractError("review submission status is invalid")
        findings = submission["findings"]
        if not isinstance(findings, list) or len(findings) > 256:
            raise ReviewContractError("review findings are invalid")
        for index, finding in enumerate(findings):
            _validate_finding(finding, index)
        operation_ref = submission["operation_ref"]
        if status == "completed":
            if operation_ref is not None:
                raise ReviewContractError("completed review cannot remain pending")
        else:
            _safe(operation_ref, "operation_ref")
            if findings:
                raise ReviewContractError("incomplete review cannot publish findings")
        returned_bytes = len(_canonical(findings)) if findings else 0
        if returned_bytes > request["max_output_bytes"]:
            raise ReviewContractError("review output budget exceeded")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": f"receipt/{request['request_id']}",
            "request_id": request["request_id"],
            "task_id": request["task_id"],
            "execution_packet_ref": request["execution_packet_ref"],
            "artifact_refs": copy.deepcopy(request["artifact_refs"]),
            "max_output_bytes": request["max_output_bytes"],
            "request_sha256": _request_digest(request),
            "provider_id": self._adapter.provider_id,
            "provider_kind": self._adapter.provider_kind,
            "status": status,
            "operation_ref": operation_ref,
            "findings": copy.deepcopy(findings),
            "returned_bytes": returned_bytes,
            "observed_at": observed_at,
            "deadline_at": request["deadline_at"],
            "async_degraded": status != "completed",
            "state_write_authority": False,
            "completion_authority": False,
            "receipt_sha256": "",
        }
        receipt["receipt_sha256"] = _digest(receipt)
        validate_review_receipt(receipt, expected_request=request)
        return receipt


def validate_review_receipt(
    receipt: Any, *, expected_request: dict[str, Any] | None = None
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ReviewContractError("review receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ReviewContractError("review receipt schema_version is invalid")
    for field in ("receipt_id", "request_id", "task_id", "provider_id"):
        _safe(receipt[field], field)
    request = {
        "request_id": receipt["request_id"],
        "task_id": receipt["task_id"],
        "execution_packet_ref": receipt["execution_packet_ref"],
        "artifact_refs": receipt["artifact_refs"],
        "max_output_bytes": receipt["max_output_bytes"],
        "deadline_at": receipt["deadline_at"],
    }
    _validate_request(request)
    request_sha256 = receipt["request_sha256"]
    if (
        not isinstance(request_sha256, str)
        or len(request_sha256) != 64
        or request_sha256 != request_sha256.lower()
        or any(character not in string.hexdigits for character in request_sha256)
        or request_sha256 != _request_digest(request)
    ):
        raise ReviewContractError("review request digest mismatch")
    if expected_request is not None:
        _validate_request(expected_request)
        if request_sha256 != _request_digest(expected_request):
            raise ReviewContractError("receipt does not match expected request")
    if receipt["provider_kind"] not in {"local", "external"}:
        raise ReviewContractError("provider_kind is invalid")
    status = receipt["status"]
    if status not in {"completed", "pending", "timed_out"}:
        raise ReviewContractError("review status is invalid")
    operation_ref = receipt["operation_ref"]
    if status == "completed":
        if operation_ref is not None or receipt["async_degraded"] is not False:
            raise ReviewContractError("completed review async state is invalid")
    else:
        _safe(operation_ref, "operation_ref")
        if receipt["async_degraded"] is not True:
            raise ReviewContractError("incomplete review must degrade asynchronously")
    findings = receipt["findings"]
    if not isinstance(findings, list) or len(findings) > 256:
        raise ReviewContractError("findings are invalid")
    if status != "completed" and findings:
        raise ReviewContractError("incomplete review cannot carry findings")
    for index, finding in enumerate(findings):
        _validate_finding(finding, index)
        if not set(finding["evidence_refs"]) <= set(receipt["artifact_refs"]):
            raise ReviewContractError("finding evidence is not a requested artifact")
    returned_bytes = len(_canonical(findings)) if findings else 0
    if receipt["returned_bytes"] != returned_bytes:
        raise ReviewContractError("returned_bytes is inaccurate")
    if returned_bytes > receipt["max_output_bytes"]:
        raise ReviewContractError("returned_bytes exceeds the request budget")
    observed_at = _timestamp(receipt["observed_at"], "observed_at")
    deadline_at = _timestamp(receipt["deadline_at"], "deadline_at")
    if status in {"completed", "pending"} and observed_at > deadline_at:
        raise ReviewContractError("review status is later than the deadline")
    if status == "timed_out" and observed_at < deadline_at:
        raise ReviewContractError("timed_out status is earlier than the deadline")
    if receipt["state_write_authority"] is not False or receipt["completion_authority"] is not False:
        raise ReviewContractError("reviewers cannot grant state or completion authority")
    digest = receipt["receipt_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest != digest.lower()
        or any(character not in string.hexdigits for character in digest)
    ):
        raise ReviewContractError("receipt_sha256 is invalid")
    if digest != _digest(receipt):
        raise ReviewContractError("review receipt digest mismatch")


def canonical_review_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    validate_review_receipt(receipt)
    return _canonical(receipt)
