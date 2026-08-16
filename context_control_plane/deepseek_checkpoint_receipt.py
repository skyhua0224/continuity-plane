"""Strict evidence receipt for the pinned DeepSeek checkpoint fixture."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
from typing import Any

SCHEMA_VERSION = "context.deepseek-checkpoint-receipt/v1alpha1"
FIXTURE_ID = "fixture/m8-01/deepseek-checkpoint-crash-recovery/v1"
REFERENCE_ID = "deepseek-ai-deepseek-harness"
SOURCE_TEST_PATH = (
    "packages/session/session-checkpoint-policy/tests/crash-recovery.e2e.ts"
)
SOURCE_REVISION = "47f943859bef60e4160492346772ded9b24f765a"
SOURCE_TREE = "f904efab9ef435201d6ba4da88a34d6366568272"
SOURCE_TEST_BLOB_SHA256 = (
    "f26537de8fe8905b3fc57bec221c1428145a844f90812fe427ece64abaa248de"
)
SIGNALS = ["request-interrupted", "TOOL_OUTCOME_UNKNOWN"]
_TEST_TITLES = {
    "persists the complete request before model dispatch",
    (
        "persists tool intent before a side effect and repairs its missing "
        "result as unknown"
    ),
}

_FIELDS = {
    "schema_version",
    "fixture_id",
    "generated_at",
    "reference_id",
    "source_revision",
    "source_tree",
    "source_test_path",
    "source_test_blob_sha256",
    "report_sha256",
    "node_version",
    "package_manager",
    "platform",
    "test_suites",
    "checkpoint_fixture_runs",
    "passed_tests",
    "failed_tests",
    "skipped_tests",
    "sigkill_count",
    "recovery_passes",
    "signals",
    "external_provider_invocations",
    "state_write_authority",
    "provider_native_authority",
    "receipt_sha256",
}


class DeepSeekCheckpointReceiptError(ValueError):
    """Raised when runnable checkpoint evidence is incomplete or forged."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _digest(receipt: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise DeepSeekCheckpointReceiptError("generated_at must be a timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeepSeekCheckpointReceiptError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        raise DeepSeekCheckpointReceiptError("generated_at must include an offset")


def _report_integer(report: dict[str, Any], field: str) -> int:
    value = report.get(field)
    if type(value) is not int or value < 0:
        raise DeepSeekCheckpointReceiptError(f"Vitest {field} is invalid")
    return value


def _validate_report_cases(report: dict[str, Any]) -> None:
    results = report.get("testResults")
    if not isinstance(results, list) or len(results) != 1:
        raise DeepSeekCheckpointReceiptError("Vitest result file coverage is invalid")
    result = results[0]
    if not isinstance(result, dict) or result.get("status") != "passed":
        raise DeepSeekCheckpointReceiptError("Vitest result file did not pass")
    name = result.get("name")
    if not isinstance(name, str) or not name.replace("\\", "/").endswith(
        SOURCE_TEST_PATH
    ):
        raise DeepSeekCheckpointReceiptError("Vitest result source is invalid")
    assertions = result.get("assertionResults")
    if not isinstance(assertions, list) or len(assertions) != 2:
        raise DeepSeekCheckpointReceiptError("Vitest assertion coverage is invalid")
    titles = {
        assertion.get("title")
        for assertion in assertions
        if isinstance(assertion, dict) and assertion.get("status") == "passed"
    }
    if titles != _TEST_TITLES:
        raise DeepSeekCheckpointReceiptError("Vitest checkpoint cases are invalid")


