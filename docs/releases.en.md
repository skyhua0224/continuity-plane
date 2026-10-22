# Releases, versions and release-note conventions

[中文](releases.md) · [Changelog](../CHANGELOG.en.md) · [Deployment](deployment.en.md)

## Release category versus alpha version

Published GitHub entries use regular **Release**. The current version is Alpha34, package `0.1.0a34`, tag `v0.1.0-alpha.34`; mark it Latest when published. This does not claim production stability or change PyPI prerelease ordering: use an exact version or `--pre`. [Alpha34: Separate HTTP interception from model content refusals](release-alpha34.md).

Metadata cleanup does not rewrite tags/commits/assets/packages or historical publication dates. Old compatibility/evidence scope remains historical.

## Titles and notes

Use `Continuity Plane Alpha N · User-facing theme`. Recent themes: [Alpha34](release-alpha34.md) HTTP interception vs. content refusal; [Alpha33](release-alpha33.md) dispatch hot path/indexed history; [Alpha32](release-alpha32.md) host recovery/dependencies/explainable dispatch; [Alpha31](release-alpha31.md) reliable inbox/structured MCP errors; [Alpha30](release-alpha30.md) reliable queue/actionable handoff; [Alpha29](release-alpha29.md) controller continuity/scoped memory; [Alpha28](release-alpha28.md) existing Gitea login aliases; [Alpha27](release-alpha27.md) private Issue/PR collaboration.

Avoid private project/device names, internal task IDs, raw commit-title lists and unsupported superlatives.

Each bilingual note contains the same facts:

1. What the user can do now and who benefits.
2. Observable changes and fixes, separate from plans.
3. Compatibility, schema, reconnect, data and rollback warnings.
4. Exact version installation/upgrade; package and plugins separately.
5. Evidence and limits, separating new/controlled/native/historical tests and skipped cases.
6. Deployment, usage, validation and changelog links.

Link current docs to main; do not invent tag links to documents absent from that historical tag.

## Maintainer checklist

- Bind tests/build to the same source; pass public privacy, link, CLI-example and package checks.
- Align CLI/MCP/plugin versions; installation is not observed native-hook delivery.
- Publish normal commits/tags without moving tags or overwriting PyPI versions.
- Create new GitHub entries as regular Release and mark the new one Latest.
- Use the existing OIDC Trusted Publisher; never publish account credentials.
- For cleanup, PATCH existing release metadata only. Back up metadata and verify unchanged release IDs, tags, published_at, asset IDs and digests; never delete/recreate releases or reupload assets.
- Observe workflows; an existing version must not be overwritten to repair a category.
- Update adoption/rollback/limits before claiming readiness; do not invent savings or costs.

[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#update-a-release) · [Workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#release).
