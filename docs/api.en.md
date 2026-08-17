# Python API

[中文](api.md)

The command line interface provides local initialization, integrity checks, and
authorized state reads:

```bash
continuity init --root . --project-id my-project
continuity verify --root .
continuity doctor --root .
continuity state show --root .
```

Lower-level modules expose versioned contracts for integrations.

```python
from continuity_plane.sqlite_state_store import SQLiteStateStore
from continuity_plane.source_registry import SourceRegistry

registry = SourceRegistry("project-a")
store = SQLiteStateStore(".continuity/state.sqlite3")
snapshot = store.read_project("my-project")
```

State integrations should use the service boundary rather than mutating
snapshots directly. Supply a trusted request context, expected revision, and
stable request ID for every authority-bearing operation.

The package is alpha. Import paths outside the documented API may change before
the first stable release; wire schemas retain explicit versions and migration
requirements.
