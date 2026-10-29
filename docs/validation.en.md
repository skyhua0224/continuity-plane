# Current capabilities, measurements and known limits

[中文](validation.md) · [Workflows](workflows.en.md) · [Historical experiments](benchmarks.en.md)

Scope: Alpha36 (`0.1.0a36`). New synthetic CLI measurements, existing regressions, anonymous user feedback and uncovered behavior are distinct evidence classes.

## Reproduce the current workflow check

Alpha27 established an isolated HTTPS Gitea 1.24.7 / local tea 0.15.0 two-synthetic-account private repository check. Alpha36 adds no new external Gitea field run; collaboration changes use deterministic regressions. The adopter script below was rerun from a fresh persistent venv containing only the Alpha36 wheel and a new workspace. Its receipt contains allowlisted synthetic aggregates only; tag CI remains authoritative for the release build.

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
| Inbox delivery/read/ack/archive/duplicate suppression | Pass; only acknowledged messages move |
| Two supervised Python workers and normal report | Pass |
| Failed child | Original exit 3 recorded; dispatch CLI nonzero (2) |
| Single-update merge metadata | Pass; no actual Git merge |
| Shared handoff, lesson, second-controller read-only | Pass |
| Scoped profile, approved memory and controller start | Pass; 1,443 JSON bytes in this fixture within a 4,096-byte budget |
| Event paging, OR wait, quiet read | Pass; 216 bytes for this fixture's no-change response |
| Worker updates card early | Automatically attached and readable through report; manual attachment does not satisfy this check |

Fourteen positive checks pass; automatic report delivery after early card updates is checked separately. Fixture bytes are not production throughput, model-token reduction or billing savings.

## Capability/evidence matrix

| Capability | Current evidence | Limits |
|---|---|---|
| Cards, evidence fields, body-only updates | CLI check + regressions | Metadata is not actual test execution |
| Open-state card-backed Todo | Regressions + synthetic path | A second independent queue is optional |
| Managed workers/reports | Normal path, early-update/replay regressions, anonymous feedback | Stale attempts and reassigned executors remain guarded |
| Timeout, idle, effort, native resume | Adapter/supervisor regressions | No new native-model/provider parameter field run |
| Transient/policy failure and fallback | Classification/reservation/late-result regressions | No automatic channel switch after policy refusal or account quota prediction |
| Changes/pagination/multi-event wait | CLI + concurrency/budget regressions | Not universal host wake-up; timeout is not worker failure |
| Rules/audience/attachments/export | CLI + version/hash regressions | Delivery does not prove the worker read a policy |
| References/lessons | Record/TSV roundtrip/conflict/correction regressions; lesson CLI | Explicit interchange; reminders do not prove adoption |
| Controller profile/memory/native notes | Scope, locked fields, candidate, revocation, origin/symlink/permission gates and bounded start regressions | Native writeback and full real-memory format coverage remain unmeasured; same-OS agents need sandboxing |
| Inbox archive, quota pause, reservation diagnosis | Synthetic end-to-end and deterministic regressions | Consumes observed errors only; no provider quota polling |
| Worker queue, handoff priority, assigned/running, batch and integration_base | Single-instance/order/duplicate/capacity/host-pause/transient-backoff, bounded handoff, state projection, JSON batch and prompt regressions | No cross-machine strong lock, process sandbox or automatic wake-up |
| Inbox generated IDs, bulk acknowledgement and structured MCP errors | CLI/MCP generated ID, explicit-ID idempotence, returned-ID ack, before/all batching, archive and schema/argparse conditional-field regressions | ACK is not host wake-up; archive does not delete; one call covers at most 1000 messages |
| Host-restart reservations, local dependencies, list envelope and cooldown reasons | boot/host stale detection, stale capacity exclusion, reconcile, dependency gating, paged envelope, non-code decision, health reason and large-history append regressions | Capacity is project+host+profile+client-family; MCP lifecycle remains host-owned |
| Index/brief/dependents/prompt code routes | Synthetic symbol + language/route regressions | New fixture uses Python; not a complete compiler graph |
| Batches/decisions/metrics/merge metadata | Schema/source/fence regressions and user feedback | Does not execute Git or invent approval |
| Controller leases/capacity/unknown operations | Second-controller CLI + concurrency regressions | Cooperating tool-mediated clients only |
| Handoffs/integration refs/session binding | CLI + regressions | No retroactive full-history allocation or permanent connection identity |
| Codex/Claude compaction | Controlled callbacks/dedup regressions; historical native manual record | No new natural/automatic-threshold/long-context model experiment |
| Native Claude child observation | Official schema and controlled callback regressions | Natural full lifecycle unproven; no inferred success/exit code |
| SSH CLI/hook/MCP | Existing handshakes plus isolation/mapped protocol regressions | No new physical two-machine trial; explicitly select bridge_config |
| Tokens/cache/injection/task deltas | In-place import/counter regressions | No real-account collection in this check; no universal billing/causal savings result |
| Drift/commitments | Parser/rule regressions and anonymous feedback | Not proof of all external/document facts |

