# Current capabilities, measurements and known limits

[中文](validation.md) · [Workflows](workflows.en.md) · [Historical experiments](benchmarks.en.md)

Scope: Alpha25 (`0.1.0a25`) and this documentation review. New synthetic CLI measurements, existing regressions, anonymous user feedback and uncovered behavior are distinct evidence classes.

## Reproduce the current workflow check

Use a **new, empty persistent directory**, never your real registry/profile:

```bash
python examples/verify_adopter_workflows.py --workspace /path/to/new-persistent-fixture --cli /path/to/continuity
```

The development tree uses `public/examples/`; a public checkout uses `examples/`. Artifacts are retained. The script creates a synthetic Git repository, isolated registry and Python workers: no model/API requests, native accounts/sessions, private projects or network calls.

[Sanitized receipt](../benchmarks/adopter-workflows.json) contains allowlisted booleans, version, bytes and exit codes only, with no hardware/host/profile identifiers, private paths, project names or raw output.

| Check | Result |
|---|---|
| Init/register/identity/assigned packet | Pass |
| Brief symbol lookup within 1024 bytes | Pass; 32 bytes for this fixture hit |
| Complete rules, mirror check, prompt body | Pass |
| Inbox delivery/read/ack | Pass |
| Two supervised Python workers and normal report | Pass |
| Failed child | Original exit 3 recorded; dispatch CLI nonzero (2) |
| Single-update merge metadata | Pass; no actual Git merge |
| Shared handoff, lesson, second-controller read-only | Pass |
| Event paging, OR wait, quiet read | Pass; 196 bytes for this fixture's no-change response |
| Worker updates card early | Gap reproduced: report file persists, but explicit attachment is required before report reads it |

Thirteen positive checks pass; one known boundary is reported separately. Fixture bytes are not production throughput, model-token reduction or billing savings.

## Capability/evidence matrix

| Capability | Current evidence | Limits |
|---|---|---|
| Cards, evidence fields, body-only updates | CLI check + regressions | Metadata is not actual test execution |
| Open-state card-backed Todo | Regressions + synthetic path | A second independent queue is optional |
| Managed workers/reports | Normal-path check, anonymous adoption feedback | Early card changes can skip report attachment |
| Timeout, idle, effort, native resume | Adapter/supervisor regressions | No new native-model/provider parameter field run |
| Transient/policy failure and fallback | Classification/reservation/late-result regressions | No automatic channel switch after policy refusal or account quota prediction |
| Changes/pagination/multi-event wait | CLI + concurrency/budget regressions | Not universal host wake-up; timeout is not worker failure |
| Rules/audience/attachments/export | CLI + version/hash regressions | Delivery does not prove the worker read a policy |
| References/lessons | Record/idempotence/correction regressions; lesson CLI | No dedicated source TSV round trip; reminders do not prove adoption |
| Index/brief/dependents/prompt code routes | Synthetic symbol + language/route regressions | New fixture uses Python; not a complete compiler graph |
| Batches/decisions/metrics/merge metadata | Schema/source/fence regressions and user feedback | Does not execute Git or invent approval |
| Controller leases/capacity/unknown operations | Second-controller CLI + concurrency regressions | Cooperating tool-mediated clients only |
| Handoffs/integration refs/session binding | CLI + regressions | No retroactive full-history allocation or permanent connection identity |
| Codex/Claude compaction | Controlled callbacks/dedup regressions; historical native manual record | No new natural/automatic-threshold/long-context model experiment |
| Native Claude child observation | Official schema and controlled callback regressions | Natural full lifecycle unproven; no inferred success/exit code |
| SSH CLI/hook/MCP | Existing installation/handshake evidence and isolation regressions | New synthetic script is not a multi-machine field run; retrieval root mapping is manual |
| Tokens/cache/injection/task deltas | In-place import/counter regressions | No real-account collection in this check; no universal billing/causal savings result |
| Drift/commitments | Parser/rule regressions and anonymous feedback | Not proof of all external/document facts |

The [Alpha25 public release CI](https://github.com/skyhua0224/continuity-plane/actions/runs/38845950775) passed 268 tests. New documentation checks run on current main separately; neither released wheels nor historical counts are rewritten.

## Known adoption gaps

1. Early card changes can separate report files from attachment. Temporarily attach the run receipt's report_file with `task update --report-file`, then use report.
2. A new MCP connection can cause same-executor ownership friction; repeated force is not the intended finished design.
3. Source/checklist-budget notices are not always complete, immediately executable commands. Tool usage itself is not the goal.
4. Dedicated `source-index.tsv` import/export is missing. An existing repository source index can remain canonical.
5. Remote MCP accepts explicit remote roots; hook mappings do not automatically translate retrieval roots or authorize arbitrary process administration.

## Privacy and effect claims

Publish allowlisted aggregates, not transcripts, credentials, device IDs, private project data or profile directories. Native usage is read on its owning host/profile, not moved elsewhere for querying.

Prompt-cache hits are not proof of Continuity-caused savings. Retain negative net-byte estimates. Longitudinal per-task cost, window utilization and work between compactions need comparable real sessions, same-origin counters and quality gates; unknowns remain unknown.
