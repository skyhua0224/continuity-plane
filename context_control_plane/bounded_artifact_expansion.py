"""Bounded artifact excerpts for provider prompt composition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .artifact_store import ArtifactRangeError, ArtifactRef, LocalArtifactStore

SCHEMA_VERSION = "context.bounded-artifact-expansion/v1alpha1"
EXPANDER_VERSION = "context.bounded-artifact-expander/v1alpha1"
MAX_RANGE_COUNT = 256
MAX_SUMMARY_BYTES = 4096
MAX_PURPOSE_BYTES = 512
_RANGE_FIELDS = {"purpose", "offset_bytes", "length_bytes"}
_RECEIPT_FIELDS = {
    "schema_version",
    "expander_version",
    "artifact_ref",
    "returned_byte_budget",
    "scanned_byte_budget",
    "requested_bytes",
    "returned_bytes",
    "scanned_bytes",
    "prompt_bytes",
    "prompt_sha256",
    "ranges",
    "state_write_authority",
    "provider_native_authority",
}
_RECEIPT_RANGE_FIELDS = {
    "purpose",
    "offset_bytes",
    "requested_bytes",
    "returned_bytes",
    "scanned_bytes",
    "output_sha256",
}


class BoundedArtifactExpansionError(ValueError):
    """Raised when an artifact cannot be expanded into a safe prompt."""


class BoundedArtifactBudgetError(BoundedArtifactExpansionError):
    """Raised when a requested expansion exceeds an explicit byte budget."""


class BoundedArtifactInputError(BoundedArtifactExpansionError):
    """Raised when expansion input is malformed or unbounded."""


def _bounded_text(value: Any, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BoundedArtifactInputError(f"{field} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BoundedArtifactInputError(f"{field} is invalid") from exc
    if len(encoded) > maximum_bytes:
        raise BoundedArtifactInputError(f"{field} exceeds bound")
    return value


def _positive_budget(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BoundedArtifactInputError(f"{field} must be a positive integer")
    return value


def _nonnegative_budget(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BoundedArtifactInputError(f"{field} must be a non-negative integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise BoundedArtifactInputError(f"{field} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise BoundedArtifactInputError(f"{field} must be a SHA-256 digest") from exc
    if value != value.lower():
        raise BoundedArtifactInputError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BoundedArtifactInputError(f"{field} must be a non-negative integer")
    return value


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_bounded_artifact_expansion_bytes(receipt: dict[str, Any]) -> bytes:
    """Return canonical bytes after validating a bounded expansion receipt."""
    validate_bounded_artifact_expansion_receipt(receipt)
    return _canonical_json_bytes(receipt)


def validate_bounded_artifact_expansion_receipt(
    receipt: dict[str, Any],
    *,
    prompt_bytes: bytes | None = None,
) -> None:
    """Validate receipt accounting and, when supplied, its prompt projection."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise BoundedArtifactInputError("bounded expansion receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise BoundedArtifactInputError("bounded expansion schema_version is invalid")
    if receipt["expander_version"] != EXPANDER_VERSION:
        raise BoundedArtifactInputError("bounded expansion expander_version is invalid")
    try:
        ref = ArtifactRef.from_document(receipt["artifact_ref"])
    except (TypeError, ValueError) as exc:
        raise BoundedArtifactInputError("bounded expansion artifact_ref is invalid") from exc
    returned_budget = _positive_budget(
        receipt["returned_byte_budget"], "returned_byte_budget"
    )
    scanned_budget = _nonnegative_budget(
        receipt["scanned_byte_budget"], "scanned_byte_budget"
    )
    requested = _nonnegative_integer(receipt["requested_bytes"], "requested_bytes")
    returned = _nonnegative_integer(receipt["returned_bytes"], "returned_bytes")
    scanned = _nonnegative_integer(receipt["scanned_bytes"], "scanned_bytes")
    prompt_size = _nonnegative_integer(receipt["prompt_bytes"], "prompt_bytes")
    _sha256(receipt["prompt_sha256"], "prompt_sha256")
    if requested != returned:
        raise BoundedArtifactInputError("requested and returned bytes differ")
    if returned > returned_budget:
        raise BoundedArtifactBudgetError("receipt exceeds returned byte budget")
    if scanned > scanned_budget:
        raise BoundedArtifactBudgetError("receipt exceeds scanned byte budget")
    if receipt["state_write_authority"] is not False:
        raise BoundedArtifactInputError("bounded expansion cannot write State")
    if receipt["provider_native_authority"] is not False:
        raise BoundedArtifactInputError("bounded expansion cannot grant provider authority")

    ranges = receipt["ranges"]
    if not isinstance(ranges, list) or len(ranges) > MAX_RANGE_COUNT:
        raise BoundedArtifactInputError("bounded expansion ranges are invalid")
    range_requested = 0
    range_returned = 0
    range_scanned = 0
    previous_end = -1
    for index, item in enumerate(ranges):
        if not isinstance(item, dict) or set(item) != _RECEIPT_RANGE_FIELDS:
            raise BoundedArtifactInputError(f"bounded expansion range {index} is invalid")
        _bounded_text(item["purpose"], f"ranges[{index}].purpose", MAX_PURPOSE_BYTES)
        offset = item["offset_bytes"]
        requested_length = item["requested_bytes"]
        returned_length = item["returned_bytes"]
        scanned_length = item["scanned_bytes"]
        if type(offset) is not int or offset < 0:
            raise ArtifactRangeError("receipt range offset must be non-negative")
        if type(requested_length) is not int or requested_length <= 0:
            raise ArtifactRangeError("receipt range length must be positive")
        if type(returned_length) is not int or returned_length <= 0:
            raise BoundedArtifactInputError("receipt range returned bytes are invalid")
        if type(scanned_length) is not int or scanned_length < 0:
            raise BoundedArtifactInputError("receipt range scanned bytes are invalid")
        if offset < previous_end:
            raise BoundedArtifactInputError("receipt ranges must not overlap")
        if offset + requested_length > ref.size_bytes:
            raise ArtifactRangeError("receipt range exceeds artifact bounds")
        if returned_length != requested_length:
            raise BoundedArtifactInputError("receipt range returned bytes differ")
        if scanned_length != ref.size_bytes:
            raise BoundedArtifactInputError("receipt range scanned bytes are inaccurate")
        _sha256(item["output_sha256"], f"ranges[{index}].output_sha256")
        previous_end = offset + requested_length
        range_requested += requested_length
        range_returned += returned_length
        range_scanned += scanned_length
    if (range_requested, range_returned, range_scanned) != (requested, returned, scanned):
        raise BoundedArtifactInputError("bounded expansion totals do not match ranges")

    if prompt_bytes is None:
        return
    if not isinstance(prompt_bytes, bytes):
        raise BoundedArtifactInputError("prompt_bytes must be bytes")
    if len(prompt_bytes) != prompt_size:
        raise BoundedArtifactInputError("prompt_bytes size does not match receipt")
    if hashlib.sha256(prompt_bytes).hexdigest() != receipt["prompt_sha256"]:
        raise BoundedArtifactInputError("prompt digest does not match receipt")
    try:
        prompt = json.loads(prompt_bytes)
    except (TypeError, ValueError) as exc:
        raise BoundedArtifactInputError("prompt bytes are not canonical JSON") from exc
    if not isinstance(prompt, dict) or set(prompt) != {
        "summary",
        "artifact_ref",
        "necessary_excerpts",
    }:
        raise BoundedArtifactInputError("prompt projection fields are invalid")
    _bounded_text(prompt["summary"], "prompt.summary", MAX_SUMMARY_BYTES)
    if prompt["artifact_ref"] != ref.to_document():
        raise BoundedArtifactInputError("prompt artifact_ref does not match receipt")
    excerpts = prompt["necessary_excerpts"]
    if not isinstance(excerpts, list) or len(excerpts) != len(ranges):
        raise BoundedArtifactInputError("prompt excerpts do not match receipt ranges")
    for index, (excerpt, range_receipt) in enumerate(zip(excerpts, ranges)):
        if not isinstance(excerpt, dict) or set(excerpt) != {
            "purpose",
            "offset_bytes",
            "length_bytes",
            "text",
        }:
            raise BoundedArtifactInputError(f"prompt excerpt {index} is invalid")
        _bounded_text(excerpt["purpose"], f"prompt.excerpts[{index}].purpose", MAX_PURPOSE_BYTES)
        if excerpt["purpose"] != range_receipt["purpose"]:
            raise BoundedArtifactInputError("prompt excerpt purpose does not match receipt")
        if excerpt["offset_bytes"] != range_receipt["offset_bytes"]:
            raise BoundedArtifactInputError("prompt excerpt offset does not match receipt")
        if type(excerpt["length_bytes"]) is not int or excerpt["length_bytes"] <= 0:
            raise BoundedArtifactInputError("prompt excerpt length is invalid")
        if excerpt["length_bytes"] != range_receipt["returned_bytes"]:
            raise BoundedArtifactInputError("prompt excerpt length does not match receipt")
        text = excerpt["text"]
        if not isinstance(text, str) or "\x00" in text:
            raise BoundedArtifactInputError(f"prompt excerpt {index} text is invalid")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BoundedArtifactInputError(f"prompt excerpt {index} text is invalid") from exc
        if len(encoded) != range_receipt["returned_bytes"]:
            raise BoundedArtifactInputError("prompt excerpt byte length does not match receipt")
        if hashlib.sha256(encoded).hexdigest() != range_receipt["output_sha256"]:
            raise BoundedArtifactInputError("prompt excerpt digest does not match receipt")


