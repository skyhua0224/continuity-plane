# Continuity shared task template

Copy the block below into a new Issue. Replace the stable task ID, objective,
acceptance and next action. Set the title in the normal Issue title field.
No Continuity installation is required to read, append progress or review its PR.

Leave assignee null until a member accepts driving the task. Driver and state are
projected from this block and readable Continuity event comments. Native sidebar
assignment discrepancies are exposed as conflicts. Claims are coordination-only.

<!-- continuity-task:v1 -->
```json
{
  "task_id": "replace-with-unique-task-id",
  "objective": "Desired result",
  "next_action": "First concrete action",
  "exit_criteria": ["Observable acceptance criterion"],
  "priority": "p1",
  "dependencies": [],
  "allowed_files": [],
  "assignee": null,
  "status": "queued",
  "body": "Background, constraints, and verification commands"
}
```

Append human progress outside the JSON block. Malformed original text is preserved
and reported; Continuity never clears or rebuilds it. Task state does not authorize
merging, publishing or deployment.
