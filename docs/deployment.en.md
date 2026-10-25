# Deployment, upgrades and troubleshooting

[中文](deployment.md) · [Documentation](README.en.md) · [Quick start](../USAGE.en.md)

Applies to CLI `0.1.0a35` and plugin tag `v0.1.0-alpha.35`. Verify CLI installation, MCP connections and native hook delivery separately.

## Choose a topology

| Scenario | Install | Additional requirement |
|---|---|---|
| Solo or same-machine sessions | One CLI, separate registered projects | No daemon required |
| Members sharing private Issues/PRs | Member-local CLI/MCP + tea, same repository | No Continuity server/shared DB; [storage contract](collaboration-storage.en.md) |
| Automatic host continuation | CLI and host plugin/hooks | Actual loading, trust and event delivery |
| Separate controller/state hosts | State-host CLI/MCP, controller SSH bridge | Explicit alias, roots and owning profile |
| Strongly consistent State service | Explicit adapter/service deployment | Not a default turnkey service; see [State](state-workflows.en.md) |

Ordinary collaboration does not require a database service, another provider login or controller fencing.

## Install and upgrade

Python 3.11+; Git is needed for tracked-file retrieval. Install native agent clients separately and authenticate on their owning host.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a35'
.venv/bin/continuity --version
```

Windows PowerShell uses `py -3 -m venv .venv`, `.venv\Scripts\python.exe` and `.venv\Scripts\continuity.exe`. No execution-policy change is required to invoke those paths. This documentation refresh is not a new field installation test of every OS.

Optional global installation:

```bash
pipx install 'continuity-plane==0.1.0a35'
pipx upgrade continuity-plane --pip-args=--pre
```

Check both `continuity --version` and `pipx list`. GitHub's regular Release category does not change PyPI alpha ordering; use an exact pin or `--pre`.

If an existing pipx/uv environment refuses recreation, update its interpreter in place:

```bash
uv pip install --python /path/to/pipx/venvs/continuity-plane/bin/python --reinstall-package continuity-plane 'continuity-plane==0.1.0a35'
```

pipx display metadata may still need its normal upgrade refresh. During a network failure, a hash-verified wheel can be installed offline. Do not disable TLS verification or persist temporary offline flags into future upgrades.

## Separate data stores

In Gitea mode, [repository/global configuration](collaboration-storage.en.md) separates shared Issue/PR authority from local State/registries. ~/.continuity is used only if explicitly selected as a data root, never as an implicit global config file.

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

### Optional Alpha35 member-local policy

These settings belong to the selected local data root and host. They do not sync across machines or prove provider account quota.

```bash
# Operator-declared capacity shared across registered projects on this host
continuity controller host-capacity --project-id demo \
  --owner controller --token "$CONTROLLER_TOKEN" \
  --host-client-limit total=8

# Configured candidates used only after same-session transient retry exhaustion
continuity worker fallback-policy set --project-id demo \
  --model primary --candidate secondary

# Optional ordinary Git-ref write protection for a canonical checkout
continuity collaboration context-set --project-id demo \
  --integration-root /workspace/demo --integration-base main \
  --worker-write-policy canonical-read-only \
  --canonical-repository-root /workspace/demo
```

Host capacity is a local run gate, not total account quota. The Git wrapper is materialized under the local persistent data directory; it guards ordinary PATH invocation of push/update-ref/receive-pack and is not a mount/namespace sandbox. Use a separate OS user, read-only mount or VM for strong isolation. Configure an optional local completion argv with `worker completion-hook set --argv-json ...`; its log and receipt stay in the job directory and it is not a remote wake-up channel.

## Codex plugin and MCP

Install the CLI, add the marketplace, then select the plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.35
codex plugin add continuity-plane@continuity-plane
```

Select retrieval or full State MCP independently. On clients that support `plugin add`:

```bash
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
```

Otherwise select the same entries in the plugin browser. Do not overwrite configuration files to install them.

The host `/plugins` browser can select the same marketplace. `continuity-plane-search` is independent; `continuity-plane-state` registers the full State MCP. Core provides recovery/hooks, not every MCP tool automatically.

For an explicit generic stdio connection, use `continuity-mcp`; retrieval uses `continuity-search-mcp`. Do not duplicate servers already supplied by a plugin. `continuity_plan`, `continuity_help` and `continuity_inbox` metadata operations need no prior State resume.

A marketplace pinned to an old tag does not switch tags during an ordinary refresh: inspect/select the target ref through the host's supported flow first. Then refresh or open a new session. Review hook trust through `/hooks`, then inspect:

If add reports a different registered source, back up the owner-host Codex configuration and reset only this marketplace registration. Do not uninstall plugins or delete account data:

```bash
# Repin only the Continuity marketplace
codex plugin marketplace remove continuity-plane
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.35
codex plugin marketplace upgrade continuity-plane
```

Repeat the add commands above for the plugins you use, then inspect actual cached installation versions, not just pip:

```bash
codex plugin list --marketplace continuity-plane --json
```

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

