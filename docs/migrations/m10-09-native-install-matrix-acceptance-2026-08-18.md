# M10-09 Native Install Matrix Acceptance

Version: 1  
Date: 2026-08-18  
Status: partial / Linux local-embedded verified; migration and native runner gates open

```yaml
document_id: context.m10-09-native-install-matrix-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m10-09-native-install-matrix-v1
affected_tasks: [M2-09, M10-09]
next_review: M10-09
```

## Contract

`context.native-install-matrix/v1alpha1` records one platform, package artifact,
requested runtime profile, install/verify/export/import/rollback/uninstall steps,
consistency boundaries, verdict, and receipt digest. The contract permits
`passed`, `blocked`, `failed`, and `unavailable`; a blocked step cannot be reported
as a completed matrix.

The default profile is `local-embedded`: SQLite authority, zero external services,
no administrator requirement, and no container requirement. The receipt validator
rejects profile overclaims, step tampering, rollback/export hash mismatch, and
failed-install/successful-uninstall combinations.

## Linux Probe

| Step | Result | Duration |
|---|---|---:|
| offline wheel install | passed | `870.4542 ms` |
| CLI verify | passed | `96.9552 ms` |
| export | blocked | CLI contract pending |
| import | blocked | CLI contract pending |
| rollback | blocked | CLI contract pending |
| uninstall | passed | `0.4078 ms` |

| Boundary | Result |
|---|---:|
| matrix verdict | `blocked` |
| platform | Linux `x86_64`, Python `3.14.6` |
| package artifact | wheel SHA-256 `fcc5653649704891350f9de732fcb10a33dd2fc372a2e01801fac6b0179f03df` |
| external services | `0` |
| State write authority | `false` |
| artifact leaks | `0` |
| receipt | [`m10-09-native-install-matrix-results.json`](../../experiments/state/m10-09-native-install-matrix-results.json) |

## SkyServer And Skyindows Probes

The same release wheel was installed with normal dependency resolution on both remote
hosts. Each probe initialized an isolated project, ran `verify` and `doctor`, then removed
the install target and project directory without administrator access.

| Host | Platform | Install | Verify + doctor | Uninstall | Verdict |
|---|---|---:|---:|---:|---|
| SkyServer | macOS 15.6.1 arm64 / Python 3.14.1 | `6385.1462 ms` | `155.7239 ms` | `24.8827 ms` | blocked by migration steps |
| Skyindows | Windows 11 AMD64 / Python 3.12.10 | `10078.0 ms` | `156.0 ms` | `47.0 ms` | blocked by migration steps |

Receipts: [macOS](../../experiments/state/m10-09-native-install-macos-results.json),
[Windows](../../experiments/state/m10-09-native-install-windows-results.json).

## Open Gates

- implement profile export/import and rollback with atomic temporary-tree replacement;
- implement the export/import/rollback CLI contract and run it on all three hosts;
- compare package, project profile, state snapshot, and rollback hashes across all
  three platforms;
- record native runner receipts before changing M10-09 to `✅`.

The public alpha remains local-embedded by default. PostgreSQL, Docmost, containers,
and administrator access are not required for this matrix.

## Reproduction

```bash
.venv/bin/python tools/run_native_install_matrix.py \
  --root . \
  --artifact /path/to/continuity_plane-0.1.0a1-py3-none-any.whl \
  --package-module continuity_plane \
  --output experiments/state/m10-09-native-install-matrix-results.json
```
