# Plans, identities and handoffs

[中文](onboarding.md) · [Start](../USAGE.en.md) · [Role workflows](workflows.en.md)

## Take the shortest path

Existing CLI/SSH scripts can remain the primary workflow when cards, handoffs, wait and batches suffice. MCP, Todo, claims and inbox are not compulsory rituals. MCP is another entry point, not automatic discovery of a remote checkout.

## Identity and cards

Declare the project, stable executor and controller. whoami reports this invocation, not persistent account registration or a lease:

```bash
continuity whoami --root . --assignee worker-a --controller controller
continuity collaboration packet --project-id demo --assignee worker-a
continuity collaboration task list --project-id demo --assignee worker-a --format brief
```

Before dispatch, make the body, actual worktree, file scope, build/test expectations and reviewer readable. Packet must not present somebody else's queued card as yours. Declared allowed-file is not OS enforcement.

MCP reconnect/card ownership usability remains a known gap. Check stable identity first; repeated force is not normal adoption or permission to take another worker's card.

## Start and finish

Read relevant packet, rules, body and code routes; retrieve inbox when messages matter. Finish with report, commit, actual checks and explicit unverified scope for controller review. Supervisor completion projects review/blocked status; Alpha26 also attaches the current attempt's report after early card updates, preserving manual status. See [validation](validation.en.md).

## Durable inbox

```bash
continuity collaboration inbox send --project-id demo --message-id handoff-a --sender controller --recipient worker-a --body 'Please check the current report'
continuity collaboration inbox list --project-id demo --recipient worker-a
continuity collaboration inbox ack --project-id demo --recipient worker-a --message-id handoff-a
```

Same ID/content is idempotent; changed content is refused. Reading does not acknowledge. Use artifact references for long reports. Messages neither guarantee wake-up nor grant authority.

## Typed State planning

Do not overwrite existing MASTER/STATUS with templates. Use [State adoption](state-workflows.en.md) for previews, explicit apply and revision/CAS validation. Ordinary collaboration needs no prior State resume.

## Remote and MCP acceptance

Use [explicit SSH CLI/MCP and hook bridging](deployment.en.md#separate-controller-and-state-hosts). A data root is not a remote URL. Never copy native accounts, profiles or sessions to query elsewhere.

Verify actual tools/version after installation. Running MCP sessions need reconnection or a new session; do not reinitialize State, change keys or loop installation. Remote retrieval takes a remote root path; hook mappings do not automatically translate it.
