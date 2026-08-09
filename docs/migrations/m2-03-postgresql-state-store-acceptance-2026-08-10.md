# M2-03 PostgreSQL State Store Acceptance

版本：1  
日期：2026-08-10  
状态：accepted optional shared backend

```yaml
document_id: context.m2-03-postgresql-state-store-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-gitea-runs-and-shadow-replay-2026-08-10
supersedes: null
affected_tasks: [M2-03, M2-05, M2-08, M2-09, M8-02]
next_review: M2-08
```

## Scope

M2-03 maps the M2-01 typed snapshot and M2-02 Event contract to an optional shared PostgreSQL backend. A project row stores revision, snapshot, snapshot hash, Event sequence and Event head. Each commit locks the project row, checks expected revision and Event head, replays the proposed Event, appends the Event, and updates the snapshot in one transaction. Event UPDATE and DELETE are rejected by database triggers.

PostgreSQL is registered as the `shared-strong` candidate backend. The default product profile remains `local-embedded`; a user installing the core product does not need PostgreSQL, Docker, Docmost, Temporal, OTel, or a remote database.

## Verification

| Gate | Result |
|---|---|
| M2-03 targeted tests | 11/11 passed |
| Concurrent same-revision writers | 8/8 produced one commit and one explicit conflict |
| Silent overwrite | 0 |
| Snapshot/Event/revision transaction | rollback, replay mismatch and duplicate identity tests passed |
| Append-only database enforcement | Event UPDATE/DELETE rejected with SQLSTATE `55000` |
| Migration | down/up reversible |
| Local full repository | 168/168 tests, verifier and compile passed after revision 20 projection update |
| Gitea branch CI | run 1039 repository-verification and secret-scan passed |
| Shadow self-dogfood | revision 18→22, 4 Events, database read equals deterministic replay |

Runs 1037 and 1038 remain failure evidence. Run 1037 showed that the configured Gitea runner did not provision a declarative service. Run 1038 started a healthy container but connected from the job container to its own localhost. Run 1039 joined PostgreSQL to the job container network and passed both required jobs. The regression test now requires explicit lifecycle, pinned image digest, job-network attachment, container DNS, health check, and unconditional cleanup.

## Performance And Footprint

Forty localhost samples measured commit p50/p95 `19.5525/22.0199 ms` and read p50/p95 `9.8665/12.7360 ms`. The empty integration container measured `0.00%` idle CPU and `32.39 MiB` memory. The OCI image reports `119,995,681` bytes; the test database measured `8,369,855` bytes and the `context_control` relations measured `196,608` bytes.

The operation timing includes a new connection, Python validation, Event replay, row lock, JSONB work and transaction commit. It is a shared-backend baseline. M2-09 will compare SQLite using the same fixture and operation boundary before the default backend is accepted.

## Shadow Receipt

[`m2-03-shadow-dogfood-receipt.yaml`](../../experiments/state/m2-03-shadow-dogfood-receipt.yaml) records a shadow-only project with active Work M2-03. Four Events preserve CI 1037 failure, CI 1038 failure, CI 1039 success and MASTER revision 19 local-first governance. The final snapshot hash is `d033e065c7f3d2f8c5e86893c452f57a4a720afe8544ab9a8da2c02d43dd0515`; the Event head is `dfae94b0352777ca46558f86834b8f5669ebf1e6a10d98816a85c5d35711dfc6`. Database read and replay output were equal, and final open blockers were empty.

The receipt grants no production authority. State MCP authorization, production claim/effect gates, embedded SQLite, forge collaboration, backup and tenant isolation remain in M2-05, M2-09, M8-08, M10-04 and M8-05.

## Completion

The M2-03 completion gate is satisfied for the optional PostgreSQL backend. The canonical queue advances to M2-08 StateStore SPI so local and shared adapters declare capabilities and pass one conformance suite. M2-09 then implements the zero-service SQLite default.
