"""Initialize and verify a local-embedded control-plane profile."""

from __future__ import annotations

import tempfile
from pathlib import Path

from continuity_plane.cli import main


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    main(["init", "--root", str(root), "--project-id", "sample-app"])
    main(["verify", "--root", str(root)])
