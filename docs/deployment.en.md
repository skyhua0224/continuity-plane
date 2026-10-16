# Deployment, upgrades and troubleshooting

[中文](deployment.md) · [Documentation](README.en.md) · [Quick start](../USAGE.en.md)

Applies to CLI `0.1.0a25` and plugin tag `v0.1.0-alpha.25`. Verify CLI installation, MCP connections and native hook delivery separately.

## Choose a topology

| Scenario | Install | Additional requirement |
|---|---|---|
| Solo or same-machine sessions | One CLI, separate registered projects | No daemon required |
| Automatic host continuation | CLI and host plugin/hooks | Actual loading, trust and event delivery |
| Separate controller/state hosts | State-host CLI/MCP, controller SSH bridge | Explicit alias, roots and owning profile |
| Strongly consistent State service | Explicit adapter/service deployment | Not a default turnkey service; see [State](state-workflows.en.md) |

Ordinary collaboration does not require a database service, another provider login or controller fencing.

## Install and upgrade

Python 3.11+; Git is needed for tracked-file retrieval. Install native agent clients separately and authenticate on their owning host.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a25'
.venv/bin/continuity --version
```

Windows PowerShell uses `py -3 -m venv .venv`, `.venv\Scripts\python.exe` and `.venv\Scripts\continuity.exe`. No execution-policy change is required to invoke those paths. This documentation refresh is not a new field installation test of every OS.

Optional global installation:

```bash
pipx install 'continuity-plane==0.1.0a25'
pipx upgrade continuity-plane --pip-args=--pre
```

Check both `continuity --version` and `pipx list`. GitHub's regular Release category does not change PyPI alpha ordering; use an exact pin or `--pre`.

If an existing pipx/uv environment refuses recreation, update its interpreter in place:

```bash
uv pip install --python /path/to/pipx/venvs/continuity-plane/bin/python --reinstall-package continuity-plane 'continuity-plane==0.1.0a25'
```

pipx display metadata may still need its normal upgrade refresh. During a network failure, a hash-verified wheel can be installed offline. Do not disable TLS verification or persist temporary offline flags into future upgrades.

## Separate data stores

| Data | Location / selector | Backup scope |
|---|---|---|
| State and governance templates | Project `.continuity/` | SQLite, checkpoint references, templates and artifacts |
| Registry, cards, messages, rules, worker records | User data root, usually `~/.local/share/continuity` | Selected data root and external attachments |
| Recoverable work | Data-root work or persistent user-state fallback | Include custom `CONTINUITY_WORK_ROOT` |
| Code index | Repository-isolated user cache | Rebuildable; not a source backup |
| Native accounts/sessions | Provider-owned profile | Not part of cross-host Continuity synchronization |

Use command-specific `--data-root` or `CONTINUITY_DATA_ROOT`. Check actual help; flags are not universal. Inspect persistent storage before using it:

```bash
continuity storage inspect --path /path/to/persistent/work
```

Coordinate writers and use a SQLite-supported backup method or a consistent quiet window. Do not copy a partially changing dataset. State bundle export/import/rollback does not back up the entire external collaboration registry.

## Codex plugin and MCP

Install the CLI, add the marketplace, then select the plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.25
codex plugin add continuity-plane@continuity-plane
```

The host `/plugins` browser can select the same marketplace. `continuity-plane-search` is independent; `continuity-plane-state` registers the full State MCP. Core provides recovery/hooks, not every MCP tool automatically.

For an explicit generic stdio connection, use `continuity-mcp`; retrieval uses `continuity-search-mcp`. Do not duplicate servers already supplied by a plugin. `continuity_plan`, `continuity_help` and `continuity_inbox` metadata operations need no prior State resume.

A marketplace pinned to an old tag does not switch tags during an ordinary refresh: inspect/select the target ref through the host's supported flow first. Then refresh or open a new session. Review hook trust through `/hooks`, then inspect:

```bash
continuity doctor --root . --codex-home /path/to/codex-profile
```

