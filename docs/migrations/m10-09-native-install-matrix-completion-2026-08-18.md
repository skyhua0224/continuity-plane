# M10-09 Native Install Matrix Completion

Version: 1  
Date: 2026-08-18  
Status: accepted three-platform local-embedded migration

```yaml
document_id: context.m10-09-native-install-matrix-completion
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m10-09-native-install-matrix-completion-v1
supersedes: context.m10-09-native-install-matrix-acceptance@1
affected_tasks: [M10-04, M10-05, M10-09]
next_review: M10-10
```

## Scope

M10-09 provides `continuity export`, `continuity import`, and
`continuity rollback` for the default local-embedded profile. Export uses the
SQLite online backup API, records every admitted member in a strict hash/size
manifest, and excludes derived packets and rollback recursion. Import rejects
unknown members, traversal paths, symlinks, oversized members, digest drift,
invalid profiles, invalid SQLite state, and Event-head mismatch before replacing
the target control directory.

Replacement uses a sibling staging directory. An existing control directory is
exported into `rollback/previous.zip` inside the incoming directory before the
atomic swap. Rollback passes through the same verification path and preserves
the replaced state as the next rollback target.

## Native Matrix

All probes used wheel SHA-256
`4bff1d3881a0a845b4c7475622faeaefc224cc4380463093628952943b60bc0f`.
Durations are wall-clock milliseconds measured by the native runner.

| Platform | Install | Verify | Export | Import | Rollback | Uninstall | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Linux x86_64 / Python 3.14.6 | `2153.8454` | `146.2499` | `54.5864` | `60.9553` | `62.5949` | `3.5177` | passed |
| macOS arm64 / Python 3.14.1 | `3201.7786` | `519.6293` | `134.3655` | `161.2509` | `148.5317` | `47.8237` | passed |
| Windows AMD64 / Python 3.12.10 | `4031.0` | `203.0` | `125.0` | `109.0` | `110.0` | `62.0` | passed |

Receipts:

- [`Linux`](../../experiments/state/m10-09-native-install-matrix-results.json)
- [`macOS`](../../experiments/state/m10-09-native-install-macos-results.json)
- [`Windows`](../../experiments/state/m10-09-native-install-windows-results.json)

Each receipt validates against `context.native-install-matrix/v1alpha1` and has
external services `0`, administrator requirement `false`, container requirement
`false`, artifact leaks `0`, and rollback/export hash consistency `true`.

## Verification

| Gate | Result |
|---|---|
| strict bundle schema and registry | passed |
| export/import State equality | passed |
| replace/rollback/toggle | passed |
| tamper leaves target unchanged | passed |
| unregistered traversal member | rejected |
| ResourceWarning under Python 3.14 | `0` |
| focused bundle tests | `5/5` |
| native steps | `18/18` passed |
| full repository | `1847` discovered; `1816` passed; `31` conditional skip; `0` failed; `353.594 s` |

## Boundaries

- The bundle contains local State, project profile, admitted governance/routing
  files, source/checkpoint bindings, and content-addressed artifacts. It does not
  contain provider transcripts, runtime virtual environments, credentials, or
  derived resume packets.
- This migration preserves one local-embedded authority. Shared-strong database
  migration, remote leases, tenant audit retention, and cross-provider lifecycle
  hooks remain separate capability work.
- The published `0.1.0a1` artifacts predate this implementation. Publication of
  the migration CLI requires a subsequent versioned release.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_local_state_bundle_cli -v
.venv/bin/python tools/run_native_install_matrix.py \
  --root . \
  --artifact /path/to/context_control_plane-0.1.0a1-py3-none-any.whl \
  --package-module context_control_plane \
  --output /path/to/receipt.json
```
