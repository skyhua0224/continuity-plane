# M10-10 Continuity Plane Public Publication Acceptance

版本：1  
日期：2026-08-18  
状态：verified

## Publication

| Item | Result |
|---|---|
| Repository | <https://github.com/skyhua0224/continuity-plane> |
| Visibility | public |
| Branch | `main` |
| Public head | `3dab3d7d1d7ee1f9428411b1edbb6cb3aca9af15` |
| Git history | `27` commits: `26` real first-parent public projections plus the release commit |
| Contributor | `skyhua0224`; all commits use the GitHub noreply identity and DCO sign-off |
| Tag | `v0.1.0-alpha.1` annotated tag |
| Release | <https://github.com/skyhua0224/continuity-plane/releases/tag/v0.1.0-alpha.1> |
| License | Apache-2.0; `LICENSE`, `NOTICE`, third-party notices present |
| Repository tree | `88` blobs; development MASTER/STATUS/AGENTS and internal evidence trees absent |

## Assets

| Asset | SHA-256 | Size |
|---|---|---:|
| `continuity_plane-0.1.0a1-py3-none-any.whl` | `b214c83d63e7ef426acc1eb70410694360b66ab5d1a79698a73ee751cb1465ff` | `200692` bytes |
| `continuity_plane-0.1.0a1.tar.gz` | `d0d0b326f57e2d796487cb838b682577be67e225e32dd027017e02b29c648e27` | `181576` bytes |

## Gates

- Public contract tests: `5/5`.
- Packaged import: `35/35` modules in an isolated environment.
- CLI flow: `continuity init`, `verify`, `doctor`, and `state show` passed.
- Gitleaks: worktree, all `27` history commits, wheel, and sdist all `0` findings.
- Legacy product/provider/private marker scan: `0` findings.
- `twine check`, `pip check`, Apache license text comparison, and dependency audit passed.
- Internal regression: `1824` discovered, `1793` passed, `31` environment-gated skips, `0` failures.
- Synthetic local authority benchmark: `1000/1000`, quality `1.0`, p95 `<1 ms`, external services `0`.

## Boundary

The alpha publication is verified on Linux x86_64. Native Windows/macOS
installation and shared deployment remain M10-09 work. The release does not
require PostgreSQL, Docmost, Docker, or an Agent plugin.
