# State Store Portability Assessment

版本：1  
日期：2026-08-10  
状态：architecture decision evidence / implementation planned

```yaml
document_id: context.state-store-portability-assessment
document_revision: 2
change_type: decision
authority_ref: explicit-user-local-first-and-forge-collaboration-requirement
supersedes: null
affected_tasks: [M2-03, M2-05, M2-08, M2-09, M8-08, M10-09]
next_review: M2-08
```

## Decision

The product default is `local-embedded`: SQLite and a local artifact store run inside the control-plane application with zero user-managed database services. `forge-coordinated` reuses an existing GitHub, Gitea, or GitLab remote for project-level Work, claim, branch, review, and CI projections. PostgreSQL remains an optional shared backend when an installation requires online multi-writer transactions, leases, tenant isolation, audit, or durable long workflows.

Capability profiles are runtime configurations rather than user editions. A single developer or a team may enable any profile according to the guarantees and operating cost it needs. Context Control Plane remains one cohesive product with one installation and lifecycle entry point; Docmost, Temporal, PostgreSQL, and OTel are opt-in capabilities within that product. Core recovery, checkpoint, Skill resolution, local retrieval, personal state, and generated project projections cannot depend on those capabilities. A Windows, macOS, or Linux user must be able to install the default profile without Docker, a database installer, administrator access, or network configuration.

## Capability Boundary

| Profile | Shared channel | Guaranteed behavior | Declared limitation |
|---|---|---|---|
| `local-embedded` | none | local transaction, revision/CAS, append-only event chain, checkpoint recovery | no cross-device visibility or unique claim |
| `forge-coordinated` | existing forge | conflict visibility for published Issue/PR/branch/assignee state; explicit remote ref expectation | offline and unpublished work can conflict after synchronization |
| `local-coordinator` | member-hosted State MCP | online shared claim/CAS with SQLite and no separate database service | coordinator availability bounds shared authority |
| `shared-strong` | State MCP service | PostgreSQL transaction/CAS, multi-writer lease, tenant and audit extensions | the selected operator owns deployment and operations |

Two disconnected collaborators have no communication channel from which either side can learn the other's unpublished work. The control plane records this as a capability limitation. It does not issue a unique claim or E8 strong-consistency receipt for that interval. Reconnection converts divergent work into an explicit conflict, review, or promotion flow.

## Current PostgreSQL Cost

The M2-03 empty local integration instance measured `0.00%` idle CPU, `32.39 MiB` memory, a `120 MB` OCI image, an `8,369,855` byte database, and `196,608` bytes of `context_control` relations. The current implementation opens a new connection per operation; 40 localhost samples measured read p50/p95 `9.8665/12.7360 ms` and commit p50/p95 `19.5525/22.0199 ms`.

These values are acceptable for the optional shared backend. They remain unnecessary installation, storage, process, upgrade, and troubleshooting costs for a local developer. M2-09 must compare SQLite using the same fixtures, operation boundaries, sample count, host, and validator path. Default adoption requires zero extra service processes, state-only restore p95 below 2 seconds, no safety-veto regression, and no PostgreSQL image or installer download.

## SQLite Basis

SQLite documents local application storage and application-specific server storage as supported patterns. Serializable behavior is implemented by serializing writes; WAL permits simultaneous readers and a writer with snapshot isolation. `BEGIN IMMEDIATE` acquires the write transaction before the read/validate/update sequence. M2-09 will use these properties for one local control-plane writer boundary and will fault-inject stale revision, busy timeout, process termination, WAL recovery, corrupted pages, event tampering, and deterministic export/import.

SQLite files remain on a local filesystem owned by the user profile. Network filesystem sharing, direct multi-device file access, and silent file synchronization are excluded. Local multi-Agent use goes through one State MCP process or an equivalent serialized adapter rather than direct uncoordinated file writes.

## Forge Collaboration Basis

GitHub and Gitea expose existing Issue and Pull Request collaboration records; assignees communicate responsibility, while labels, dependencies, branches, reviews, and CI provide additional observable state. A provider adapter can derive a shared Work projection without adding infrastructure or uploading personal state.

Git supports `--force-with-lease=<ref>:<expect>` to update a remote ref only when its current value matches an explicit expected value. Git also defines `--atomic` so all supported ref updates succeed or fail together. These mechanisms are candidate CAS primitives for a minimal coordination ref. M8-08 must test GitHub and Gitea behavior, branch protection, concurrent pushes, retries, deleted refs, force-push restrictions, API rate limits, offline divergence, and contributors who do not run the control plane before adopting the ref design. Forge Issue/PR state remains the primary compatibility path.

## Installation And Migration Plan

1. M2-08 defines a StateStore protocol and capability manifest independent of SQL dialect and transport.
2. M2-09 implements SQLite with the M2-01/M2-02 conformance suite and Windows/macOS/Linux fault matrix.
3. M2-05 exposes the selected local or shared adapter through one provider-neutral State MCP contract.
4. M8-08 maps GitHub/Gitea/GitLab project facts into a shared projection and labels every degraded guarantee.
5. M10-09 installs `local-embedded` by default, proposes forge integration when a remote is detected, and keeps PostgreSQL/Docmost as explicit opt-in extensions.
6. Migration exports canonical events and snapshot hashes, imports them into the target adapter, replays to byte-equivalent state, then performs a reversible cutover. Automatic silent promotion is forbidden.

## Sources

| Source | Retrieved | Snapshot SHA-256 | Use |
|---|---|---|---|
| [PostgreSQL Windows installers](https://www.postgresql.org/download/windows/) | 2026-08-10 | `dd6463eba7069b5834817f0eaf2747dabb3a47eeb80416b1adda3d8026f72d70` | native installer and binary archive deployment cost |
| [SQLite Appropriate Uses](https://www.sqlite.org/whentouse.html) | 2026-08-10 | `d0850096c26e9c888acaf2d6e2ac029e86f66f51a87461b77b94f1639d309b79` | local storage and application-specific server patterns |
| [SQLite Isolation](https://www.sqlite.org/isolation.html) | 2026-08-10 | `23395653f04984e4310b287ed548202d874e06ecd2cba17f32b2714e5b7ebef7` | serialized writes, WAL snapshot isolation, `BEGIN IMMEDIATE` |
| [Git push](https://git-scm.com/docs/git-push) | 2026-08-10 | `f70fb7767323c66d8314bf9f16760ee32b6fb1bc91cfc7f3ed18853be0125b08` | explicit `force-with-lease` expectation and atomic ref update |
| [Gitea Issues and Pull Requests](https://docs.gitea.com/usage/issues-prs) | 2026-08-10 | `99851599d205c3abb03101cd07c018e78ae217acae07cecadfdffdb9b28f45fd` | provider collaboration surface |
| [GitHub About issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/about-issues) | 2026-08-10 | `0cbe5f00ad909fb5d380ef3fdc584dab078e0eef32d7308a364eb1908d362011` | assignee, dependency, label, PR and project integration surface |
