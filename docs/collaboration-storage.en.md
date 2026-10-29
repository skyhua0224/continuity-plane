# Gitea mode: repository declarations and member-local data

Version 1, Alpha36. [中文](collaboration-storage.md) · [Commands](gitea-collaboration.en.md)

| Data | Location | Sharing / authority |
|---|---|---|
| Backend, instance, repository, logical credential reference | Repository .continuity/collaboration.toml | Commit this declaration; never a token or local account path |
| Rules, acceptance policy, Issue / PR templates | Versioned docs/collaboration/, AGENTS.md, .gitea/ | Git-reviewed shared policy, not dynamic task state |
| Objectives, acceptance, dependencies, driver, blockers, handoffs | Issue body and event comments | Sole shared task authority |
| Reviews, PRs, commits, merge fact | Gitea PR / Git | Review-ready or closed is not delivered |
| Local project registration and checkout paths | Data root projects/LOCAL-ID/project.json | Member-local; do not commit or copy to another member |
| Issue snapshots | projects/ID/gitea/SCOPE/cache/ | Explicit cache-only, never ownership proof |
| Pending / ambiguous writes | Same scope operations/ | Durable local journal; reconcile unknown outcomes without blind resend |
| Consumer progress | Same scope consumers/ | Local, ACK after complete pagination; no shared cursor file |
| Recovery, index, usage, reports, work directories | Local data/cache/recovery roots, optional ignored repository sidecars | Local state, not another shared task ledger; no raw transcripts in Git |
| Global rules and reference registry | Data root global-rules/, references/ | Local by default; not automatically synced to teammates |
| Fallback, completion hook, canonical write guard, host capacity and bounded worker-job index | Data-root worker-policy.json, project-context.json, host-capacity.json and projects/ID/worker-job-history.json | Member/host policy and bounded local observations; never task authority or provider-quota proof |
| Native login, token and AI session data | Each member's own tea / host profile | Never copied between machines or profiles; Continuity invokes tea without extracting tokens |

SCOPE derives from instance/repository/credential_ref. Local project IDs may differ between members. AI executor changes never transfer human ownership. Handoff requests are not acceptance.

## Repository layout

```text
.continuity/
  .gitignore
  collaboration.toml
  controller-profile.json # optional shared conventions
  knowledge/*.hash.json   # optional reviewed project knowledge
docs/collaboration/       # optional versioned policy
.gitea/                  # optional Issue / PR templates
AGENTS.md                # optional start protocol
```

```toml
[collaboration]
backend = "gitea-issues"
instance = "https://forge.example.com:3443"
repository = "team/sample"
credential_ref = "tea:work"
```

Each member creates their own work login locally. HTTPS ports and subpaths are supported. Do not add unsupported data_root, account or model-token fields. configure creates the following .continuity/.gitignore only if absent; existing content is preserved:

```gitignore
# Shared declarations and reviewed knowledge only; runtime data stays member-local.
*
!.gitignore
!collaboration.toml
!controller-profile.json
!knowledge/
!knowledge/*.json
```

If the parent repository ignores .continuity entirely, explicitly review and add only approved files. Never force-add the whole directory. No competing tasks.json, shared Todo, native session directory or credentials belong here. Existing MASTER / STATUS may hold policy or clearly sourced projections, not independently writable task authority. Enabling Gitea never auto-uploads or deletes local cards.

## Global directory and ~/.continuity

Global means this OS user / selected local profile, not a shared service. Data-root precedence is a command's explicit --data-root, CONTINUITY_DATA_ROOT, XDG_DATA_HOME/continuity, then ~/.local/share/continuity. ~/.continuity is not implicitly read as global configuration; there is no automatic ~/.continuity/config.toml.

To intentionally use that directory:

```bash
mkdir -p "$HOME/.continuity"
chmod 700 "$HOME/.continuity"
export CONTINUITY_DATA_ROOT="$HOME/.continuity"
continuity collaboration register --project-id demo --control-root /workspace/sample
continuity collaboration backend doctor --project-id demo --format json
```

Changing roots selects different local registration and cursors; it does not migrate them. Do not copy other members' roots or authentication. Use persistent private storage, not tmpfs, /tmp or /var/tmp. CLI and its MCP server must select the same root. Separate local identities may select separate roots.

```json
{
  "command": "/absolute/path/to/continuity-mcp",
  "env": {"CONTINUITY_DATA_ROOT": "/absolute/member-local/continuity-data"}
}
```

This is a host-specific stdio server fragment, not a project declaration. Omit env for defaults. tea uses its own member-local XDG configuration; do not overwrite XDG_CONFIG_HOME merely to connect a project or point it to someone else's profile.

Shared rules should live in versioned repository policy. Local rules import/export and global/reference scopes are not automatic team synchronization. Imported rules should retain their source revision and be checked for drift.

An AGENTS.md start contract should read the declaration and versioned rules, check CLI / backend doctor / sync, mutate tasks only through Issues with observed remote_revision and stable operation-id, read all pages before ACK, and keep merge/publish/deploy authorization independent. REV is an observation hash, not server CAS. Never claim a handoff or review was accepted merely because it was requested. No permanent controller, State init or extra database is required.

Missing installation uses an explicitly user-approved build and persistent prefix via the [bootstrap script](../examples/gitea-bootstrap.py). Missing credentials require native local tea login, never a pasted plaintext token.

## Alpha35 member-local worker policy

worker-policy.json stores explicit fallback candidates and optional on-job-end argv. project-context.json worker_write_guard stores canonical roots and enablement. The top-level host-capacity.json stores operator-declared client-family limits for this host. None belongs in Git, and none proves another machine's login, provider quota or live process state. Copying these files does not migrate native authentication and must never be used to query another member's account.

The canonical write guard materializes a PATH Git wrapper in persistent local data and blocks ordinary push/update-ref/receive-pack mistakes; it is not an OS sandbox. The completion hook uses local argv, a separate log and a receipt without a shell; it is not a remote wake-up service. Shared policy belongs in reviewed repository documents; each member separately configures local paths, credentials and data roots.
