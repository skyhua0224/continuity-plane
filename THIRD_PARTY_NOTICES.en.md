# Third-Party Notices

[中文](THIRD_PARTY_NOTICES.md)

Continuity Plane uses or interoperates with the following third-party
components. These components remain subject to their own licenses.

## Runtime Dependencies

| Component | Role | License | Source |
|---|---|---|---|
| PyYAML | YAML parsing | MIT | <https://github.com/yaml/pyyaml> |
| jsonschema | JSON Schema validation | MIT | <https://github.com/python-jsonschema/jsonschema> |
| psycopg | Optional PostgreSQL adapter | LGPL-3.0-only | <https://github.com/psycopg/psycopg> |

The project does not vendor these Python packages. Package installers resolve
them as declared dependencies. The PostgreSQL dependency remains optional.

## SPDX License Identifier Snapshot

The packaged Skill validator contains a generated snapshot of SPDX license
identifiers sourced from `spdx/license-list-data` at revision
`c4a7237ec8f4654e867546f9f409749300f1bf4c`. The snapshot contains identifiers
and provenance metadata, not the full license texts.

Source: <https://github.com/spdx/license-list-data>

## Development Tools

Build, test, lint, audit, and release tools are development dependencies and
are not bundled into the runtime wheel. Their licenses are available from
their respective distributions.
