# Project Master

[中文](MASTER.md)

Version: 1

## Purpose

Define the stable project outcome, non-negotiable constraints, work graph, and
completion gates. Runtime task state belongs in `STATUS.md` and the local state
store.

## Constraints

- Current code and versioned evidence override recalled history.
- Completed or rejected work cannot be reopened without an explicit correction.
- External effects require an active claim, expected revision, and validator.
- Token reduction cannot weaken build, test, security, or evidence gates.

## Work

| ID | Status | Outcome | Dependencies | Completion gate |
|---|---|---|---|---|
| work-initial | planned | Define the first measurable outcome | none | evidence recorded |

## Release Gates

- Required work is complete.
- Current evidence resolves every completion claim.
- Repository verification passes.
- Secrets, personal paths, private identifiers, and raw transcripts are absent.
