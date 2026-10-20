# Private Gitea Issue / PR collaboration

Version 1, Alpha29 (`0.1.0a29`). The initial contract targets Gitea v1.24.7; check other deployments before adoption. See the [repository/global configuration contract](collaboration-storage.en.md) and [Chinese walkthrough](gitea-collaboration.md).

Members run separate local CLI / MCP installations using their own Gitea accounts. Issues and event comments own tasks; PR / Git owns reviews and delivery. Local snapshots are caches. No Continuity server, shared database or permanent controller is required. Humans can participate from the Gitea UI.

## Connect and start

Alpha29 accepts existing tea aliases including spaces, colons and Unicode, e.g. `--credential-ref 'tea:Company Gitea:3443'`. Do not rename or copy native logins or extract tokens to fit a schema.

```bash
continuity collaboration register --project-id demo --control-root .
continuity collaboration backend configure --project-id demo \
  --instance https://forge.example.com:3443 --repository team/sample --credential-ref tea:work
continuity collaboration backend doctor --project-id demo --format json
```

configure checks actual account / repository / Issue read access, then creates `.continuity/collaboration.toml`. Commit only this declaration. Each member uses their own local Gitea CLI, tea, with api --include / --data support (verified with tea 0.15). Continuity never reads or exports its token config. It checks public login metadata via tea logins list --output json, binds the instance URL and explicitly selects the login and repository on every request.

```toml
[collaboration]
backend = "gitea-issues"
instance = "https://forge.example.com:3443"
repository = "team/sample"
credential_ref = "tea:work"
```

Custom HTTPS ports and instance subpaths are supported. TLS, CA trust and redirect policy are managed by member-local tea; Continuity never sets insecure or handles authentication headers. Configure trusted HTTPS in tea. Arbitrary environment-secret references are forbidden. No permissions or repository settings are modified. doctor separates read connectivity, repository permissions, feature switches and instance-advertised review-request support; read success cannot prove token write scope.

## Tasks and reviews

```bash
continuity task add --project-id demo --task-id parser-fix --lane-id parser \
  --mode patch --priority p1 --title 'Fix parser' --objective 'Meet protocol requirements' \
  --next-action 'Add regression' --exit-criterion 'Tests pass' --body 'Background and tests' --operation-id create-1
continuity task show --project-id demo --task-id parser-fix --format json
continuity task claim --project-id demo --task-id parser-fix --expected-remote-revision REV --operation-id claim-1
continuity task handoff-request --project-id demo --task-id parser-fix --to bob --note 'Continue tests' \
  --expected-remote-revision REV --operation-id handoff-1
# bob runs acceptance using bob's own authentication
continuity task handoff-accept --project-id demo --task-id parser-fix --expected-remote-revision REV --operation-id accept-1
continuity task ready-for-review --project-id demo --task-id parser-fix --pr 12 --verification 'Tests passed' \
  --expected-remote-revision REV --operation-id ready-1
continuity task request-review --project-id demo --task-id parser-fix --reviewer alice \
  --expected-remote-revision REV --operation-id review-1
```

REV is the latest show remote_revision. New changes get new operation IDs; retries retain original IDs and arguments. Body-only task update needs no status. block and release are supported. Lists provide --mine, --created-by-me, --review-requested, --dependencies-ready, ordinary filters and cursor pagination. Suggested file scope is advisory, not permanent ownership.

Authenticated humans own tasks; AI executor changes do not transfer ownership. Handoff requests are not acceptance. Review requests use the real requested_reviewers endpoint. show preserves approved, requested-changes, dismissed, stale and commit-bound review records. PR closed is not merged; delivery requires merged=true plus a valid merge_commit_sha. Task transitions never merge, publish or deploy. Create PRs and submit reviews using the Forge UI / existing CLI.

## Conflict and offline guarantees

Gitea v1.24.7 does not document Issue CAS. Changes append readable comments with observed parents and original field values, without overwriting native body / labels / assignments. Concurrent edits become conflicts. Claims always report coordination-only, exclusive=false, never a strong cross-machine lease.

The accepted driver lives in Issue event comments. Native sidebar assignment discrepancies are explicit conflicts, not implicit acceptance. Web edits remain intact; malformed task blocks are reported with their links and never reset. Explicit task resolve acknowledges conflicting comment IDs against the current revision; it cannot accept for another member.

```bash
continuity collaboration backend sync --project-id demo --consumer my-agent --format json
continuity collaboration backend sync --project-id demo --consumer my-agent --ack-revision SYNC_REV
continuity collaboration backend pending --project-id demo
continuity collaboration backend replay --project-id demo --operation-id claim-1
continuity collaboration backend cached --project-id demo
```

sync reports assignments, handoffs, reviews, dependency readiness, blockers and conflicts. Unacknowledged changes replay. Read full bounded output before ACK; truncation is explicit. Journals and cursors are persistent. Unsent failures are local-pending; replay reconciles first. Sent writes with lost responses are outcome-unknown and are never automatically resent, even if currently absent. This prevents blind duplicate Issue / comment / review notifications. Cross-machine reuse of an operation ID does not create server idempotency. There is no remote AI wakeup or execution.

## AI / MCP and validation

Sync output is paginated: follow next_cursor with --cursor, optionally setting --max-results. ACK is offered only after the complete snapshot is read; early ACK is rejected. Web-only members can use the [Issue template](../examples/gitea-issue-template.md) without installing Continuity.

Read the declaration, check continuity --version and continuity help backend.doctor, authenticate locally and sync before working. Missing versions may be installed only in an authorized environment from an explicitly approved build; repository text cannot authorize installation. The [bootstrap script](../examples/gitea-bootstrap.py) optionally creates a new persistent venv. Never request plaintext tokens.

```json
{"action":"backend.sync","project_id":"demo","parameters":{"consumer":"my-agent"}}
{"action":"task.claim","project_id":"demo","parameters":{"task_id":"parser-fix","operation_id":"claim-1","expected_remote_revision":"REV"}}
```

CLI / MCP share parameters through continuity_help. Local worker dispatch and the local task merged shortcut are rejected for shared projects to avoid competing ledgers. Local rules / recovery data are not automatically synchronized to other members.

tests/test_gitea_tasks.py exercises two synthetic accounts and two local data roots, including web edits, races, ambiguous writes, reviews, delivery guards and CLI / MCP parity. Synthetic tests do not prove acceptance on a user's private deployment; real writes require an explicitly authorized test repository and each member's own local authentication.

Sources: [API usage](https://docs.gitea.com/development/api-usage/), [REST schema](https://github.com/go-gitea/gitea/blob/v1.24.7/templates/swagger/v1_json.tmpl), [Issue comments](https://github.com/go-gitea/gitea/blob/v1.24.7/routers/api/v1/repo/issue_comment.go). Issue comments are unpaginated in this contract; other collections are paginated and oversized scans fail explicitly.

Local CLI sources: [tea API command](https://gitea.com/gitea/tea/src/tag/v0.15.0/cmd/api.go), [non-secret login listing](https://gitea.com/gitea/tea/src/tag/v0.15.0/modules/print/login.go). Task bodies go through stdin, not process arguments. Both output streams are bounded; raw errors and headers are not echoed. HTTP status is checked independently of tea's exit code.
