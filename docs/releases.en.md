# Releases, versions and release-note conventions

[中文](releases.md) · [Changelog](../CHANGELOG.en.md) · [Deployment](deployment.en.md)

## Release category versus alpha version

Published GitHub entries use regular **Release**. The current version is Alpha35, package `0.1.0a36`, tag `v0.1.0-alpha.36`; mark it Latest when published. This does not claim production stability or change PyPI prerelease ordering: use an exact version or `--pre`. [Alpha35: Observable dispatch and resumable waits](release-alpha35.md).

Metadata cleanup does not rewrite tags/commits/assets/packages or historical publication dates. Old compatibility/evidence scope remains historical.

## Titles and notes

Use `Continuity Plane Alpha N · User-facing theme`. Recent themes: [Alpha36](release-alpha36.md) agents use it on their own, fewer tokens; [Alpha35](release-alpha35.md) observable dispatch and resumable waits; [Alpha34](release-alpha34.md) HTTP interception vs. content refusal; [Alpha33](release-alpha33.md) dispatch hot path/indexed history; [Alpha32](release-alpha32.md) host recovery/dependencies/explainable dispatch; [Alpha31](release-alpha31.md) reliable inbox/structured MCP errors; [Alpha30](release-alpha30.md) reliable queue/actionable handoff; [Alpha29](release-alpha29.md) controller continuity/scoped memory; [Alpha28](release-alpha28.md) existing Gitea login aliases; [Alpha27](release-alpha27.md) private Issue/PR collaboration.

Avoid private project/device names, internal task IDs, raw commit-title lists and unsupported superlatives.

From Alpha36 on, release notes follow this shape. The reader is someone about to upgrade: first say what this version solves, then what they need to do.

1. **Title and one paragraph**: the problem this version solves and who benefits most.
2. **Must-know notes first, in bold**: reconnect MCP, configuration changes, incompatibilities.
3. **Changes grouped by theme** with an emoji heading. One Chinese sentence then one English sentence per item, same facts:
   - "Added X: when you …, you can now …."
   - "X now does Y (it used to Z)."
   - "Fixed … when …."
4. **Measurements** from this version only: machine, data size, method (tool, runs), median and p95, the comparison if any, and how to reproduce.
5. **Known limits**, one sentence each.
6. **Upgrade** command, then links to the changelog and documentation.

Write user-visible behaviour. Internal file names, class names and implementation paths belong in the changelog or commit history. Older experiments keep their version and date and live in [historical experiments](benchmarks.en.md).

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
