# Replay Fixtures

Fixtures are sanitized, minimal, deterministic regressions extracted from real failures. Raw provider transcripts do not belong here.

Each fixture must contain:

- stable fixture ID and schema version;
- project/task IDs and initial typed state;
- minimal user/model/tool events required to reproduce the failure;
- expected active task, decisions, constraints, blockers, effects, and next action;
- expected allow/veto result;
- opaque source hash and extraction version;
- sanitizer result and sensitivity classification;
- evidence references that establish the expected result.

The first AlkaidLab corpus must cover CL queue bypass, LocalSend experiment promotion, single-ping inference, pacer comparison mismatch, N-68 rollback, N-42/N-67 blocking, final-SRTT misuse, measurement-harness bottlenecks, task switching, stale Skills, concurrent claims, backend 503, checkpoint corruption, and crash boundaries around commits.

Use JSON for versioned fixtures. `*.jsonl` is ignored to prevent accidental provider-transcript commits.

