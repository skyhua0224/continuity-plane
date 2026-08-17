# Security Policy

[中文](SECURITY.md)

## Reporting

Report vulnerabilities privately to the maintainers of the repository where
you obtained this software. Do not open a public issue for a vulnerability that
could expose credentials, private source, state records, or external effects.

For the canonical repository, use GitHub private vulnerability reporting:
<https://github.com/skyhua0224/continuity-plane/security/advisories/new>.

## Security Boundaries

- Provider output and memory candidates have no State write authority.
- State writes require authorization, expected revision, and validators.
- External effects require a current claim, scope ownership, and idempotency key.
- Raw provider transcripts and credentials are excluded from Git.
- Public artifacts are scanned for secrets, personal paths, and private IDs.

The default local profile does not listen on a network port. Shared deployments
must add transport authentication, tenant/project isolation, audit retention,
backup, and recovery procedures appropriate to their environment.
