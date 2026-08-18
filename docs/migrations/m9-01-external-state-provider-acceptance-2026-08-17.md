# M9-01 External State Provider Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified transport-neutral local shadow contract

```yaml
document_id: context.m9-01-external-state-provider-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-01-external-state-provider-v1
affected_tasks: [M2-05, M8-05, M9-01, M9-02, M9-03, M9-04, M9-05, M9-06, M9-07]
next_review: M9-02
```

## Scope

M9-01 defines a transport-neutral, read-only provider over the existing State
MCP `context.state.read` tool. Every successful result carries the exact State
revision, event head, registry digest, capability digest, full defensive
snapshot, canonical State digest, projection digest and HMAC-SHA256 signature.
Consumers validate the typed snapshot, revision binding, digests and trusted
provider signature before rendering a view.

The provider validates the external request before dispatch, then delegates
authorization and authoritative lookup to State MCP. Unauthorized requests
remain indistinguishable at the provider boundary and reach neither project nor
Event lookup. An `expected_revision` value pins a render request; a different
current revision returns `stale_view` without a projection. A null value reads
the latest coherent State MCP snapshot.

The implementation has no Docmost package, network, database or runtime
dependency. It is the provider contract that M9 views can host behind an MCP or
application transport. Direct access to Docmost storage is outside the
contract.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.external-state-request/v1alpha1` | bounded project read with an optional expected State revision |
| `context.external-state-projection/v1alpha1` | revision-bound, typed-state-validated and HMAC-authenticated snapshot with zero write and effect authority |
| `context.external-state-response/v1alpha1` | strict success/error envelope for the external read tool |
| `context.external-state-projection-benchmark/v1alpha1` | consistency, denial, source-integrity, authority and latency receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior, benchmark, runner and schema tests | `22/22` pass |
| same revision across projection/source/snapshot | `1000/1000`; rate `1.0` |
| State digest equality | `1000/1000`; rate `1.0` |
| HMAC-signed projection validation | `1000/1000`; rate `1.0` |
| stale-view rejection | `1000/1000`; rate `1.0` |
| unauthorized rejection | `1000/1000`; rate `1.0` |
| torn-source rejection | `1000/1000`; rate `1.0` |
| unauthorized State reads | `0` |
| State write, controlled-action, provider and external-effect authority violations | `0` |
| provider invocations / external services | `0 / 0` |
| projection latency | p50 `0.761533 ms`; p95 `1.908619 ms`; max `4.549670 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| receipt | [`m9-01-external-state-projection-results.json`](../../experiments/evidence/m9-01-external-state-projection-results.json), receipt SHA-256 `7c06f2c92a321fecdf0f89d0f0ff15e695ae95558203ed0cccd8e9f0ffea4fc3`, file SHA-256 `b0922487816db3a7dab0b4b463d869a0d77925a33b58c2d95f4339c4a0b9fc26` |

Focused fault coverage includes malformed and torn State MCP success responses,
malformed source error responses, longest valid provider/request identities,
oversized external requests, unknown write tools, authorization before lookup,
defensive-copy behavior, partial/unknown/invalid typed snapshots, wrong HMAC
keys, coordinated snapshot/digest tampering and revision/digest/authority
tampering after receipt digest recomputation. The benchmark validator
independently checks identity, exact numeric types, finite latency, timestamp
format, count bounds, rates, zero-tolerance fields, source hashes and
acceptance-gate derivation.

## Safety boundaries

The projection SHA-256 values bind canonical content. HMAC-SHA256 authenticates
the provider envelope to consumers holding the configured key. The host remains
responsible for a trusted State MCP instance, a trusted `RequestContext`,
authorization policy, signing-key distribution, process or transport isolation
and transport security. A remote deployment must preserve these boundaries.
The verified signer uses one active key; rotation and historical verification
key retention remain M10 production requirements.

The provider exposes no commit, claim, effect, approval or promotion tool.
`state_write_authority`, `controlled_action_authority`, `provider_authority` and
`external_effect_authority` remain zero. M9-05 controlled human actions must
call the corresponding State MCP tool with authorization, expected revision and
validator gates; a rendered projection never grants that authority.

No live Docmost extension, HTTP/SSE transport, browser rendering or multi-node
deployment is proven by this acceptance. M9-02 through M9-07 consume this
contract and retain their own completion gates. The Yundi339 Docmost snapshot
remains a candidate reference.

## Reproduction

```text
.venv/bin/python tools/run_external_state_projection_benchmark.py --root . --iterations 1000 --generated-at 2026-08-17T17:00:00+08:00
.venv/bin/python -m unittest tests.test_m9_01_external_state_provider tests.test_m9_01_benchmark tests.test_m9_01_contract_schemas -v
ruff check context_control_plane/external_state_provider.py context_control_plane/external_state_provider_benchmark.py tests/test_m9_01_external_state_provider.py tests/test_m9_01_benchmark.py tests/test_m9_01_contract_schemas.py tools/run_external_state_projection_benchmark.py
```
