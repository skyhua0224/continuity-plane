# Typed planning records and executor handoffs

[中文](onboarding.md)

## Typed planning records

MASTER defines goals, order, decisions and constraints. State stores typed objects and events.
Editing Markdown does not update State automatically. Add records explicitly:

```bash
continuity work add --root . --work-id stage-a --title "Stage A" \
  --source-file .continuity/MASTER.md --scope testing
```

The default is a read-only preview with `expected_revision`. After reviewing it, repeat the
same command with `--apply --expected-revision REVISION`. Writes pass through State MCP
authorization, CAS and validators, preserve existing Work/claims/evidence, and refresh the
checkpoint and STATUS projection. Identical additions create no duplicate event. Conflicting
content under the same ID is rejected without overwriting a record.

After editing canonical MASTER/STATUS, use explicit `--refresh-source`. The preview lists
changed sources; apply binds refreshed source evidence and records in the same State event.
With an active claim, specify its owner as `--actor-ref`. The claim, scope, other source
contents and existing evidence are preserved. This operation never renews or replaces a claim.

```bash
continuity decision add --root . --decision-id choice-a --work-id stage-a \
  --statement "Use local execution" --source-file .continuity/MASTER.md
continuity constraint add --root . --constraint-id boundary-a --work-id stage-a \
  --statement "Preserve production configuration" --source-file .continuity/MASTER.md
```

Work and decisions default to `proposed`; an explicitly approved decision can use
`--record-status accepted`. A source hash establishes provenance, not successful tests or
Work completion. Add referenced parent/dependency Work first. Existing `work activate`
can activate a recorded Work with matching title/scope; incomplete dependencies are not skipped.
No claim or external-effect authority is granted by recording a plan.

`committed-repair-required` means the State event is saved, not rolled back. Follow its
`next_action` to repair the checkpoint, then run `checkpoint verify`.

## Task cards and lanes

```bash
continuity collaboration task update --root . --task-id narrow-fix \
  --status active --assignee worker/subtask
```

Registered roots resolve the project identity; explicit `--project-id` is also supported.
One assignee may hold one active card in each lane. A lane still allows only one active card
per assignee. Child identities such as `worker/subtask` are supported. Cards are advisory
metadata, not State authorization or a gate on ordinary development.

## Durable inbox

```bash
continuity collaboration inbox send --root . --message-id handoff-a \
  --sender reviewer --recipient worker --body "Check stage A evidence"
continuity collaboration inbox list --root . --recipient worker
continuity collaboration inbox ack --root . --recipient worker --message-id handoff-a
```

The MCP tool `continuity_inbox` supports `send`, `list` and `ack`. Delivery is project- and
recipient-scoped, and retries with the same message ID are idempotent. Bodies are limited to
2048 UTF-8 bytes; use file references for longer handoffs, and never send credentials. Reading
does not acknowledge a message. Acknowledge after processing.

With an assignee, `continuity_collaboration_packet` includes up to three bounded inbox hints.
Lifecycle hooks can inject hints when the recipient matches the actual Session identity.
Hints carry no authority and do not automatically interrupt current work. Session-addressed
messages must use the actual Session ID as recipient.

This release provides durable delivery and retrieval, **not guaranteed wake-up of a sleeping
or host-owned session**. It does not launch a second provider process to compete for a writer.
Cross-device handoffs still require explicit synchronization of collaboration data or files.

Controllers can use `collaboration changes --since CURSOR` for incremental events instead of
polling each executor. `context lookup` and `context search` support `--format brief` for model
first reads; JSON remains for scripts. Shared project rules and unresolved commitments use
`collaboration rule-add/rule-list` and `commitment-add/commitment-list/commitment-close`, and
are included in bounded packets.

## Verify MCP activation

## First-run example

Minimal controller/executor flow:

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
continuity collaboration task add --project-id demo --task-id task-a --lane-id core \
  --mode patch --priority p1 --title "Implement A" --objective "Complete A" \
  --next-action "Run tests" --exit-criterion "Tests pass" --assignee worker-a
continuity collaboration packet --project-id demo --assignee worker-a
continuity collaboration task update --project-id demo --task-id task-a --status ready-for-review \
  --branch feat/a --commit abcdef1 --verification "test command: pass"
```

An executor starts with `whoami`, `packet`, and `inbox list`, then closes with
`task update --status ready-for-review` and a message to the controller. For state on another
machine, point `--data-root` at an SSH-mounted or explicitly synchronized directory; Continuity
does not start SSH implicitly.

Running MCP clients may not hot-load new servers or updated tool catalogs. A successful
package install does not prove the current session sees its tools. Inspect the tool list;
reconnect/reload using the host's supported mechanism, or start a new session if necessary.
Do not rebuild project State or edit provider keys. Ordinary work continues when MCP is
unavailable; use the CLI temporarily instead of entering an installation retry loop.

`continuity status --root .` is equivalent to `continuity status render --root .`.
Root-aware commands also accept `continuity --root PROJECT_ROOT COMMAND ...`.
If both root positions are used, they must resolve to the same project.
Expected CLI input, file and collaboration errors return structured errors and a nonzero
exit code, without a Python traceback.
