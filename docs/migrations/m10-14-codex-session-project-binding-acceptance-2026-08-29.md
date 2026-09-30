# M10-14 Codex Session Project Binding Acceptance

Version: 1  
Date: 2026-08-29  
Status: verified local candidate

```yaml
document_id: context.m10-14-codex-session-project-binding-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://m10-14/session-project-binding/revision-1
supersedes: null
affected_tasks: [M8-02, M10-01, M10-11, M10-14]
next_review: M10-01
```

## Scope

The Codex integration previously inferred the MCP project root from the process
working directory before the Session performed an explicit `continuity_resume`.
The lifecycle command bridge also resolved each event from `cwd` independently.
A long-running Session whose working directory differed from its established
governance project could therefore receive another project's packet.

The current contract uses the first successful explicit resume as the Session
identity boundary. The MCP process starts unbound, accepts one project root, and
rejects later root changes before calling the CLI. `PostToolUse` records the
successful MCP resume under a hashed Codex `session_id`; subsequent lifecycle
events read that binding before considering `cwd`. The local binding is
digest-protected, profile-bound, written with mode `0600`, and has no State or
completion authority. Invalid binding data fails closed.

The host fields and MCP tool coverage follow the official OpenAI Hooks contract:
`session_id` and `cwd` are stable common inputs, while `PostToolUse` exposes the
canonical MCP tool name, tool arguments, and result.

## Verification

| Gate | Result |
|---|---:|
| focused MCP, lifecycle, and public builder tests | `48/48` |
| full repository suite | `1973` passed, `31` environment skips |
| Ruff | passed |
| first explicit root from unrelated process cwd | requested project packet returned |
| second project root in the same MCP process | rejected before CLI execution |
| later lifecycle event with unrelated cwd | original bound project retained |
| conflicting successful-looking resume result | blocked; original binding retained |
| invalid binding file | fail closed; cwd fallback calls `0` |
| binding file permissions | `0600` |
| direct SQLite changes | `0` |

The live acceptance used two existing project roots and read-only resume calls.
No raw Session identifier, absolute project path, packet body, or product State
was admitted to this document.