The local plugin uses the installed `continuity-host-hook` for SessionStart, SessionEnd, PreCompact, PostCompact, UserPromptSubmit, PostToolUse, Stop, SubagentStart and SubagentStop. Start a new session and inspect event evidence.

Claude PostCompact verifies; SessionStart(compact) injects. Native subagent start/stop are observations, not exit codes, successful tests or complete reports. Configuration checks do not establish naturally occurring live-subagent coverage.

### Multiple Claude profiles

Install/update each actual profile separately on its owner host. Paths below are examples; never copy another profile's credentials, sessions or entire configuration:

```bash
# Run on this profile's owning host; repeat separately for other profiles
CLAUDE_CONFIG_DIR=/path/to/profile-a claude plugin marketplace update continuity-plane
CLAUDE_CONFIG_DIR=/path/to/profile-a claude plugin update continuity-plane@continuity-plane --scope user
```

Run claude plugin list with the same CLAUDE_CONFIG_DIR, then reconnect/start a new session. For the SSH bridge below, do not also enable duplicate local plugin hooks. Keep an explicit bridge config/project mapping per profile and run bridge install for each. Verify that mode through its hook runtime path and fresh MCP serverInfo.version, rather than requiring a duplicate plugin-list entry. A shared public package is not shared native account/session state.

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

Launch `continuity-search-mcp` over SSH with a remote repository `root`, or use the local search-MCP routing introduced in Alpha29 and retained in Alpha35: set tool argument `bridge_config` or server environment `CONTINUITY_BRIDGE_CONFIG=/path/to/bridge.json`. An exact local_root maps to remote_root and invokes `continuity-remote-cli`. Optional settings are `remote_cli_endpoint`, `lookup_timeout_seconds` and owning-host `remote_data_root`. Missing/ambiguous mappings or transport failure produce an error, never local fallback. Remote mode rejects local cache_path/data_root. There is no arbitrary file/process administration. This route has synthetic protocol tests, not a new Alpha35 two-machine field run.

Set server environment `CONTINUITY_ASSIGNEE=worker-a` or pass the same `executor_id` on each `continuity_plan` call. Task add/update/claim and Todo activation use that stable identity; other executors' conflicts remain rejected. session_id describes recovery and no longer becomes the card assignee. This is declared collaboration identity, not native account authentication; do not configure all workers with one identity.

A controller configured with `CONTINUITY_ROLE=controller` can annotate task metadata without an explicit executor_id/assignee; this preserves the worker's assignment instead of reassigning it to the controller. Explicit executor_id still checks ownership. This routing convention grants no external-operation authority.

## Acceptance, troubleshooting and rollback

| Symptom | First check |
|---|---|
| New CLI but stale MCP/tools | Reconnect; check serverInfo.version and actual tools, not State reinitialization |
| MCP absent after host kill/reboot | The host-owned MCP does not survive reboot. Open a new host MCP or use CLI/explicit SSH remote until reconnected; `collaboration doctor` reports the lifecycle boundary |
| No compaction continuation | Actual machine/profile, hook trust and paired save/restore records |
| Missing message/end event | Recipient, task ID, pagination cursor and wait timeout; do not skip undelivered pages |
| Worker ended but monitoring did not wake | Reconnect `worker events --since-cursor … --follow`. Checkpoints and hooks do not replace a durable event stream |
| job.json lacks status/exit_code | New Alpha35 jobs synchronize terminal state; reconcile legacy jobs from result.json without inventing missing facts |
| Missing rules | Complete rules export and attachment hashes, not silent clipping |
| Own card rejected after MCP reconnect | Check CONTINUITY_ASSIGNEE/executor_id against the stored assignee; old connection IDs are not silently migrated |
| Worker changes card before report capture | Fixed from Alpha29; check old runtimes, stale attempts or reassignment. Historical report files can still be attached explicitly |
| Dispatch/resume stays CPU-bound | From Alpha35, inspect current execution bindings first: no running attempt must not replay history. Then inspect `dispatch-safety-index.json` and the change-log tail; do not delete history |
| Host capacity or expired cooldown is confusing | Alpha35 computes expired in show and no longer keeps dispatch blocked after reset. Clear by model and use `--all-matching` for multiple provider records |
| Worker pushed into a canonical repository | Confirm canonical-read-only and its roots were enabled. Absolute git paths or another program can bypass the wrapper; use OS isolation rather than claiming a sandbox |
| HTTP 403/Cloudflare is labelled policy-review | Inspect original `output_tail`; absent an explicit model safety marker, resume reclassifies from the HTTP/auth evidence. If ambiguity remains, review the exact job and use `worker clear-policy-review --reason …` |
| Hook failure | Temporarily set `CONTINUITY_DISABLED=1`; retain evidence and configuration, not account deletion |

Save software versions, configuration backups and data locations before upgrading. Restore compatible hook/MCP entrypoints and reconnect after rollback. A guarded ledger v4 project rejects older dispatchers: coordinate users and retain pre-upgrade backups rather than silently downgrading its schema. Inspect host-restart leftovers with `controller reservations --state stale`; with the controller lease, run `controller reconcile-host --apply --reason …`.

Removing software does not clear project records. Never recursively delete a whole user data root or provider profile as an uninstall step.
