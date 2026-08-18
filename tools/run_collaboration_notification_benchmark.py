#!/usr/bin/env python3
"""Run and persist the M8-10 collaboration notification benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.collaboration_notification_benchmark import (
    benchmark_collaboration_notifications,
    validate_collaboration_notification_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--event-count", type=int, default=1000)
    parser.add_argument("--campaign-steps", type=int, default=256)
    parser.add_argument("--generated-at", default="2026-08-17T13:30:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "experiments/evidence/m8-10-collaboration-notification-results.json"
        ),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    receipt = benchmark_collaboration_notifications(
        root=root,
        event_count=args.event_count,
        campaign_steps=args.campaign_steps,
        generated_at=args.generated_at,
    )
    validate_collaboration_notification_benchmark(receipt, root=root)
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(
            "M8-10 collaboration notification benchmark failed: "
            f"{receipt['verdict']['failed_gates']}"
        )

    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(receipt, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    print(
        "M8-10 collaboration notification benchmark: "
        f"events={receipt['published_events']}/{receipt['publish_attempts']} "
        f"dual={receipt['consistent_dual_session_events']}/"
        f"{receipt['dual_session_delivery_attempts']} "
        f"catch_up={receipt['offline_catch_up_deliveries']}/"
        f"{receipt['offline_catch_up_attempts']} "
        f"duplicate={receipt['duplicate_suppressions']}/"
        f"{receipt['duplicate_attempts']} "
        f"publish_p95={receipt['latency_ms']['publish']['p95_ms']:.6f}ms "
        f"campaign_attempts={receipt['campaign_scale']['validation_attempts']} "
        f"decision={receipt['verdict']['decision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
