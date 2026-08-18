# M10-08 Public Release Surface Acceptance

版本：1  
日期：2026-08-18  
状态：superseded by M10-10 public history publication

## Scope

The release surface is built from an allowlisted dependency closure. The
development `MASTER.md`, `STATUS.md`, `AGENTS.md`, migration history, research
corpus, raw transcripts, and current repository history are excluded. A fresh
Git history is initialized with a generic release identity.

## Artifact

| Artifact | Result |
|---|---|
| Fresh mirror | `87` files, one `main` commit |
| Mirror commit | `05df063e8d7a0c2e590093624394c6965811c062` |
| Wheel | `context_control_plane-0.1.0a1-py3-none-any.whl` |
| Wheel SHA-256 | `d2596026f0cb3ab06b5372b74ad74b767edabfa87801bf58f79f73d37b4458c3` |
| Source archive SHA-256 | `3899f0cd52dd7ad123e154397cadaabe5301940cbed921637afa518ca0a4cfb7` |
| Wheel size | `196167` bytes |
| Source archive size | `176532` bytes |

## Gates

| Gate | Evidence |
|---|---|
| Public contract tests | `5/5` passed |
| Internal regression | `1824` discovered; `1793` passed; `31` environment-gated skips; `0` failures; `223.155 s` |
| Packaged module imports | `35/35` passed in an isolated environment |
| CLI install flow | `init`, `verify`, `doctor`, and `state show` passed from wheel-only install |
| SQLite authority | revision `0` snapshot created; event/CAS and checkpoint round-trip public tests passed |
| Worktree marker scan | `0` findings |
| Git history marker scan | `0` findings; one release identity only |
| Wheel and source archive marker scan | `0` findings |
| Gitleaks worktree/history/wheel/sdist | `0` findings |
| Package metadata | `twine check` passed; `pip check` passed |
| Dependency audit | no known vulnerabilities; unpublished project skipped by advisory service |
| Local synthetic benchmark | `1000/1000` reads; quality `1.0`; median `0.186336 ms`; p95 `0.418620 ms`; external services `0` |

## Method

The release builder copies only neutral root documents, public docs, templates,
runtime schemas, optional PostgreSQL migration data, a closed Python import
closure, public tests, and aggregate benchmark data. It then scans the complete
worktree and the fresh Git history before committing. Clean-room verification
builds both sdist and wheel, installs the wheel in a new virtual environment,
imports every packaged module, executes the CLI flow, and repeats secret and
marker scans on extracted archives.

## Boundary

This is an alpha release candidate. Linux x86_64 clean-room verification is
complete. Native Windows and macOS installation, migration, and uninstall are
M10-09 work. Shared PostgreSQL and human-console deployments remain opt-in and
are not required by the local profile.

The single-commit mirror recorded here was a pre-publication release-surface
gate. M10-10 replaced it with a sanitized 27-commit public history while
preserving the same release-neutral boundary.
