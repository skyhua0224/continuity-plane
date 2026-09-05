"""Generate measurements from the real CLI/tool-hook subprocess fixture."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_codex_advisory_hooks import AdvisoryHookTests  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = unittest.TextTestRunner(stream=sys.stderr).run(unittest.TestSuite([
            AdvisoryHookTests("test_real_cli_boundary_and_concurrent_processes_preserve_state")
        ]))
    if not result.wasSuccessful():
        return 1
    receipt = json.loads(captured.getvalue())
    receipt["observed_at"] = datetime.now(UTC).isoformat()
    receipt["measurement_source"] = "local-cli-process-fixture"
    receipt["provider_token_usage_available"] = False
    receipt["provenance"] = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in (
            "tools/run_codex_advisory_probe.py",
            "tests/test_codex_advisory_hooks.py",
            "integrations/codex/continuity-plane/scripts/continuity-hook.py",
            "integrations/codex/continuity-plane/scripts/continuity-advisory-hook.py",
            "integrations/codex/continuity-plane/hooks/hooks.json",
            "context_control_plane/codex_hook_launcher.py",
        )
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
