"""Non-blocking tool entry compatible with the released two-argument launcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def main() -> int:
    try:
        path = Path(__file__).with_name("continuity-hook.py")
        spec = importlib.util.spec_from_file_location("continuity_advisory", path)
        if spec is None or spec.loader is None:
            return 0
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.advisory_main()
    except Exception:
        # Optional tool observations never acquire command authority.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