Installed configuration, trusted hooks and observed native delivery are different states. Do not replace entire Codex/provider configurations or move auth files to repair integration. [Official plugin documentation](https://developers.openai.com/plugins/build/plugins).

## Claude Code

Install under the profile that actually runs the controller:

```bash
claude plugin marketplace add skyhua0224/continuity-plane
claude plugin install continuity-plane@continuity-plane --scope user
claude plugin list
```

The local plugin uses the installed `continuity-host-hook` for SessionStart, PreCompact, PostCompact, UserPromptSubmit, PostToolUse, Stop, SubagentStart and SubagentStop. Start a new session and inspect event evidence.

Claude PostCompact verifies; SessionStart(compact) injects. Native subagent start/stop are observations, not exit codes, successful tests or complete reports. Configuration checks do not establish naturally occurring live-subagent coverage.

Do not register duplicate plugin/manual hooks. An executable CLI alone is not proof of adoption. [Official Claude plugin documentation](https://code.claude.com/docs/en/plugins).

## Separate controller and state hosts

Install/register on the state host and configure an existing SSH alias on the controller. Begin read-only:

```bash
continuity --remote state-host --remote-root /srv/project --remote-endpoint /opt/continuity/bin/continuity-remote-cli --version
continuity --remote state-host --remote-root /srv/project --remote-endpoint /opt/continuity/bin/continuity-remote-cli collaboration task list --project-id demo --format brief
```

Replace every alias/path. A remote data root belongs to the remote process environment, not an SSH URL. Do not share a live SQLite database across machines. Worktrees, adapters and native sessions must belong to the execution host/profile.

### Claude hook bridge

Keep this private controller-side configuration out of public Git:

```json
{
  "host": "state-host",
  "remote_endpoint": "/opt/continuity/bin/continuity-remote-receive",
  "mcp_endpoints": {
    "continuity": "/opt/continuity/bin/continuity-mcp",
    "continuity-search": "/opt/continuity/bin/continuity-search-mcp"
  },
  "forward_checklists": false,
  "mappings": [
    {"local_root": "/workspace/project", "remote_root": "/srv/project"}
  ]
}
```

Target only the owning profile and use the canonical remote-hook launcher:

```bash
continuity bridge install --profile /path/to/claude-profile --config-file /path/to/bridge.json --executable /opt/continuity/bin/continuity-remote-hook
```

Installation retains byte backups and manages only this bridge's hooks. Reinstall should make no changes. Check one hook per eight event types and actual configured SSH MCP handshakes. Use `supersedes_config_files` to identify replaced config paths explicitly.

The bridge can forward the bounded current explicit checklist by default; the example disables that. It does not forward historical transcript bodies. Credentials, native session stores and transcript paths must not be migrated.

### Remote retrieval

Launch `continuity-search-mcp` over SSH to run it on the state host; its tool `root` must be a **remote** repository path. Hook bridge mappings do not currently auto-translate retrieval roots. That routing convenience remains a known gap. There is no arbitrary remote file/process administration interface.

## Acceptance, troubleshooting and rollback

| Symptom | First check |
|---|---|
| New CLI but stale MCP/tools | Reconnect; check serverInfo.version and actual tools, not State reinitialization |
| No compaction continuation | Actual machine/profile, hook trust and paired save/restore records |
| Missing message/end event | Recipient, task ID, pagination cursor and wait timeout; do not skip undelivered pages |
| Missing rules | Complete rules export and attachment hashes, not silent clipping |
| Own card rejected after MCP reconnect | Stable executor identity; current ownership usability needs work, repeated force is not the normal path |
| Worker changes card before report capture | Attach the run receipt's report_file using task update --report-file, then read report; see known validation limits |
| Hook failure | Temporarily set `CONTINUITY_DISABLED=1`; retain evidence and configuration, not account deletion |

Save software versions, configuration backups and data locations before upgrading. Restore compatible hook/MCP entrypoints and reconnect after rollback. A guarded ledger v4 project rejects older dispatchers: coordinate users and retain pre-upgrade backups rather than silently downgrading its schema.

Removing software does not clear project records. Never recursively delete a whole user data root or provider profile as an uninstall step.
