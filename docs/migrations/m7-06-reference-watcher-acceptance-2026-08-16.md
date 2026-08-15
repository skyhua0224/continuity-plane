# M7-06 ReferenceWatcher Acceptance

Version: 2  
Date: 2026-08-16  
Status: verified local-embedded offline watcher

```yaml
document_id: context.m7-06-reference-watcher-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/m7-06-reference-watcher-v1
affected_tasks: [M7-01, M7-02, M7-06]
next_review: M8-07
```

## Scope

M7-06 binds each watched source to a canonical `watch_sha256`, stable source
identity, authority kind, watch revision, baseline revision, baseline content
hash, validity deadline, trusted-time evidence and affected assertion IDs.
Trusted-time observations also persist verifier ID, version, trust-anchor digest
and registry-entry digest; live observations resolve that entry and verify its
digest before invoking the attestation callable.
Observation validation requires the expected watch or a trusted watch resolver.
Current and unavailable evidence resolve through content-addressed artifacts.
The watcher performs no network calls and does not read an implicit system
clock.

The observation contract derives `unchanged`, `changed`, `unavailable` and
`expired`. The decision contract retains current assertions only for unchanged,
current observations. Revision or hash changes mark every affected assertion
stale. Unavailable or expired evidence quarantines every affected assertion.
Completion admission remains empty until a new bearing assertion resolves its
current source bytes and retrieval receipt, binds the current source identity,
revision and hash, postdates the watch observation and explicitly supersedes
every affected assertion. Decision validation requires the expected observation
or a trusted observation resolver. A self-consistent receipt digest does not
provide a trust anchor.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.reference-watch-observation/v1alpha1` | canonical watch binding, source identity, baseline/current revision and hash, availability, freshness, verified trusted time and resolved current evidence |
| `context.reference-watch-decision/v1alpha1` | expected-observation binding, exact current/stale/quarantined matrix, verified supersedes edge and completion-eligible assertion IDs |
| `context.reference-watcher-benchmark/v1alpha1` | offline outcome coverage, completion veto, five-family adversarial binding coverage, current re-verification and deterministic replay receipt |

Observation and decision receipts set State write, completion and provider-native
authority to `false`. State MCP remains responsible for revision/CAS validation
and any authoritative assertion or task-state transition.

## Verification

| Gate | Result |
|---|---|
| focused behavior | watcher and benchmark `22/22` pass |
| replay | `1000/1000`; mismatch `0`; outcomes digest `061a1bf3bdd2f83244510b986606b9f1b21b2806d1e3c25c0dbeabfd5e61a9f1` |
| unchanged | `200/200` retained as current |
| upstream change | revision-only and hash-only fixtures `400/400` stale/quarantine |
| unavailable and expired | `400/400` quarantined |
| completion veto | unreviewed unsafe assertion allows `0/800` |
| current re-verification | replacement assertion releases `400/400`; superseded assertion releases `0` |
| adversarial binding | detached decision `400/400` rejected; detached observation `400/400`; forged state matrix `400/400`; unresolved replacement `400/400`; unverified trusted time `1000/1000`; total `2600/2600`, allows `0` |
| authority | violations `0` |
| external services | `0` |
| receipt | [`m7-06-reference-watcher-results.json`](../../experiments/evidence/m7-06-reference-watcher-results.json), internal receipt SHA-256 `e1d31c887bd4715c708702dc1cd99a9dc0dfda007d35fb62dca233be3769ead8`, file SHA-256 `55f2a6436d172935102509978abd276848975ef54b62d30f107fe5b0cf71aa85` |

## Contract Digests

| Contract | SHA-256 |
|---|---|
| observation schema | `bf80e5d606223a40e116923046384f6919838b38d403494ceb0c094928e3d1d1` |
| decision schema | `19c4dc9a9da62f1a0ca2bf543bf0836ae0ea3349dfc5067cc11e0f3458ea9478` |
| benchmark schema | `a569f97c13b8248b31e894150fae94529b55bf10df2a0f48a7891fb82126c310` |
| watcher implementation | `b1c07f23e60cbc386aea34ccbe3c399ca552cb9d40f539a1830f31d281b1db67` |
| benchmark implementation | `d3fa0de4959a21cc0760099e86b2710a4508c1f079ec56ffa86a7fb0c05c1a0a` |

## Boundaries

The verified adapter accepts retrieved bytes through trusted evidence and
artifact resolvers. Live HTTP, Git forge, standards registry and attestation
transport adapters remain outside this acceptance. Live mode rejects fixture
clocks and requires a registry verifier resolver that binds verifier ID,
version, trust anchor, registry-entry digest, kind, timestamp and evidence
bytes before attestation verification. Missing watch, observation, artifact, evidence or attestation
resolvers fail closed. Provider adapters must validate transport identity and
preserve the resulting evidence artifacts.

ReferenceWatcher admission is a prerequisite for watched external assertions;
it does not replace M7-01 provenance validation or the M7-02 claim-evidence
policy. The schemas require registry admission before release activation.

## Reproduction

```text
.venv/bin/python tools/generate_m7_06_acceptance.py --samples 1000
.venv/bin/python -m unittest tests.test_m7_06_reference_watcher tests.test_m7_06_reference_watcher_benchmark -q
ruff check context_control_plane/reference_watcher.py context_control_plane/reference_watcher_benchmark.py tests/test_m7_06_reference_watcher.py tests/test_m7_06_reference_watcher_benchmark.py tools/generate_m7_06_acceptance.py
```
