# M7-05 Affected-Test Selection Acceptance

Version: 2  
Date: 2026-08-16  
Status: verified trusted local contract, independent replay, and current-repository command gate

```yaml
document_id: context.m7-05-affected-test-selection-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/m7-05-affected-test-selection-v2
affected_tasks: [M7-03, M7-05, M10-00]
next_review: M10-00
```

## Scope

M7-05 defines provider-neutral change-set, deterministic derivation, affected
graph, required-test inventory, selection receipt, golden matrix, and benchmark
contracts. The current Verification Profile defines required gates. A current,
content-addressed test inventory defines the full-validation boundary. The
affected graph may only reduce that inventory; it cannot add completion
authority or weaken a required gate.

The trusted change-set resolver derives the complete changed-path set from the
authoritative base/head diff and binds the result to the current repository
revision. Graph and inventory derivation receipts bind source, adapter,
configuration, dynamic-edge evidence, profile and output digests. The trusted
context resolver verifies the current repository/profile and admitted adapter
anchors before selection. Receipt validation replays these inputs and compares
the complete output.

Changed paths map to graph nodes. Reverse dependency closure includes every
dependent node before selecting tests. Tests marked `always_run` remain in all
partial selections. Unknown paths, incomplete or stale graphs, unresolved
dynamic edges, repository or profile mismatch, and unavailable graph evidence
produce `full-validation`. Missing, stale, unbound or invalid required-test
inventory provenance rejects selection because a safe full scope cannot be
established.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.affected-graph/v1alpha1` | current repository graph, dependency edges, completeness, dynamic-edge status and source provenance |
| `context.required-test-inventory/v1alpha1` | current profile-bound full required-test boundary and per-test resource estimates |
| `context.affected-change-set/v1alpha1` | trusted base/head diff, complete changed paths and current repository binding |
| `context.deterministic-derivation-receipt/v1alpha1` | source, adapter, configuration, dynamic-edge evidence and output derivation binding |
| `context.affected-test-selection/v1alpha1` | selected or full-validation decision, fallback reasons, affected nodes, required gates and reduction estimates |
| `context.affected-test-selection-fixture/v1alpha1` | fixed profile, inventory, graph variants, provenance payloads and scenarios |
| `context.affected-test-selection-golden/v1alpha1` | independent expected scenario outcomes and current command catalog |
| `context.affected-test-selection-benchmark/v1alpha1` | correctness, replay, authority and measured local-workload receipt |

Every graph, inventory and selection receipt sets State write and completion
authority to `false`. Selection performs no external service calls. State MCP,
M7-02 claim-evidence admission and M7-03 Verification Profile receipts remain
required for an authoritative completion transition.

## Verification

| Gate | Result |
|---|---|
| focused behavior, trusted replay and schema | `21/21` passed |
| scenario replay | `1000/1000`; eight scenarios at `125` samples each |
| safe partial selection | `250/250`; missed tests `0`; unsafe partial selections `0` |
| fail-safe fallback | `750/750` full-validation; fallback mismatches `0` |
| deterministic replay | mismatches `0`; receipt validator replays complete trusted inputs |
| synthetic micro workload | `50.20%` wall-time reduction; retained as a microbenchmark only |
| current repository commands | `3,313,235,065 ns -> 94,873,214 ns`; reduction `97.13%` |
| current selected commands | `m7-05-contract`, `python-compile`, `ruff-selection` |
| authority | State write and completion authority counts `0` |
| external services | `0` |
| receipt | [`m7-05-affected-test-selection-results.json`](../../experiments/evidence/m7-05-affected-test-selection-results.json), benchmark SHA-256 `45c352369ac6f020bf293a0638742baf8bdbc9f20c2af669a532fdb4cc68e39c` |

The implementation sequence observed red states for missing trusted contracts,
unsealed graph/inventory derivation, self-sealed receipt removal, forged
benchmark counts/timing, missing independent golden data, and schema/runtime
divergence. The final focused suite is green.

## Boundaries

The micro workload measures one local SHA-256 workload per test. It is a
deterministic comparison fixture and does not satisfy the repository wall-time
gate. The repository gate runs the command catalog from the independent golden
matrix under the current working tree and records raw `perf_counter_ns` samples,
exit codes, output digests, interpreter, platform and repository tree digest.
The selected command set is accepted only when every required command passes,
missed-test count is zero and the fresh command reduction is at least 30%.

The independent verifier reloads the golden matrix and fixture, replays all
scenario counts, recomputes selection and dependency closure, validates receipt
replay, recomputes micro workload outputs and checks current repository tree and
command catalog digests. A source that falsely declares a graph complete remains
an upstream evidence admission risk until its trusted adapter and source anchor
are admitted. The M7-05 schemas require registry admission before release
activation.

## Reproduction

```text
.venv/bin/python tools/generate_m7_05_acceptance.py --iterations 1000
.venv/bin/python -m unittest tests.test_m7_05_affected_test_selection tests.test_m7_05_affected_test_selection_benchmark tests.test_m7_05_trusted_selection tests.test_m7_05_trusted_benchmark tests.test_m7_05_contract_schemas -q
ruff check context_control_plane/affected_test_selection.py context_control_plane/affected_test_selection_benchmark.py tests/test_m7_05_affected_test_selection.py tests/test_m7_05_affected_test_selection_benchmark.py tests/test_m7_05_trusted_selection.py tests/test_m7_05_trusted_benchmark.py tests/test_m7_05_contract_schemas.py tools/generate_m7_05_acceptance.py
```