@dataclass(frozen=True, slots=True)
class BoundedArtifactExpansion:
    """Immutable prompt projection and its range-accounting receipt."""

    _prompt_bytes: bytes
    _receipt_bytes: bytes

    @property
    def prompt(self) -> dict[str, Any]:
        return json.loads(self._prompt_bytes)

    @property
    def prompt_bytes(self) -> bytes:
        return self._prompt_bytes

    @property
    def receipt(self) -> dict[str, Any]:
        return json.loads(self._receipt_bytes)


def expand_artifact(
    store: LocalArtifactStore,
    *,
    artifact_ref: ArtifactRef,
    summary: str,
    necessary_ranges: list[dict[str, Any]],
    returned_byte_budget: int,
    scanned_byte_budget: int,
) -> BoundedArtifactExpansion:
    """Read only the requested prompt excerpts through the verified local CAS."""
    if not isinstance(store, LocalArtifactStore):
        raise BoundedArtifactInputError("store must be a LocalArtifactStore")
    if not isinstance(artifact_ref, ArtifactRef):
        raise BoundedArtifactInputError("artifact_ref must be an ArtifactRef")
    _bounded_text(summary, "summary", MAX_SUMMARY_BYTES)
    _positive_budget(returned_byte_budget, "returned_byte_budget")
    _nonnegative_budget(scanned_byte_budget, "scanned_byte_budget")
    if (
        not isinstance(necessary_ranges, list)
        or len(necessary_ranges) > MAX_RANGE_COUNT
    ):
        raise BoundedArtifactInputError("necessary_ranges must be a bounded list")
    for request in necessary_ranges:
        if not isinstance(request, dict) or set(request) != _RANGE_FIELDS:
            raise BoundedArtifactInputError("necessary range fields are invalid")
        _bounded_text(request["purpose"], "range purpose", MAX_PURPOSE_BYTES)
        offset = request["offset_bytes"]
        length = request["length_bytes"]
        if type(offset) is not int or offset < 0:
            raise ArtifactRangeError("artifact range offset must be non-negative")
        if type(length) is not int or length <= 0:
            raise ArtifactRangeError("artifact range length must be positive")
        if length > store.max_range_bytes:
            raise ArtifactRangeError("artifact range exceeds configured bound")
        if offset > artifact_ref.size_bytes or offset + length > artifact_ref.size_bytes:
            raise ArtifactRangeError("artifact range exceeds artifact bounds")
    ordered_ranges = sorted(
        necessary_ranges,
        key=lambda item: (item["offset_bytes"], item["length_bytes"], item["purpose"]),
    )
    previous_end = -1
    for request in ordered_ranges:
        if request["offset_bytes"] < previous_end:
            raise BoundedArtifactInputError("necessary ranges must not overlap")
        previous_end = request["offset_bytes"] + request["length_bytes"]
    requested_bytes = sum(item["length_bytes"] for item in ordered_ranges)
    if requested_bytes > returned_byte_budget:
        raise BoundedArtifactBudgetError("artifact expansion exceeds returned byte budget")
    projected_scanned_bytes = len(ordered_ranges) * artifact_ref.size_bytes
    if projected_scanned_bytes > scanned_byte_budget:
        raise BoundedArtifactBudgetError("artifact expansion exceeds scanned byte budget")
    excerpts: list[dict[str, Any]] = []
    range_receipts: list[dict[str, Any]] = []
    returned_bytes = 0
    scanned_bytes = 0
    for request in ordered_ranges:
        payload = store.read_range(
            artifact_ref,
            offset=request["offset_bytes"],
            length=request["length_bytes"],
        )
        returned_bytes += len(payload)
        scanned_bytes += artifact_ref.size_bytes
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BoundedArtifactInputError("artifact excerpt is not valid UTF-8") from exc
        excerpts.append(
            {
                "purpose": request["purpose"],
                "offset_bytes": request["offset_bytes"],
                "length_bytes": len(payload),
                "text": text,
            }
        )
        range_receipts.append(
            {
                "purpose": request["purpose"],
                "offset_bytes": request["offset_bytes"],
                "requested_bytes": request["length_bytes"],
                "returned_bytes": len(payload),
                "scanned_bytes": artifact_ref.size_bytes,
                "output_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    prompt = {
        "summary": summary,
        "artifact_ref": artifact_ref.to_document(),
        "necessary_excerpts": excerpts,
    }
    prompt_bytes = _canonical_json_bytes(prompt)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "expander_version": EXPANDER_VERSION,
        "artifact_ref": artifact_ref.to_document(),
        "returned_byte_budget": returned_byte_budget,
        "scanned_byte_budget": scanned_byte_budget,
        "requested_bytes": requested_bytes,
        "returned_bytes": returned_bytes,
        "scanned_bytes": scanned_bytes,
        "prompt_bytes": len(prompt_bytes),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "ranges": range_receipts,
        "state_write_authority": False,
        "provider_native_authority": False,
    }
    result = BoundedArtifactExpansion(
        _prompt_bytes=prompt_bytes,
        _receipt_bytes=_canonical_json_bytes(receipt),
    )
    validate_bounded_artifact_expansion_receipt(
        result.receipt,
        prompt_bytes=result.prompt_bytes,
    )
    return result
