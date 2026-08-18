# M8-08 Forge Collaboration Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified offline forge-coordinated shadow adapter

```yaml
document_id: context.m8-08-forge-collaboration-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-08-forge-collaboration-v1
affected_tasks: [M2-07, M2-08, M8-02, M8-08, M10-03, M10-09]
next_review: M8-09
```

## Scope

M8-08 defines a provider-neutral, read-only forge projection for GitHub,
Gitea and GitLab-shaped snapshots. Issues, pull requests, branches,
assignees, requested reviewers, reviews and CI statuses form candidate Work,
claim and evidence records. Git-visible ownership remains
`claim_uniqueness: not-guaranteed`; authority remains in the configured State
MCP profile.

The verified adapter executes entirely against fixed local snapshots. It does
not invoke a forge provider, update a remote ref, write Typed State or grant a
claim. A remote branch change is represented by an intent bound to the exact
observed remote OID. A later authorized dispatch adapter must perform the
remote compare-and-swap operation.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.forge-work-projection/v1alpha1` | normalized Issue/PR/branch/assignee/review/CI projection with a deterministic SHA-256 digest |
| `context.forge-ref-update-intent/v1alpha1` | projection-digest and expected-remote-OID-bound branch update intent with remote effect authority disabled |
| `context.forge-unpublished-work/v1alpha1` | local-only work disclosure with no unique-claim guarantee and an explicit publish-or-State-MCP resolution |
| `context.forge-collaboration-benchmark/v1alpha1` | mapping, replay, conflict, downgrade, authority and latency acceptance receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior, contract, benchmark and CLI tests | `17/17` pass |
| GitHub/Gitea visible Work/claim/evidence mapping | `2000/2000`; rate `1.0` |
| deterministic projection replay | `2000/2000`; rate `1.0` |
| stale remote-ref rejection | `2000/2000`; rate `1.0` |
| unpublished work explicit downgrade | `1000/1000`; rate `1.0` |
| generated ref-update intents | `2000` |
| authority escalations | `0` |
| provider invocations / external services | `0 / 0` |
| local benchmark latency | p50 `0.169632 ms`; p95 `0.351431 ms`; max `0.737969 ms` |
| receipt | [`m8-08-forge-collaboration-results.json`](../../experiments/evidence/m8-08-forge-collaboration-results.json), receipt SHA-256 `e9c0b2ee70ca05e257a65aeccf4b411767aa6e0d21fc103fea0785b2d7e918c6`, file SHA-256 `e37ef56853898826be2d10af5c98caf10c2768856b3d0fe689f943ec0c23f08e` |

The benchmark runs `1000` iterations over one fixed structural fixture for
each of the GitHub and Gitea adapters. The counts measure repeated projection,
replay and conflict behavior; they do not represent `2000` distinct payload
shapes. Separate focused tests cover GitLab equal-IID isolation, self-hosted
instance identity, subgroup paths, strict-schema bounds and forged receipt
rejection. Multiple pull requests explicitly linked to one Issue are rejected
as ambiguous. Live provider behavior is outside this offline completion gate.

## Safety boundaries

Projection replay regenerates normalized output and rejects a mismatched
`projection_sha256`. Remote ref intent creation verifies and carries that
digest, then rejects a stale expected OID.
Offline and unpublished work remains local-only until it is published or
admitted through State MCP. Forge assignment and branch visibility do not
provide an exclusive claim, lease clock, completion authority or effect
authority.

This evidence proves deterministic local projection and explicit degradation.
It does not prove remote API compatibility, webhook delivery, cross-device
availability or production remote mutation. Those capabilities require live
M10 fixtures and an authorized adapter.

## Reproduction

```text
.venv/bin/python tools/run_forge_collaboration_benchmark.py --samples 1000 --generated-at 2026-08-17T08:00:00+08:00
.venv/bin/python -m unittest tests.test_m8_08_forge_collaboration tests.test_m8_08_contract_schemas tests.test_m8_08_benchmark -q
```