def compose_deepseek_checkpoint_receipt(
    report: dict[str, Any],
    *,
    generated_at: str,
    source_revision: str,
    source_tree: str,
    source_test_blob_sha256: str,
    report_sha256: str,
    node_version: str,
    package_manager: str,
    platform: str,
) -> dict[str, Any]:
    """Compose evidence only when the pinned two-case fixture passed completely."""
    if not isinstance(report, dict) or report.get("success") is not True:
        raise DeepSeekCheckpointReceiptError("DeepSeek checkpoint fixture did not pass")
    _validate_report_cases(report)
    suites = _report_integer(report, "numTotalTestSuites")
    passed_suites = _report_integer(report, "numPassedTestSuites")
    failed_suites = _report_integer(report, "numFailedTestSuites")
    pending_suites = _report_integer(report, "numPendingTestSuites")
    total = _report_integer(report, "numTotalTests")
    passed = _report_integer(report, "numPassedTests")
    failed = _report_integer(report, "numFailedTests")
    pending = _report_integer(report, "numPendingTests")
    todo = _report_integer(report, "numTodoTests")
    if (
        suites <= 0
        or passed_suites != suites
        or failed_suites != 0
        or pending_suites != 0
        or total != 2
        or passed != total
        or failed != 0
        or pending != 0
        or todo != 0
    ):
        raise DeepSeekCheckpointReceiptError(
            "DeepSeek checkpoint fixture counts are incomplete"
        )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": FIXTURE_ID,
        "generated_at": generated_at,
        "reference_id": REFERENCE_ID,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "source_test_path": SOURCE_TEST_PATH,
        "source_test_blob_sha256": source_test_blob_sha256,
        "report_sha256": report_sha256,
        "node_version": node_version,
        "package_manager": package_manager,
        "platform": platform,
        "test_suites": suites,
        "checkpoint_fixture_runs": total,
        "passed_tests": passed,
        "failed_tests": failed,
        "skipped_tests": pending + todo,
        "sigkill_count": total,
        "recovery_passes": passed,
        "signals": list(SIGNALS),
        "external_provider_invocations": 0,
        "state_write_authority": False,
        "provider_native_authority": False,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    validate_deepseek_checkpoint_receipt(receipt)
    return receipt


def validate_deepseek_checkpoint_receipt(receipt: object) -> None:
    """Fail closed unless the receipt proves the exact pinned fixture gate."""
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise DeepSeekCheckpointReceiptError("checkpoint receipt fields are invalid")
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": FIXTURE_ID,
        "reference_id": REFERENCE_ID,
        "source_revision": SOURCE_REVISION,
        "source_tree": SOURCE_TREE,
        "source_test_path": SOURCE_TEST_PATH,
        "source_test_blob_sha256": SOURCE_TEST_BLOB_SHA256,
        "package_manager": "pnpm@11.7.0",
        "checkpoint_fixture_runs": 2,
        "passed_tests": 2,
        "failed_tests": 0,
        "skipped_tests": 0,
        "sigkill_count": 2,
        "recovery_passes": 2,
        "signals": SIGNALS,
        "external_provider_invocations": 0,
        "state_write_authority": False,
        "provider_native_authority": False,
    }
    for field, expected in fixed.items():
        if receipt[field] != expected:
            raise DeepSeekCheckpointReceiptError(f"checkpoint {field} is invalid")
    _timestamp(receipt["generated_at"])
    if type(receipt["test_suites"]) is not int or receipt["test_suites"] <= 0:
        raise DeepSeekCheckpointReceiptError("checkpoint test_suites is invalid")
    for field in ("report_sha256", "receipt_sha256"):
        value = receipt[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise DeepSeekCheckpointReceiptError(f"checkpoint {field} is invalid")
    node_version = receipt["node_version"]
    if not isinstance(node_version, str) or not node_version:
        raise DeepSeekCheckpointReceiptError("checkpoint node_version is invalid")
    if receipt["platform"] not in {"linux", "darwin"}:
        raise DeepSeekCheckpointReceiptError("checkpoint platform is unsupported")
    if receipt["receipt_sha256"] != _digest(receipt):
        raise DeepSeekCheckpointReceiptError("checkpoint receipt digest mismatch")
