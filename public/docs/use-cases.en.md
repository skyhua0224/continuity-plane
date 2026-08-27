# Use Cases

[中文](use-cases.md)

If an agent works for hours or several Sessions share one repository, these incidents are
familiar. The common failure is a missing durable state for the current task, permissions,
and side effects outside the chat window.

## Compaction Makes The Agent Look Different

- The agent was fixing a test before compaction and answers an already settled question afterward;
- You say “continue” and it rereads the MASTER, replans, or repeats completed work;
- Rejected, reverted, or completed work appears in the queue again;
- Recovery reads so much source and documentation that the new window fills immediately;
- A 1M context delays compaction but does not remove redundant history by itself.

The control plane checkpoints the active task, latest decision, constraints, return point,
acknowledged input, and first allowed action. A canary runs before code changes, commits, or
deployment. In matched tasks, redundant-history input fell `40.25%`, near-limit history fell
`95.06%`, and answer quality stayed `3/3`.

## Sessions Step On Each Other

- Session A is deploying while Session B merges another revision into main;
- both Sessions decide that nobody is deploying and publish, roll back, or migrate the same environment;
- one Session changes a PR or force-pushes while another continues from an old head;
- a failed retry has no effect identity and repeats the same external operation.

The Work Ledger records who owns each scope and which revision is current. SQLite coordinates
multiple Sessions on one machine. Cross-device unique claims use an existing Git forge or an
explicit shared State service. PostgreSQL and Docmost are optional. Current measurements
reduced duplicate tool calls by `55.88%`, delivered same-revision dual-Session events `1000/1000`,
and recorded zero authority violations.

## People And Agents Repeat Each Other's Work

- One contributor implements a module locally and another cannot see the unpublished Work;
- reviewers, executors, and deployment Sessions search the same source and official documents again;
- a decision verified in a PR never reaches shared evidence, so the next collaborator reopens it;
- a handoff keeps only a summary and loses the blocker, next action, and return point.

Project Graph, Work Ledger, Decision Timeline, and Evidence Matrix show the mainline, owner,
dependencies, evidence, and impact. Memory supplies candidates; it cannot declare completion
or grant effect authority.

## A Casual Idea Pulls The Mainline Away

- A future idea mentioned during execution makes the agent abandon the current task;
- an experiment branch has no return point, attempt budget, or promotion gate;
- a design discussion is treated as an approved implementation order;
- a real task switch returns to a session with no checkpoint.

The default action is capture-and-continue: save the Idea and keep the active task. An Idea
enters the mainline only after an explicit switch, review, scope check, budget, and promotion
gate. Returning to the original task expands the relevant checkpoint and references instead of
replaying the entire side conversation.

## Large Projects Lose Their Shape

- Hundreds of modules, thousands of tasks, and cross-repository dependencies do not fit in a directory tree;
- an agent finds a same-named symbol but cannot tell whether it is mainline, experimental, old, or replacement code;
- a human handoff requires searching Issues, PRs, chat, and reports to understand one decision;
- a low-level change has an unclear impact on Products, Work, Constraints, and tests.

Project Graph provides a deterministic task map. Relationship/Impact explores the affected
relationship clusters. Decision Timeline and Evidence Matrix explain why a choice was made,
when it was superseded, and where its evidence lives. The projection core passed scale tests;
complete Docmost pages, Obsidian Canvas/Bases, and interaction acceptance follow the
[visual product plan](visual-products.en.md).

## Memory, Skills, And Documents Drift

- An old path, decision, or constraint returns after compaction;
- a Skill loads in full every time and may come from the wrong version;
- a changed Skill path, digest, dependency, or applicability goes unnoticed;
- MASTER and STATUS are either stale or filled with every session narrative;
- standards and official documents are searched repeatedly without revision or freshness.

Historical memory is marked as a candidate. Skills use versions, hashes, rule IDs,
applicability, and quarantine. MASTER holds governance intent, STATUS holds the current route,
and reports or projections cannot submit authoritative state. Current Skill source bytes fell
`96.54%`; long-term token and window utilization still require host traces.

## Quality Must Survive Speed

- Evidence or constraints are dropped to make the prompt smaller;
- affected builds, tests, fault injection, or live checks are skipped to finish sooner;
- the model claims completion without tests, artifact hashes, or current provenance;
- recovery continues side effects after a crash, 503, or corrupted checkpoint.

The mandatory consistency campaign passes `10/10`; compaction, Idea, interrupt,
and worker-loss faults pass `4/4`;
stale-history revival, silent CAS overwrite, and authority violations are `0`. Complete
measurements, methods, and limitations are in the [benchmark method](benchmarks.en.md).
