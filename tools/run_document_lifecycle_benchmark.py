#!/usr/bin/env python3
"""Run and publish the M0-10 document lifecycle benchmark."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.document_lifecycle_benchmark import (
    benchmark_document_lifecycle,
    load_document_lifecycle_benchmark_baseline,
    load_document_lifecycle_benchmark_config,
    validate_document_lifecycle_benchmark_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="run document lifecycle benchmark")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--generated-at", default="2026-08-12T00:00:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/state/m0-10-document-lifecycle-results.yaml"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    config = load_document_lifecycle_benchmark_config(root)
    baseline_ref = config["baseline_git_ref"]
    samples = config["validator_samples"]
    manifest = yaml.safe_load(
        (root / "profiles/document-control-manifest.yaml").read_text(encoding="utf-8")
    )
    baseline_status, baseline_master = load_document_lifecycle_benchmark_baseline(
        root, baseline_ref
    )
    receipt = benchmark_document_lifecycle(
        root,
        manifest,
        baseline_ref=baseline_ref,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
        generated_at=args.generated_at,
        samples=samples,
    )
    live = validate_document_lifecycle_benchmark_receipt(
        root,
        manifest,
        receipt,
        baseline_ref=baseline_ref,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
    )
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(receipt, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(
        "document lifecycle benchmark: "
        f"status={receipt['metrics']['status_bytes_reduction_percent']}% "
        f"master_section={receipt['metrics']['master_max_section_reduction_percent']}% "
        f"live_p95={live['validator_p95_ms']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
