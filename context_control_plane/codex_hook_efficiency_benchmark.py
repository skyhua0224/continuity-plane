"""Measured Codex hook lifecycle overhead without provider transcript admission."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "context.codex-hook-efficiency-benchmark/v1alpha1"
HOOK_PATH = "integrations/codex/continuity-plane/scripts/continuity-hook.py"
HOOK_CONFIG_PATH = "integrations/codex/continuity-plane/hooks/hooks.json"
OFFICIAL_HOOK_CONTRACT = "https://developers.openai.com/codex/hooks"
EVENT_SEQUENCE = (
    "SessionStart:startup",
    "PostToolUse:Bash",
    "PreCompact:auto",
    "PostCompact:auto",
    "SessionStart:compact",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return _digest(_canonical(body))


def _git_blob(root: Path, git_ref: str, relative: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), "show", f"{git_ref}:{relative}"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        raise ValueError("baseline hook source is unavailable")
    return completed.stdout


def _percent_reduction(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0
    return round((baseline - candidate) / baseline * 100, 6)


def _percentile(samples: list[float], ratio: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return round(ordered[index], 6)


def _registered_event_sequence(encoded: bytes) -> list[str]:
    try:
        document = json.loads(encoded)
        registered = set(document["hooks"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("hook contract is invalid") from exc
    return [item for item in EVENT_SEQUENCE if item.split(":", 1)[0] in registered]


def _fake_continuity(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_CONTINUITY_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(" ".join(args) + "\\n")
packet = json.loads(os.environ["MCP_BINDING_ENVELOPE"])
if args and args[0] == "resume":
    print(json.dumps(packet, sort_keys=True, separators=(",", ":")))
elif args and args[0] == "autorun":
    print(json.dumps({
        "status": "continued",
        "state_event_created": False,
        "next_action": "continue-active-work",
        "resume_packet": packet,
    }, sort_keys=True, separators=(",", ":")))
else:
    print('{"status":"ok"}')
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _payloads(project: Path, session_id: str) -> list[dict[str, Any]]:
    base = {
        "session_id": session_id,
        "cwd": str(project),
        "model": "provider-model",
        "turn_id": f"turn-{session_id}",
    }
    return [
        {**base, "hook_event_name": "SessionStart", "source": "startup"},
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": f"tool-{session_id}",
            "tool_input": {"command": "python -m unittest tests.test_stage"},
            "tool_response": {"exit_code": 0, "output": "ok"},
        },
        {**base, "hook_event_name": "PreCompact", "trigger": "auto"},
        {**base, "hook_event_name": "PostCompact", "trigger": "auto"},
        {**base, "hook_event_name": "SessionStart", "source": "compact"},
    ]


def _model_context_bytes(event: str, stdout: str) -> int:
    if event not in {"SessionStart", "PostToolUse"} or not stdout:
        return 0
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        return 0
    hook = output.get("hookSpecificOutput")
    context = hook.get("additionalContext") if isinstance(hook, dict) else None
    return len(context.encode("utf-8")) if isinstance(context, str) else 0


def _measure_arm(
    hook: bytes,
    *,
    root: Path,
    samples: int,
    arm: str,
    registered_event_sequence: list[str],
) -> dict[str, Any]:
    calls_total = 0
    context_bytes_total = 0
    stdout_bytes_total = 0
    deny_count = 0
    stop_count = 0
    durations: list[float] = []
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        script = temp / f"continuity-hook-{arm}.py"
        script.write_bytes(hook)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        _fake_continuity(fake_bin / "continuity")
        project = temp / "portable-project"
        project.mkdir()
        control = project / ".continuity"
        control.mkdir()
        (control / "project.yaml").write_text(
            "schema_version: context.project/v1alpha1\nproject_id: portable-project\n",
            encoding="utf-8",
        )
        (control / "status-projection.json").write_text(
            '{"revision":8}\n', encoding="utf-8"
        )
        packet = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-active", "title": "Active Work"},
            "claim": {
                "claim_id": "claim-active",
                "actor_ref": "actor-active",
                "status": "active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "code-edit"}
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        for sample in range(samples):
            calls = temp / f"calls-{arm}-{sample}.txt"
            plugin_data = temp / f"plugin-data-{arm}-{sample}"
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(root / "integrations/codex/continuity-plane"),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "MCP_BINDING_ENVELOPE": json.dumps(packet),
                "CONTINUITY_EFFECT_POLICY": "auto",
            }
            started = time.perf_counter_ns()
            payloads = _payloads(project, f"benchmark-{arm}-{sample}")
            for event_name, payload in zip(EVENT_SEQUENCE, payloads, strict=True):
                if event_name not in registered_event_sequence:
                    continue
                completed = subprocess.run(
                    [sys.executable, str(script)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                    timeout=10,
                )
                if completed.returncode != 0:
                    raise ValueError(f"{arm} hook execution failed")
                stdout_bytes_total += len(completed.stdout.encode("utf-8"))
                context_bytes_total += _model_context_bytes(
                    payload["hook_event_name"], completed.stdout
                )
                if completed.stdout:
                    try:
                        output = json.loads(completed.stdout)
                    except json.JSONDecodeError:
                        output = {}
                    hook_output = output.get("hookSpecificOutput")
                    if (
                        isinstance(hook_output, dict)
                        and hook_output.get("permissionDecision") == "deny"
                    ):
                        deny_count += 1
                    if output.get("continue") is False:
                        stop_count += 1
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            calls_total += len(calls.read_text(encoding="utf-8").splitlines())
    return {
        "samples": samples,
        "registered_event_sequence": registered_event_sequence,
        "continuity_calls_total": calls_total,
        "continuity_calls_per_sample": calls_total / samples,
        "model_context_bytes_total": context_bytes_total,
        "model_context_bytes_per_sample": context_bytes_total / samples,
        "hook_stdout_bytes_total": stdout_bytes_total,
        "hook_stdout_bytes_per_sample": stdout_bytes_total / samples,
        "deny_count": deny_count,
        "stop_count": stop_count,
        "wall_ms_p50": round(statistics.median(durations), 6),
        "wall_ms_p95": _percentile(durations, 0.95),
    }


def benchmark_codex_hook_efficiency(
    root: str | Path,
    *,
    baseline_git_ref: str,
    samples: int,
    observed_at: str,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if type(samples) is not int or samples < 1 or samples > 1000:
        raise ValueError("samples is invalid")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at requires a timezone")
    baseline = _git_blob(root, baseline_git_ref, HOOK_PATH)
    candidate = (root / HOOK_PATH).read_bytes()
    baseline_contract = _git_blob(root, baseline_git_ref, HOOK_CONFIG_PATH)
    candidate_contract = (root / HOOK_CONFIG_PATH).read_bytes()
    baseline_events = _registered_event_sequence(baseline_contract)
    candidate_events = _registered_event_sequence(candidate_contract)
    arms = {
        "baseline": _measure_arm(
            baseline,
            root=root,
            samples=samples,
            arm="baseline",
            registered_event_sequence=baseline_events,
        ),
        "candidate": _measure_arm(
            candidate,
            root=root,
            samples=samples,
            arm="candidate",
            registered_event_sequence=candidate_events,
        ),
    }
    improvements = {
        "continuity_call_reduction_percent": _percent_reduction(
            arms["baseline"]["continuity_calls_per_sample"],
            arms["candidate"]["continuity_calls_per_sample"],
        ),
        "model_context_byte_reduction_percent": _percent_reduction(
            arms["baseline"]["model_context_bytes_per_sample"],
            arms["candidate"]["model_context_bytes_per_sample"],
        ),
        "hook_stdout_byte_reduction_percent": _percent_reduction(
            arms["baseline"]["hook_stdout_bytes_per_sample"],
            arms["candidate"]["hook_stdout_bytes_per_sample"],
        ),
        "wall_ms_p50_reduction_percent": _percent_reduction(
            arms["baseline"]["wall_ms_p50"], arms["candidate"]["wall_ms_p50"]
        ),
        "wall_ms_p95_reduction_percent": _percent_reduction(
            arms["baseline"]["wall_ms_p95"], arms["candidate"]["wall_ms_p95"]
        ),
    }
    acceptance = {
        "candidate_deny_count_zero": arms["candidate"]["deny_count"] == 0,
        "candidate_stop_count_zero": arms["candidate"]["stop_count"] == 0,
        "continuity_call_reduction_at_least_30_percent": (
            improvements["continuity_call_reduction_percent"] >= 30
        ),
        "model_context_reduction_at_least_30_percent": (
            improvements["model_context_byte_reduction_percent"] >= 30
        ),
    }
    acceptance["passed"] = all(acceptance.values())
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "parameters": {
            "samples": samples,
            "effect_policy": "auto",
            "event_sequence": list(EVENT_SEQUENCE),
            "baseline_git_ref": baseline_git_ref,
        },
        "provenance": {
            "baseline_hook_sha256": _digest(baseline),
            "candidate_hook_sha256": _digest(candidate),
            "baseline_hook_contract_sha256": _digest(baseline_contract),
            "candidate_hook_contract_sha256": _digest(candidate_contract),
            "official_hook_contract": OFFICIAL_HOOK_CONTRACT,
        },
        "arms": arms,
        "improvements": improvements,
        "acceptance": acceptance,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    validate_codex_hook_efficiency_receipt(receipt, root=root)
    return receipt


def validate_codex_hook_efficiency_receipt(
    receipt: dict[str, Any], *, root: str | Path
) -> None:
    expected_fields = {
        "schema_version",
        "observed_at",
        "parameters",
        "provenance",
        "arms",
        "improvements",
        "acceptance",
        "state_write_authority",
        "completion_authority",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise ValueError("receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("receipt schema is unsupported")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise ValueError("receipt digest mismatch")
    root = Path(root).resolve()
    parameters = receipt["parameters"]
    if (
        not isinstance(parameters, dict)
        or parameters.get("effect_policy") != "auto"
        or parameters.get("event_sequence") != list(EVENT_SEQUENCE)
        or type(parameters.get("samples")) is not int
        or parameters["samples"] < 1
    ):
        raise ValueError("receipt parameters are invalid")
    baseline = _git_blob(root, parameters["baseline_git_ref"], HOOK_PATH)
    candidate = (root / HOOK_PATH).read_bytes()
    baseline_contract = _git_blob(
        root, parameters["baseline_git_ref"], HOOK_CONFIG_PATH
    )
    candidate_contract = (root / HOOK_CONFIG_PATH).read_bytes()
    provenance = receipt["provenance"]
    if provenance != {
        "baseline_hook_sha256": _digest(baseline),
        "candidate_hook_sha256": _digest(candidate),
        "baseline_hook_contract_sha256": _digest(baseline_contract),
        "candidate_hook_contract_sha256": _digest(candidate_contract),
        "official_hook_contract": OFFICIAL_HOOK_CONTRACT,
    }:
        raise ValueError("receipt provenance mismatch")
    if receipt["state_write_authority"] is not False or receipt[
        "completion_authority"
    ] is not False:
        raise ValueError("benchmark receipt cannot carry authority")
    arms = receipt["arms"]
    if not isinstance(arms, dict) or set(arms) != {"baseline", "candidate"}:
        raise ValueError("receipt arms are invalid")
    for arm in arms.values():
        if not isinstance(arm, dict) or arm.get("samples") != parameters["samples"]:
            raise ValueError("arm measurement is invalid")
    if arms["baseline"].get("registered_event_sequence") != (
        _registered_event_sequence(baseline_contract)
    ) or arms["candidate"].get("registered_event_sequence") != (
        _registered_event_sequence(candidate_contract)
    ):
        raise ValueError("arm hook contract is invalid")
    expected_improvements = {
        "continuity_call_reduction_percent": _percent_reduction(
            arms["baseline"]["continuity_calls_per_sample"],
            arms["candidate"]["continuity_calls_per_sample"],
        ),
        "model_context_byte_reduction_percent": _percent_reduction(
            arms["baseline"]["model_context_bytes_per_sample"],
            arms["candidate"]["model_context_bytes_per_sample"],
        ),
        "hook_stdout_byte_reduction_percent": _percent_reduction(
            arms["baseline"]["hook_stdout_bytes_per_sample"],
            arms["candidate"]["hook_stdout_bytes_per_sample"],
        ),
        "wall_ms_p50_reduction_percent": _percent_reduction(
            arms["baseline"]["wall_ms_p50"], arms["candidate"]["wall_ms_p50"]
        ),
        "wall_ms_p95_reduction_percent": _percent_reduction(
            arms["baseline"]["wall_ms_p95"], arms["candidate"]["wall_ms_p95"]
        ),
    }
    if receipt["improvements"] != expected_improvements:
        raise ValueError("receipt improvement mismatch")
    expected_acceptance = {
        "candidate_deny_count_zero": arms["candidate"]["deny_count"] == 0,
        "candidate_stop_count_zero": arms["candidate"]["stop_count"] == 0,
        "continuity_call_reduction_at_least_30_percent": (
            expected_improvements["continuity_call_reduction_percent"] >= 30
        ),
        "model_context_reduction_at_least_30_percent": (
            expected_improvements["model_context_byte_reduction_percent"] >= 30
        ),
    }
    expected_acceptance["passed"] = all(expected_acceptance.values())
    if receipt["acceptance"] != expected_acceptance:
        raise ValueError("receipt acceptance mismatch")


__all__ = [
    "benchmark_codex_hook_efficiency",
    "validate_codex_hook_efficiency_receipt",
]
