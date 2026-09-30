# M7-03 Verification Profile Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local-embedded contract and decision benchmark

```yaml
document_id: context.m7-03-verification-profile-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m7-03-verification-profile-v1
affected_tasks: [M4-08, M7-03, M7-04, M7-05]
next_review: M7-04
```

## Scope

M7-03 defines provider-neutral Verification Profile, project adapter, run
receipt and decision contracts. Profiles classify gates as required,
conditional or optional and bind each gate to typed conditions, capabilities,
dependencies, evidence requirements and integer thresholds with explicit
units. Adapters use typed executable, argument, working-directory and
environment references; free-form shell strings are excluded.

Run receipts bind work and project revisions, repository revision, profile and
adapter digests, invocation digest, gate, time interval, content-addressed
evidence and measurements. Passed TDD gates require ordered red and green
evidence. Decisions reject future receipts, stale or untrusted observations,
cross-profile substitution, missing required evidence and incomplete gate
sets. Optional failures remain visible and non-blocking. Profiles, adapters,
receipts and decisions have no State write or completion authority.

## Verification

| Gate | Result |
|---|---|
| strict contracts | five `context.verification-*` v1alpha1 schemas registered with current hashes |
| project fixtures | AlkaidLab and `portable-python-library` profiles validate and participate in every benchmark iteration |
| focused behavior | continuity and Verification Profile tests `24/24` pass |
| replay | `1000/1000` deterministic decisions; four scenarios at `250` samples each |
| outcomes | expected blocked `250`; expected satisfied `750`; false allow `0`; false deny `0` |
| optional failure | `250` failed optional gate receipts observed; overall completion remained non-blocking |
| integrity | replay mismatch `0`; future receipt, unbound artifact, cross-profile receipt and incomplete decision admission `0` |
| authority | State write and completion authority `0` |
| external services | `0` |
| receipt | [`m7-03-verification-profile-results.json`](../../experiments/evidence/m7-03-verification-profile-results.json) replays byte-equivalent |

## Boundaries

The AlkaidLab profile is a committed contract fixture. This acceptance does
not claim that the AlkaidLab repositories, devices, weak-network paths or live
performance gates were executed. Production adapters must supply current
repository, host capability and artifact evidence. State MCP authorization,
revision/CAS and M7-02 claim-evidence validation remain required before a
verification decision can complete work.

## Reproduction

```text
.venv/bin/python tools/generate_m7_03_fixtures.py
.venv/bin/python tools/generate_m7_03_acceptance.py
.venv/bin/python -m unittest tests.test_m5_07_continuity_incident tests.test_m7_03_verification_profile tests.test_m7_03_verification_profile_benchmark -q
```
