#!/usr/bin/env python3
"""Compare duplicate and coordinated Codex work on three real repositories."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOTS = {
    "platform": Path("/home/skyhua/Projects/alkaidlab-platform"),
    "foundation": Path("/home/skyhua/Projects/foundation-sunshine"),
    "moonlight": Path("/home/skyhua/Projects/moonlight-vplus"),
}
EXPECTED = {"platform": True, "foundation": False, "moonlight": False}


def _parse(output: str) -> dict[str, Any]:
    usage = None
    message = None
    tools: list[str] = []
    warnings = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            message = item.get("text")
        elif item_type == "error":
            warnings += 1
        elif item_type != "reasoning":
            material = {
                key: item.get(key)
                for key in ("type", "command", "path", "name")
                if item.get(key) is not None
            }
            tools.append(
                hashlib.sha256(
                    json.dumps(material, sort_keys=True).encode()
                ).hexdigest()
            )
    if not isinstance(usage, dict) or not isinstance(message, str):
        raise TypeError("Codex collaboration result is incomplete")
    try:
        answer = json.loads(message)
    except json.JSONDecodeError as exc:
        raise TypeError("Codex collaboration answer is not JSON") from exc
    return {
        "usage": usage,
        "answer": answer,
        "tool_hashes": tools,
        "warnings": warnings,
    }


def _invoke(prompt: str) -> dict[str, Any]:
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-sol",
        "-C",
        str(ROOTS["platform"]),
        "--add-dir",
        str(ROOTS["foundation"]),
        "--add-dir",
        str(ROOTS["moonlight"]),
        prompt,
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOTS["platform"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Codex collaboration worker failed")
    return {
        **_parse(completed.stdout),
        "wall_time_ms": (time.perf_counter() - started) * 1000,
    }


def _parallel(prompts: list[str]) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(prompts)) as executor:
        results = list(executor.map(_invoke, prompts))
    return results, (time.perf_counter() - started) * 1000


def _metrics(results: list[dict[str, Any]], wall_time_ms: float) -> dict[str, Any]:
    all_hashes = [value for result in results for value in result["tool_hashes"]]
    return {
        "provider_calls": len(results),
        "input_tokens": sum(result["usage"]["input_tokens"] for result in results),
        "cached_input_tokens": sum(
            result["usage"]["cached_input_tokens"] for result in results
        ),
        "tool_calls": len(all_hashes),
        "duplicate_tool_calls": len(all_hashes) - len(set(all_hashes)),
        "worker_wall_time_ms": sum(result["wall_time_ms"] for result in results),
        "parallel_wall_time_ms": wall_time_ms,
        "warning_count": sum(result["warnings"] for result in results),
    }


def _uncoordinated() -> dict[str, Any]:
    prompt = (
        "Inspect all three repositories using read-only tools. Determine whether "
        "each repository currently contains AlkModuleMessage or alk_module_message. "
        "Return exactly JSON with boolean keys platform, foundation, moonlight. "
        "Repositories: /home/skyhua/Projects/alkaidlab-platform, "
        "/home/skyhua/Projects/foundation-sunshine, "
        "/home/skyhua/Projects/moonlight-vplus."
    )
    results, wall = _parallel([prompt, prompt])
    quality = all(result["answer"] == EXPECTED for result in results)
    return {"metrics": _metrics(results, wall), "quality": quality}


def _coordinated() -> dict[str, Any]:
    platform_prompt = (
        "Inspect only /home/skyhua/Projects/alkaidlab-platform with read-only tools. "
        'Return exactly JSON {"platform":true} if AlkModuleMessage or '
        "alk_module_message exists, otherwise false."
    )
    products_prompt = (
        "Inspect only /home/skyhua/Projects/foundation-sunshine and "
        "/home/skyhua/Projects/moonlight-vplus with read-only tools. Return exactly "
        "JSON with boolean keys foundation and moonlight for whether "
        "AlkModuleMessage or alk_module_message exists."
    )
    workers, wall = _parallel([platform_prompt, products_prompt])
    merged = {**workers[0]["answer"], **workers[1]["answer"]}
    verifier_prompt = (
        "Do not call tools. Verify these two worker outputs against expected task "
        f"semantics and return exactly {json.dumps(EXPECTED, separators=(',', ':'))}. "
        f"Worker outputs: {json.dumps([item['answer'] for item in workers])}"
    )
    verifier = _invoke(verifier_prompt)
    worker_metrics = _metrics(workers, wall)
    metrics = _metrics(workers + [verifier], wall + verifier["wall_time_ms"])
    quality = merged == EXPECTED and verifier["answer"] == EXPECTED
    return {"metrics": metrics, "worker_metrics": worker_metrics, "quality": quality}


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for condition in ("uncoordinated", "coordinated"):
        rows = [sample for sample in samples if sample["condition"] == condition]
        result[condition] = {
            "samples": len(rows),
            "input_tokens_mean": sum(row["metrics"]["input_tokens"] for row in rows)
            / len(rows),
            "tool_calls_mean": sum(row["metrics"]["tool_calls"] for row in rows)
            / len(rows),
            "duplicate_tool_calls_mean": sum(
                row["metrics"]["duplicate_tool_calls"] for row in rows
            )
            / len(rows),
            "parallel_wall_time_ms_mean": sum(
                row["metrics"]["parallel_wall_time_ms"] for row in rows
            )
            / len(rows),
            "quality_rate": sum(row["quality"] for row in rows) / len(rows),
        }
        if condition == "coordinated":
            result[condition]["workers_only_input_tokens_mean"] = sum(
                row["worker_metrics"]["input_tokens"] for row in rows
            ) / len(rows)
            result[condition]["workers_only_tool_calls_mean"] = sum(
                row["worker_metrics"]["tool_calls"] for row in rows
            ) / len(rows)
            result[condition]["workers_only_parallel_wall_time_ms_mean"] = sum(
                row["worker_metrics"]["parallel_wall_time_ms"] for row in rows
            ) / len(rows)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run real Codex collaboration A/B")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "/home/skyhua/Projects/context-control-plane/experiments/evidence/"
            "m10-01-codex-collaboration-results.json"
        ),
    )
    args = parser.parse_args(argv)
    samples = []
    for index in range(args.iterations):
        samples.append(
            {
                "sample_id": f"uncoordinated-{index}",
                "condition": "uncoordinated",
                **_uncoordinated(),
            }
        )
        samples.append(
            {
                "sample_id": f"coordinated-{index}",
                "condition": "coordinated",
                **_coordinated(),
            }
        )
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-collaboration-ablation/v1alpha1",
        "benchmark_id": "m10-01-codex-collaboration",
        "generated_at": "2026-08-18T03:00:00+08:00",
        "repository_revisions": {
            name: subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for name, path in ROOTS.items()
        },
        "samples": samples,
        "summary": _summary(samples),
        "provider_invocations": args.iterations * 5,
        "external_side_effects": 0,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
