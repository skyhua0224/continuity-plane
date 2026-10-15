# Alpha24 — reliable workers and low-cost continuity

Version: `0.1.0a24` / plugin tag `v0.1.0-alpha.24`.

Worker lifecycle, bounded same-session recovery, event pagination/waiting, report capture,
commit-bound evidence, rule delivery, prompt assembly, task-backed Todo, batches and actual-run
recommendations share the `continuity` CLI. CLI/MCP/help and host guidance expose the same planning
operations. Policy refusal and incomplete provider output stop automatic replay; they do not select
another channel to avoid a refusal. Missing ordinary metadata warns without stopping authorized work.

The explicit SSH bridge supports a controller whose authoritative project state is on another host.
An actual native manual Claude `/compact` on its owning host/profile passed save/restore and exact
return-point checks. Automatic-threshold, large-context and long business-session acceptance remain
unverified. Existing MCP connections must reconnect; installing software cannot hot-reload them.

Raw credentials, private provider configuration and native session stores remain on the owning
host/profile. Only authorized project artifacts and normalized labeled observations cross hosts.
Recoverable work uses persistent storage; temporary buffers are disposable. No outside account-system
integration is included. Native Claude automatic quota querying is withdrawn; no polling service runs.
Real token/cache ingestion and explicit quota records remain independent of that withdrawn plan.

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a24'
continuity --version
```

For existing pipx environments that use the uv backend:

```bash
uv pip install --python /path/to/pipx/venvs/continuity-plane/bin/python \
  --reinstall-package continuity-plane 'continuity-plane==0.1.0a24'
```

Upgrade participating CLI/MCP runtimes together before enabling newer ledger features. Start a new
MCP connection afterward; do not terminate other sessions. See [field workflows](field-workflows.md)
and [usage](../USAGE.en.md) for setup and the evidence coverage table. This release does not claim
actual monetary savings, a universal token reduction rate, or newly verified macOS/Windows field use.
