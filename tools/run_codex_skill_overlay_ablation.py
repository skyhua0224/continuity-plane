#!/usr/bin/env python3
"""Measure Codex global Skill loading with full, selected and empty overlays."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.codex_usage_benchmark import parse_codex_jsonl_result

_SOURCE_HOME = Path("/home/skyhua/.codex")
_PROMPT = (
    "Do not call tools. Return exactly one JSON object with verdict=ok and "
    "next_action=M10-00."
)


def _prepare_home(root: Path, selected: list[str] | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth.json").symlink_to(_SOURCE_HOME / "auth.json")
    (root / "config.toml").write_text(
        """model_provider = "custom"
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
model_context_window = 1000000
model_auto_compact_token_limit = 700000
model_catalog_json = "/home/skyhua/.codex/model-catalog-1m.json"
disable_response_storage = true

[model_providers.custom]
name = "Ciii Codex Luke"
base_url = "http://127.0.0.1:15722/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "PROXY_MANAGED"
""",
        encoding="utf-8",
    )
    if selected:
        skills = root / "skills"
        skills.mkdir()
        for skill in selected:
            source = _SOURCE_HOME / "skills" / skill
            if not source.is_dir():
                raise RuntimeError(f"selected Skill is unavailable: {skill}")
            (skills / skill).symlink_to(source, target_is_directory=True)
    return root


def _run(root: Path, home: Path, condition: str, index: int) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
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
        str(root),
        "--skip-git-repo-check",
        _PROMPT,
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Skill overlay condition failed: {condition}-{index}")
    parsed = parse_codex_jsonl_result(completed.stdout)
    return {
        "sample_id": f"{condition}-{index}",
        "condition": condition,
        "usage": parsed["usage"],
        "quality": parsed["quality"],
        "warning_count": len(parsed["warnings"]),
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(sample["condition"], []).append(sample)
    result = {}
    for condition, items in grouped.items():
        tokens = [item["usage"]["input_tokens"] for item in items]
        result[condition] = {
            "count": len(items),
            "input_tokens_mean": sum(tokens) / len(tokens),
            "quality_rate": sum(item["quality"]["correct"] for item in items)
            / len(items),
            "warning_count_total": sum(item["warning_count"] for item in items),
        }
    full = result["full"]["input_tokens_mean"]
    for condition in ("selected", "empty"):
        result[condition]["reduction_vs_full_percent"] = round(
            (full - result[condition]["input_tokens_mean"]) / full * 100,
            4,
        )
    return result


def _skill_inventory(paths: list[Path]) -> dict[str, int]:
    files = sorted(
        file for path in paths for file in path.rglob("SKILL.md") if file.is_file()
    )
    return {
        "skill_count": len(files),
        "skill_source_bytes": sum(file.stat().st_size for file in files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run Codex Skill overlay ablation")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-codex-skill-overlay-results.json"),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 10:
        raise SystemExit("iterations must be 1..10")
    root = args.root.resolve()
    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="context-skill-overlay-") as directory:
        temporary = Path(directory)
        selected_home = _prepare_home(
            temporary / "selected",
            ["human-first-writing", "session-memory-hygiene"],
        )
        empty_home = _prepare_home(temporary / "empty", [])
        for condition, home in (
            ("full", _SOURCE_HOME),
            ("selected", selected_home),
            ("empty", empty_home),
        ):
            for index in range(args.iterations):
                samples.append(_run(root, home, condition, index))
    full_inventory = _skill_inventory([_SOURCE_HOME / "skills"])
    selected_inventory = _skill_inventory(
        [
            _SOURCE_HOME / "skills" / "human-first-writing",
            _SOURCE_HOME / "skills" / "session-memory-hygiene",
        ]
    )
    skill_reduction = round(
        (
            full_inventory["skill_source_bytes"]
            - selected_inventory["skill_source_bytes"]
        )
        / full_inventory["skill_source_bytes"]
        * 100,
        4,
    )
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-skill-overlay-ablation/v1alpha1",
        "benchmark_id": "m10-00-codex-skill-overlay",
        "generated_at": "2026-08-18T01:30:00+08:00",
        "selected_skill_ids": [
            "human-first-writing",
            "session-memory-hygiene",
        ],
        "samples": samples,
        "summary": _summary(samples),
        "skill_inventory": {
            "full": full_inventory,
            "selected": selected_inventory,
            "source_bytes_reduction_percent": skill_reduction,
        },
        "provider_invocations": len(samples),
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
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
