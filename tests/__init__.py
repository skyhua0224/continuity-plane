"""Continuity regression tests; explicit package identity for subprocess fixtures."""

import atexit
import os
import tempfile

# An implicit CLI init/hook in a fixture must never mutate the real registry.
# Children inherit the same disposable test root; an explicit copied data root
# supplied by the caller remains supported. No account/profile store is changed.
if not os.environ.get("CONTINUITY_DATA_ROOT"):
    _test_registry = tempfile.TemporaryDirectory(prefix="continuity-test-registry-")
    os.environ["CONTINUITY_DATA_ROOT"] = _test_registry.name
    atexit.register(_test_registry.cleanup)