The [Alpha25 public release CI](https://github.com/skyhua0224/continuity-plane/actions/runs/38845950775) passed 268 tests. New documentation checks run on current main separately; neither released wheels nor historical counts are rewritten.

## Alpha36 checks

- Real agent sessions: Claude Code (Sonnet 5.5) ran coordinator, takeover and code-lookup scenarios in an isolated data root, 3 runs per scenario and version, with prompts that never mention Continuity; `tools/run_agent_adoption_eval.py` counted MCP/CLI calls, tokens and cost. The Codex worker scenario was not completed because the Codex quota ran out during the eval.
- Large-project reads, code lookup, resume after compaction, worker waiting and MCP proxy reconnects: methods and numbers are in the [release notes](release-alpha36.md#-实测--measurements).
- Regressions: `tests/test_agent_tools.py`, `test_agent_steering.py`, `test_adoption_telemetry.py` and `tests/test_a36_*.py`; the full source suite has 2,764 tests, with only the M7-05 receipt and M8-10 latency gate failing, as on Alpha35.

## Alpha35 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| Missed worker events and monitoring windows | worker events --follow emits an appended event as NDJSON from its cursor; all-project cursor parsing is covered | A local CLI stream is not a remote wake-up service; --all-projects uses project:cursor pairs |
| Inbox completion notices | inbox wait consumes the persistent message event; inbox unread --count agrees with the same acknowledgement fact | Recipient acknowledgement remains explicit; another host's AI is not automatically woken |
| Job terminal state and observed checkout | job.json/index running to completed, exit_code 0, actual cwd/HEAD, finish cursor and nonblocking metadata failure | Legacy jobs are not backfilled with invented facts; native children without heartbeat remain observations |
| Dispatch hot path and output | No running binding forbids history scanning; a binding reads from its start cursor; build-lock idle is diagnostic; task show defaults to 64 KiB | Oversized single records still fail explicitly; lock waits are not killed |
| Capacity, quota and fallback | Cross-project host gate, prior-boot exclusion, expired cooldown clearance and transient-only fallback with reassignment | Host capacity is operator policy; auth/policy failures do not fallback; no account-quota query |
| Card semantics and safety | queued preserves assignee, explicit unassign, controller-lease reason, superseded dependency, PATH Git guard and async completion hook | The Git wrapper is not a sandbox and absolute paths bypass it; the hook is not a remote wake-up |

Focused regressions are in `tests/test_alpha35_control_observability.py`, alongside existing supervisor, dispatch, project-context, controller-recovery and adoption suites.

## Alpha34 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| HTTP interception vs. content refusal | 401/403, Cloudflare, cf-ray, proxy and explicit `cyber_policy` precedence; legacy correction | Explicit model-safety markers still block blind replay; clearance requires exact job and reason |
| Automatic and reviewed resume | Same-thread dry-run resume after 403; exact-job clearance, idempotent event and result correction | `operator-cleared` is a local audit decision, not provider authentication or a new content-safety approval |

## Alpha33 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| Dispatch hot path | Incremental safety index, corruption rebuild, tail-only reads and first/subsequent timings on large local projects | First upgrade still performs one sequential backfill; the change log remains authoritative |
| Input and output bounds | Empty bodies fail before policy history; oversized `batch-list` budgets cap and return a cursor | A single record larger than 131072 bytes still fails explicitly rather than disappearing |
| Token diagnostics | Expiry, owner, missing token and wrong-project token have distinct messages | Controller tokens are cooperative fencing, not native account authentication |

## Alpha32 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| Host-restart reservations | host/boot fingerprints, legacy boot-time inference, reservation inventory, dry-run/apply reconcile, clear outcomes and stale capacity exclusion | Only provably stale records are cleared; unknown/live remain |
| Dependencies and list shape | local/shared dependencies, dependencies-ready, claim/active/dispatch gates and one paged envelope | Dependencies are scheduling hints and gates, not module ownership |
| Decisions, health and performance | non-code decisions without commit, dispatch-health reason/task/attempt, and no full-history scan for events without event IDs | Other project writers can still delay the project lock; no universal concurrency guarantee |
| MCP host restart | collaboration doctor explains host ownership and CLI/SSH fallback | Continuity does not auto-spawn a host stdio process |

## Alpha31 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| MCP send without message_id | CLI/MCP generate and return an ID; explicit-ID resend is deduplicated; the returned ID can be acknowledged | Generated IDs do not make retries idempotent; pass an explicit ID when that is required |
| Raw MCP exceptions | Missing inbox sender and invalid planning parameters return isError/structuredContent/failed_gate | JSON-RPC errors remain for unknown tools or malformed envelopes |
| Large unread inbox | Bounded acknowledgement by time or all, followed by archive of acknowledged messages | The human still chooses the acknowledgement window; historical messages are not auto-acknowledged |
| Root/project drift | Error includes requested root/project and both registered identities with a correction command | Local registration is not automatically merged with a remote authority |

## Alpha30 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| Durable dispatch queue | Single-instance lock, priority, duplicate suppression, host pause, capacity gate, quota/rate-limit backoff and state tests | Lock covers one project/data root; not a remote queue or provider SLA |
| Handoff truncation | Pending decision, blocked card and failed attempt retained at a 1 KiB budget | Ordinary change history may be omitted; request a larger budget for full detail |
| Todo state | Assigned and live running binding are distinct; blocked reasons are retained | A native child without heartbeat remains an observation, not proof of a controlled run |
| Usage limit | Quota outranks generic policy wording; automatic quota recording, pause and legacy-job correction tests | Uses provider reset evidence only; unknown reset is never invented |
| Shell-safe task batch | File/stdin JSON, stop-on-first-error and local/shared field routing tests | Not an all-or-none transaction across Issues |
| Reservation and integration base | Live/stale/unknown diagnosis and non-authority integration_base prompt regression | Diagnosis does not terminate or take over a process; base grants no merge authority |

## Alpha29 adoption regressions

| Fix | Current evidence | Boundary |
|---|---|---|
| Reports after early updates | Review/completed/merged/blocked, stale-attempt and replay tests; automatic CLI report read | Reassigned executors and stale attempts cannot overwrite the new attachment |
| Stable executor | Two fresh MCP processes; Todo activation with distinct connection IDs | Configure executor_id/CONTINUITY_ASSIGNEE; no silent takeover of old identities |
| Actionable guidance | Real-parser command checks, dedup and no second card-workflow queue | Unknown write parameters are not invented; does not prove adoption |
| Reference TSV | Unicode, extra columns, idempotence, atomic conflicts/invalid rows, MCP interface | Explicit interchange, no overwrite/automatic sync or proof of reading |
| Mapped lookup | Separate synthetic repository through the receiving CLI protocol; unmapped/over-budget rejection | No new physical two-machine trial or arbitrary remote file/process administration |

[test_a26_adoption.py](../tests/test_a26_adoption.py) contains thirteen Alpha26 synthetic regressions. Alpha35 observability tests live in `tests/test_alpha35_control_observability.py`; Alpha34 classification and clearance tests remain in `tests/test_alpha34_policy_classification.py`. See [Alpha35](release-alpha35.md) for this release's limits.

## Privacy and effect claims

Publish allowlisted aggregates, not transcripts, credentials, device IDs, private project data or profile directories. Native usage is read on its owning host/profile, not moved elsewhere for querying.

Prompt-cache hits are not proof of Continuity-caused savings. Retain negative net-byte estimates. Longitudinal per-task cost, window utilization and work between compactions need comparable real sessions, same-origin counters and quality gates; unknowns remain unknown.
