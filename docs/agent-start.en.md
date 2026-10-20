# A starting point for people and agents

[中文](agent-start.md) · [Install and initialize](../USAGE.en.md) · [Deployment](deployment.en.md)

Find the current task, next action and verification entry. CLI, SSH and MCP are interchangeable. Cards and shared handoffs need no second Todo, repeated claim or inbox ritual.

## Registered project: four commands

If `.continuity/collaboration.toml` declares gitea-issues, first follow the [Gitea workflow](gitea-collaboration.en.md): check features, your own local authentication and backend sync. Human Gitea accounts own shared tasks, not the local agent assignee below. No permanent controller is required. Installation needs user authorization; never request plaintext tokens.

For controller replacement or a new Claude/Codex controller, run `continuity controller start --project-id PROJECT --member LOCAL_MEMBER --format brief --max-output-bytes 4096`. It combines a bounded plan, next action, reporting/question/tool profile and approved memory. Private sources still require local consent; shared servers use `--public-only`. See [controller continuity](controller-memory.en.md).

Run inside the project. Replace demo, agent-1 and controller with the real project, independent executor instance and controller. Concurrent workers must not share an identity.

```bash
continuity --version
continuity whoami --root . --assignee agent-1 --controller controller
continuity collaboration packet --project-id demo --assignee agent-1
continuity handoff show --project-id demo
```

Alpha29 CLI is 0.1.0a29; plugins are 0.1.0-alpha.29. First [initialize an unregistered project](../USAGE.en.md#initialize), without overwriting an existing .continuity directory. whoami neither logs into providers nor acquires a lease.

## Paste into the brief

> Use this project's Continuity cards and handoff. Check the project, stable executor identity and assigned task. Continue from an existing source-fresh, checkpoint-verified packet; otherwise read bounded packet/handoff references, not the whole history. Do not take another executor's queued card, force or claim merely for ceremony. Try one bounded symbol lookup, then narrow search on a miss. Read applicable primary documentation before implementation; record version, section and rationale. Index imports are not reading evidence. Attach the final report, commit, actual checks and unverified scope. Hand back next steps, in-flight work and pending decisions. Metadata grants no push, deployment or credential-operation authority.

Notify on changed events, blockers or decisions, not every tool use. Without a card, follow the agreed work or request assignment rather than guessing a task.

## Expand when useful

```bash
# Select only what the current task needs
continuity help task.update
continuity context lookup --root . --query YOUR_SYMBOL --format brief --max-output-bytes 1024
continuity rules export --project-id demo --audience worker --format text
continuity todo from-cards --project-id demo
```

Replace the symbol. Use complete rules and hashed attachments; card-backed Todo is a view. Explicit reference import-tsv/export-tsv exchanges an existing source index without silently overwriting conflicts. Use reference record only for actual reading/adoption, never fabricated evidence. See [role workflows](workflows.en.md) for dispatch and reports.

MCP continuity_help discovers the same operation parameters. Pass this example to continuity_plan:

```json
{"action":"task.list","project_id":"demo","format":"brief","parameters":{"assignee":"agent-1"}}
```

Keep executor_id stable across calls or configure server CONTINUITY_ASSIGNEE. A connection/session ID is not an executor. Without MCP use CLI/SSH; ordinary planning needs no State claim.

## Installation acceptance

- Check CLI, cached plugins and fresh MCP handshakes separately. Pip does not refresh plugins; repin old tags using [deployment steps](deployment.en.md#codex-plugin-and-mcp).
- Configure and verify every actual Claude profile, not only the default directory.
- Old sessions may need reconnection/new chats. Do not kill in-flight workers for upgrades. Installed configuration does not prove natural compaction delivery.
- Process native accounts, sessions and usage on their owning host/profile. Share approved project artifacts, never copied account directories.

See [validation](validation.en.md) for tested and unknown scope.
