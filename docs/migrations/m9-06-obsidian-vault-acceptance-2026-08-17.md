# M9-06 Read-Only Obsidian Vault Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified read-only projection contract

```yaml
document_id: context.m9-06-obsidian-vault-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-06-obsidian-vault-v1
affected_tasks: [M9-01, M9-02, M9-03, M9-04, M9-05, M9-06, M9-07]
next_review: M9-07
```

## Scope

M9-06 exports one authenticated, same-State-revision human-readable vault for
Obsidian. The export contains four deterministic Markdown files:

| Path | Source content |
|---|---|
| `00 Context Control Plane.md` | project identity, State revision, signed source digests and vault authority |
| `10 Project Graph.md` | Project Graph, active Work set, Work Ledger and graph health |
| `20 Decisions and Evidence.md` | Decision Timeline, Constraint Matrix, Evidence Matrix and projection health |
| `30 Context Health.md` | Context, Reference, Harness and Replay health plus bounded drilldowns |

The exported manifest records template version, project ID, State revision and
digest, source projection digests, every Markdown byte size and SHA-256, vault
SHA-256, and an HMAC signature. It has zero State-write, completion, approval,
provider-native and external-effect authority.

## Source And File Bindings

The exporter accepts only the M9-02 Project Graph, M9-03 Decision/Evidence and
M9-04 Context Health projection schema versions. Every source must have its
expected zero-authority shape, canonical projection digest and HMAC signature.
All three sources must agree on project ID, State revision and State SHA-256.
M9-04 additionally binds its source projection digest to M9-02 and its
Decision/Evidence projection digest to M9-03.

The writer only creates an absent or empty destination directory. Validation
requires exactly the four generated Markdown files and
`.context-control-plane.json`; extra Markdown, a changed generated file, a
modified manifest, a recomputed public file/vault digest without its HMAC
signature, or a non-empty destination all fail closed. Each generated file is
limited to `4 MiB` before manifest creation and at validation.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.obsidian-vault/v1alpha1` | signed manifest for four bounded, read-only Markdown projections |
| `context.obsidian-vault-benchmark/v1alpha1` | local build/write/validate, integrity and latency receipt |

Both contracts use strict JSON Schema and exact registry hashes. The vault
schema rejects unknown fields, unmanaged file paths, authority escalation and
invalid identifier, timestamp, hash, signature or byte-count forms.

## Verification

| Gate | Result |
|---|---|
| M9-06 behavior, schema, benchmark and runner tests | `17/17` pass |
| signed same-revision build/write/validate | `1000/1000`; rate `1.0` |
| source revision-binding rejection | `1000/1000`; rate `1.0` |
| generated Markdown tamper rejection | `1000/1000`; rate `1.0` |
| resealed manifest tamper rejection | `1000/1000`; rate `1.0` |
| unmanaged Markdown rejection | `1000/1000`; rate `1.0` |
| non-empty destination overwrite rejection | `1000/1000`; rate `1.0` |
| authority violations / provider invocations / external services | `0 / 0 / 0` |
| local build/write/validate latency | 1,000 samples; p50 `0.493540 ms`; p95 `0.759800 ms`; max `1.059245 ms`; threshold p95 `<50 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| receipt | [`m9-06-obsidian-vault-results.json`](../../experiments/evidence/m9-06-obsidian-vault-results.json), receipt SHA-256 `a77a6b2eb1d426dd027bf99c7f6863d1f54e3ed1bc02759bcd58f0cf1e9dd563`, file SHA-256 `76b6897f052a9492278a76d0a3f93e286e0740c76e7c321fd888b5baa4b2c0d3` |

The measured path uses local in-process signed fixture projections and local
filesystem I/O. It does not measure Obsidian indexing or rendering, Docmost,
browser transport, PostgreSQL, network latency, provider execution, raw
transcript export, a shared deployment or human governance actions.

## Authority Boundary

Obsidian is a generated read-only vault. It cannot submit active-task, decision,
claim, completion, promotion, correction or external-effect changes. Human
approval and correction continue through the controlled M9-05 governance facade
and State MCP authorization, expected revision/CAS and validator gates. A local
Markdown edit cannot become a State change or bypass those controls.

## Reproduction

```text
.venv/bin/python tools/run_obsidian_vault_benchmark.py --root . --iterations 1000 --generated-at 2026-08-17T23:59:00+08:00 --output experiments/evidence/m9-06-obsidian-vault-results.json
.venv/bin/python -m unittest tests.test_m9_06_obsidian_vault tests.test_m9_06_obsidian_vault_benchmark tests.test_m9_06_contract_schemas -v
ruff check context_control_plane/obsidian_vault.py context_control_plane/obsidian_vault_benchmark.py tests/test_m9_06_obsidian_vault.py tests/test_m9_06_obsidian_vault_benchmark.py tests/test_m9_06_contract_schemas.py tools/run_obsidian_vault_benchmark.py
```
