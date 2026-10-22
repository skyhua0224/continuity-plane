# Controller continuity and scoped memory

Version 1, Alpha34. [中文](controller-memory.md). Restores working style and approved knowledge without copying native accounts, tokens, full sessions or discovered directory trees.

## Start

```bash
continuity controller start --project-id demo --member alice --format brief --max-output-bytes 4096
```

The `controller-start/v1` packet combines handoff revision/plan/next action/pending decisions, effective profile, approved private memory, repository knowledge, conflicts and an instruction boundary. It does not scan the repository or launch providers. Use `handoff show` for fresh task and worker observations.

If required context exceeds the budget, status is `needs-expansion` and no ACK is written. A complete current packet may be acknowledged with `--ack-revision REV --profile-sha256 SHA`; this means delivered, not understood or approved.

## Scopes

| Data | Location | Effect |
|---|---|---|
| Project conventions | committed `.continuity/controller-profile.json` | defaults and locked fields |
| Member preferences | owning machine private-memory profiles | global then project, subject to project locks |
| Approved knowledge | private approval; explicit export to `.continuity/knowledge/` | human-reviewed Git data |

Profile fields are language, report_format, report_triggers, question_policy, tool_preferences, model_preferences and integration_base. They grant no tool, merge, deployment, account or credential permission. Host and current project instructions prevail.

```bash
continuity profile set --project-id demo --member alice --patch-file profile.json --expected-revision 0
continuity profile show --project-id demo --member alice --format brief
```

`--public-only` reads repository defaults for shared server/SSH contexts. Private profiles and records stay on the owning host.

## Records and sharing

```bash
continuity memory note --project-id demo --member alice --record-id report-style \
  --kind preference --scope private-project --source user-instruction \
  --text 'Report conclusion first, then verified, unverified and pending items'
continuity memory adopt --project-id demo --member alice --record-id report-style --expected-revision 1 --reason 'Explicitly adopted for this project'
continuity memory notes --project-id demo --member alice --format brief
continuity memory share --project-id demo --member alice --record-id report-style --expected-revision 2 --reason 'Shared team convention'
```

Records begin as candidates and only enter a packet after adoption. Revocation and expiry remove them. Sharing writes a content-hashed file for human review; it does not stage, commit or publish. Different members and projects remain scoped. Same-OS-user agents require host sandboxing; ignore rules are not access control.

## Native notes

Native interop is read-only candidate ingestion. Consent binds member, project, local provider origin and exact Markdown/text files. Continuity does not discover directories, follow links or read auth/settings/config/session/rollout/history/secret/token sources.

```bash
continuity memory grant --project-id demo --member alice --consent-id notes-v1 --provider codex --source-file /owning/profile/note.md
continuity memory ingest --project-id demo --member alice --consent-id notes-v1
continuity memory consent-revoke --project-id demo --member alice --consent-id notes-v1
```

After one grant, `controller start` on the owning machine incrementally refreshes those exact files automatically; no repeated manual ingest is required. An unavailable source is reported without blocking plan recovery. Imported material remains candidate lessons and never becomes executable instruction. Native writeback, cross-host transfer and sharing require separate authorization. Alpha34 still does not mutate native files or claim bidirectional writeback. Private MCP access is disabled by default; an owning local stdio server may set `CONTINUITY_PRIVATE_MEMORY_MCP=1`, with no other-member override. Shared servers use public-only context.

## Handoff and failures

`handoff write` saves declarations quickly; `handoff show` computes current facts, while `--auto` generates observation candidates. `--capture-member alice` stores a private profile snapshot on the member's host outside shared handoff.

Inbox archive moves only acknowledged messages to recoverable per-ID files, preserves unread messages and suppresses duplicate resend. Observed quota/rate/capacity failures pause the exact origin/model; unknown quota resets do not trigger automatic retry or invented availability.

```bash
continuity dispatch-health show --project-id demo --format brief
continuity dispatch-health clear --project-id demo --provider codex --model MODEL --reason 'Reconciled against a newer observed run'
```

Reservation errors report job, owner, time and duplicate/stale type. `task batch-update --file` processes at most 50 revisioned items, stops on first failure and is not a cross-Issue transaction.

Acceptance checks observable continuation: same next action, reporting/question/tool habits, no repeated answered questions and no candidate/expired memory treated as approval. Model internal state is not claimed identical. Memory cannot broaden external authority, transfer human ownership or replace Issue/PR authority.
