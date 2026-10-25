"""One parameter contract for local planning CLI, MCP, and on-demand help."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path

OPERATIONS = {
    **{
        f"memory.{name}": ("memory", name)
        for name in (
            "note",
            "notes",
            "show",
            "adopt",
            "revoke",
            "grant",
            "consent-revoke",
            "ingest",
            "share",
            "history",
        )
    },
    **{f"profile.{name}": ("profile", name) for name in ("show", "set")},
    "controller.start": ("controller", "start"),
    **{
        f"controller.{name}": ("controller", name)
        for name in (
            "acquire",
            "renew",
            "release",
            "status",
            "reservations",
            "reconcile-host",
        )
    },
    **{f"session.{name}": ("session", name) for name in ("bind", "show", "unbind")},
    **{f"lesson.{name}": ("lesson", name) for name in ("add", "list", "show")},
    **{f"handoff.{name}": ("handoff", name) for name in ("write", "show")},
    **{
        f"project.context-{name}": ("collaboration", "context-" + name)
        for name in ("set", "show")
    },
    "storage.inspect": ("storage", "inspect"),
    **{
        f"quota.{name}": ("quota", name)
        for name in ("record", "report", "handoff", "return")
    },
    **{
        f"rules.{name}": ("rules", name)
        for name in (
            "export",
            "check-mirror",
            "attachment",
            "update",
            "history",
            "affected",
            "snapshot-export",
            "snapshot-import",
            "import",
        )
    },
    **{
        f"reference.{name}": ("reference", name)
        for name in (
            "add",
            "list",
            "show",
            "record",
            "preference",
            "import-tsv",
            "export-tsv",
        )
    },
    **{
        f"todo.{name}": ("todo", name)
        for name in (
            "compile",
            "list",
            "complete",
            "block",
            "link",
            "activate",
            "from-cards",
        )
    },
    **{
        f"worker.queue.{name}": ("worker", "queue", name)
        for name in ("enqueue", "status", "pause", "resume", "cancel", "requeue")
    },
    **{
        f"task.{name}": ("collaboration", "task", name)
        for name in (
            "add",
            "update",
            "list",
            "show",
            "claim",
            "next",
            "release",
            "block",
            "handoff-request",
            "handoff-accept",
            "ready-for-review",
            "request-review",
            "resolve",
        )
    },
    **{
        f"backend.{name}": ("collaboration", "backend", name)
        for name in ("configure", "doctor", "sync", "pending", "replay", "cached")
    },
    **{
        name: ("collaboration", name)
        for name in (
            "packet",
            "changes",
            "wait",
            "recommend",
            "scenarios",
            "doctor",
            "drift",
            "prompt",
            "batch-add",
            "batch-list",
            "status-render",
            "decision-add",
            "decision-list",
            "outbox-replay",
            "repair-events",
        )
    },
    **{
        f"inbox.{name}": ("collaboration", "inbox", name)
        for name in ("send", "list", "ack", "archive", "unread", "wait")
    },
    **{
        name: ("collaboration", name.replace(".", "-"))
        for name in (
            "rule.add",
            "rule.list",
            "commitment.add",
            "commitment.list",
            "commitment.close",
        )
    },
    "project.register": ("collaboration", "register"),
    "whoami": ("whoami",),
    "usage.ingest": ("observe", "ingest-usage"),
    "usage.report": ("observe", "report"),
    "worker.prepare": ("worker", "prepare"),
}
ALIASES = {
    f"continuity_todo_{name}": f"todo.{name}"
    for name in ("compile", "list", "complete", "block", "link", "activate")
}
CLI_HELP = {
    "controller.run": ("controller", "run"),
    "controller.clear-reservation": ("controller", "clear-reservation"),
    "controller.clear-operation": ("controller", "clear-operation"),
    "controller.host-capacity": ("controller", "host-capacity"),
    "quota.refresh": ("quota", "refresh"),
    "quota.tick": ("quota", "tick"),
    **{
        f"worker.{n}": ("worker", n)
        for n in (
            "dispatch",
            "resume",
            "report",
            "configure",
            "clear-policy-review",
            "events",
            "jobs",
            "fallback-policy",
            "completion-hook",
        )
    },
    "worker.queue.run": ("worker", "queue", "run"),
    "supervisor.run": ("supervisor", "run"),
    "bridge.install": ("bridge", "install"),
    "task.merged": ("task", "merged"),
    "task.batch-update": ("task", "batch-update"),
    "dispatch-health.show": ("dispatch-health", "show"),
    "dispatch-health.clear": ("dispatch-health", "clear"),
    **{
        f"context.{n}": ("context", n)
        for n in ("index", "lookup", "search", "dependents")
    },
}
ALIASES.update(continuity_collaboration=None)
READ_ACTIONS = {
    "memory.notes",
    "memory.show",
    "memory.history",
    "profile.show",
    "controller.status",
    "controller.reservations",
    "session.show",
    "lesson.list",
    "lesson.show",
    "handoff.show",
    "project.context-show",
    "storage.inspect",
    "reference.list",
    "reference.show",
    "rules.export",
    "rules.check-mirror",
    "rules.attachment",
    "rules.history",
    "rules.affected",
    "worker.prepare",
    "prompt",
    "batch-list",
    "status-render",
    "decision-list",
    "usage.report",
    "worker.queue.status",
    "quota.report",
    "whoami",
    "task.list",
    "task.show",
    "task.next",
    "backend.doctor",
    "backend.pending",
    "backend.cached",
    "todo.list",
    "packet",
    "changes",
    "wait",
    "scenarios",
    "doctor",
    "drift",
    "rule.list",
    "commitment.list",
    "inbox.list",
    "inbox.unread",
    "inbox.wait",
}
PLURALS = {
    "exit_criteria": "exit_criterion",
    "allowed_files": "allowed_file",
    "preauthorized_effects": "preauthorized_effect",
    "queued_effects": "queued_effect",
}


def _parser(operation):
    from .cli import build_parser

    if operation not in OPERATIONS and operation not in CLI_HELP:
        raise ValueError("unsupported planning operation; use continuity help overview")
    parser = build_parser()
    for part in (OPERATIONS | CLI_HELP)[operation]:
        group = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        parser = group.choices[part]
    return parser


def help_for(topic="overview"):
    if topic in {
        "overview",
        "todo",
        "task",
        "collaboration",
        "reference",
        "rules",
        "worker",
        "supervisor",
        "bridge",
        "context",
        "backend",
        "memory",
        "profile",
        "handoff",
        "inbox",
        "dispatch-health",
    }:
        names = [
            name
            for name in (OPERATIONS | CLI_HELP)
            if topic in {"overview", "collaboration"} or name.startswith(topic + ".")
        ]
        return {
            "topic": topic,
            "operations": names,
            "scope": "Local planning by default; configured Gitea tasks use shared Issues/PR via member-local tea. No State claim, provider launch, merge or deployment.",
            "next": "Use continuity help OPERATION for its complete CLI/MCP parameters.",
            "cli_only_operations": list(CLI_HELP),
        }
    parser = _parser(topic)
    parameters = []
    for action in parser._actions:
        if action.dest == "help":
            continue
        parameters.append(
            {
                "name": "full" if "--full" in action.option_strings else action.dest,
                "flags": action.option_strings,
                "required": action.required,
                "repeatable": isinstance(action, argparse._AppendAction),
                "type": getattr(
                    action.type,
                    "__name__",
                    "boolean"
                    if isinstance(
                        action, (argparse._StoreTrueAction, argparse._StoreFalseAction)
                    )
                    else "string",
                ),
                "choices": list(action.choices) if action.choices is not None else None,
                "default": action.default,
                "help": action.help,
            }
        )
    return {
        "operation": topic,
        "cli_only": topic in CLI_HELP,
        "parameters": parameters,
        "help": parser.format_help(),
        "mutates_local_metadata": topic not in READ_ACTIONS,
        "shared_backend_effects": "Configured gitea-issues task mutations publish to Issues or request real PR reviews using local member authentication; no merge/deploy authority",
    }


def _brief(value):
    if isinstance(value, dict):
        if value.get("packet_kind") == "controller-start/v1":
            return [
                f"status={value['status']} handoff_revision={value['handoff_revision']} complete={value['complete']}",
                "profile="
                + json.dumps(value["profile"], ensure_ascii=False, sort_keys=True),
                "plan: " + str(value.get("plan") or ""),
                "next: " + str(value.get("next_action") or ""),
                "pending=" + json.dumps(value["pending_decisions"], ensure_ascii=False),
                *[r["record_id"] + " | " + r["text"] for r in value["memory"]],
                f"memory_omitted={value['memory_omitted']} shared_conflicts={value['shared_conflicts']}",
                value["instruction_boundary"],
                value["observations"],
            ]
        if "prompt" in value and "prompt_sha256" in value:
            return [
                f"prompt_sha256={value['prompt_sha256']} rules_digest={value.get('rules_digest')}",
                value["prompt"],
            ]
        if "rules" in value and "rules_digest" in value:
            return [
                f"complete={value.get('complete')} rules={value.get('rule_count')} digest={value['rules_digest']}"
            ] + [_row(r) for r in value["rules"]]
        if "offset_chars" in value and "sha256" in value:
            return [
                f"sha256={value['sha256']} offset={value['offset_chars']} next_offset={value.get('next_offset_chars')} complete={value['complete']}",
                value["text"],
            ]
        if "parameters" in value:
            return [
                value["operation"]
                + (
                    " (CLI only; not a planning MCP action)"
                    if value.get("cli_only")
                    else ""
                ),
                value["help"].strip(),
            ]
        if "operations" in value:
            return [
                value["scope"],
                *[
                    name
                    + (
                        " (CLI only)"
                        if name in value.get("cli_only_operations", [])
                        else ""
                    )
                    for name in value["operations"]
                ],
                value["next"],
            ]
        if "read_model" in value and isinstance(value.get("observed"), dict):
            observed = value["observed"]
            changed = len(observed.get("changed_cards", []))
            total = observed.get("changed_cards_total", changed)
            pending = value.get("declared", {}).get("pending_decisions") or []
            attention = observed.get("attention") or []
            inbox = observed.get("inbox", {})
            inbox_hint = (
                f"unread={inbox.get('count', 0)} {inbox.get('unit', 'inbox messages')} "
                f"scope={inbox.get('scope', 'unknown')} oldest={inbox.get('oldest_at')}; "
                f"bulk-ack: {inbox.get('bulk_ack_command', 'continuity collaboration inbox ack --before TIME --limit 1000')}; "
                f"ack-one: {inbox.get('acknowledge_command', 'continuity collaboration inbox ack')}; "
                f"archive-after-ack: {inbox.get('archive_command', 'continuity collaboration inbox archive --limit 1000')}"
            )
            return [
                f"handoff revision={value.get('revision', 0)} as_of={observed.get('as_of')} running={observed.get('running_workers_total', 0)} attention={len(attention)} changed_cards={changed}/{total} remaining_changed_cards={max(0, total - changed)}",
                f"unread={inbox.get('count', 0)} {inbox.get('unit', 'inbox messages')} scope={inbox.get('scope', 'unknown')} oldest={inbox.get('oldest_at')}",
                *[f"PENDING DECISION | {str(item)[:320]}" for item in pending],
                *[_row(r) for r in attention],
                "plan: " + str(value.get("plan") or "")[:300],
                "next: " + str(value.get("next_action") or "")[:240],
                inbox_hint,
                *[
                    _row(r)
                    for r in observed.get("changed_cards", [])
                    if r.get("task_id") not in {a.get("task_id") for a in attention}
                ],
                "Read JSON for declared/observed provenance and omitted details.",
            ]
        rows = next(
            (
                value[k]
                for k in ("items", "tasks", "changes", "messages", "matches")
                if isinstance(value.get(k), list)
            ),
            None,
        )
        if rows is not None:
            heading = f"status={value.get('status', 'ok')} count={len(rows)}"
            if value.get("truncated"):
                totals = [
                    value[k]
                    for k in (
                        "total_items",
                        "total_tasks",
                        "total_changes",
                        "total_messages",
                        "total_matches",
                    )
                    if isinstance(value.get(k), int)
                ]
                total = max(totals or [len(rows)])
                heading += f" total={total} remaining={max(0, total - len(rows))} truncated=true"
                heading += " [narrow filters or increase max_output_bytes]"
            if "next_cursor" in value:
                heading += f" next_cursor={value['next_cursor']} timed_out={value.get('timed_out', False)}"
            if value.get("ack_revision"):
                heading += " ack_revision=" + value["ack_revision"]
            if value.get("has_more"):
                heading += " has_more=true"
            if isinstance(value.get("consumer"), dict):
                heading += f" consumer={value['consumer']['name']} acknowledged_cursor={value['consumer']['acknowledged_cursor']}"
            return [heading] + [_row(row) for row in rows]
        return [_row(value)]
    if isinstance(value, list):
        return [f"count={len(value)}"] + [_row(row) for row in value]
    return [str(value)]


def _row(row):
    if not isinstance(row, dict):
        return str(row)
    if "reference_id" in row:
        fields = [
            row.get("scope"),
            row["reference_id"],
            row.get("review_status"),
            row.get("title"),
            row.get("source"),
            row.get("version"),
            row.get("section"),
        ]
        if row.get("review_due"):
            fields.append("review-due")
        return " | ".join(str(v).replace("\n", " ") for v in fields if v)
    identity = (
        row.get("record_id")
        or row.get("rule_id")
        or row.get("task_id")
        or row.get("item_id")
        or row.get("message_id")
        or row.get("project_id")
        or ""
    )
    status = row.get("execution_state") or row.get("status") or row.get("event") or ""
    title = (
        row.get("title")
        or row.get("next_action")
        or row.get("text")
        or row.get("reason")
        or ""
    )
    if not identity and not status and not title:
        return json.dumps(row, ensure_ascii=False, sort_keys=True)
    line = " | ".join(str(v).replace("\n", " ") for v in (identity, status, title) if v)
    if row.get("assignee"):
        line += " | " + str(row["assignee"])
    if row.get("blocked_reason"):
        line += " | blocked: " + str(row["blocked_reason"])[:240]
    if row.get("next_action") and row.get("title"):
        line += " -> " + str(row["next_action"]).replace("\n", " ")
    if row.get("attachment"):
        line += (
            " | attachment="
            + row["attachment"]["sha256"]
            + " "
            + row["attachment"]["read_command"]
        )
    if row.get("evidence_gaps"):
        line += " | EVIDENCE GAPS: " + ", ".join(row["evidence_gaps"])
    if row.get("planning_warnings"):
        line += " | NOTE: " + "; ".join(row["planning_warnings"])
    if row.get("authority") == "gitea-issue":
        line += (
            " | revision="
            + str(row.get("remote_revision", ""))
            + " | coordination-only, exclusive=false"
        )
    for flag in ("review_requested", "handoff_requested", "dependencies_ready"):
        if row.get(flag):
            line += " | " + flag
    if row.get("conflict_count"):
        line += " | conflicts=" + str(row["conflict_count"])
    return line


def _critical_handoff(value, budget=None):
    declared = value.get("declared") or {}
    observed = value.get("observed") or {}
    pending = list(declared.get("pending_decisions") or [])
    attention = list(observed.get("attention") or [])
    candidates = []
    for pending_limit, attention_limit, text_limit in (
        (4, 8, 320),
        (3, 5, 240),
        (2, 3, 160),
        (1, 2, 100),
        (1, 2, 64),
        (1, 2, 48),
        (1, 2, 32),
        (1, 1, 64),
        (1, 1, 48),
    ):
        candidate = {
            "revision": value.get("revision", 0),
            "declared": {
                "plan": str(declared.get("plan") or "")[: text_limit * 2],
                "next_action": str(declared.get("next_action") or "")[:text_limit],
                "pending_decisions": [
                    str(item)[:text_limit] for item in pending[:pending_limit]
                ],
                "pending_decisions_total": len(pending),
            },
            "observed": {
                "as_of": observed.get("as_of"),
                "attention": [
                    {
                        key: (
                            str(row.get(key))[:text_limit]
                            if key in {"title", "reason"}
                            else row.get(key)
                        )
                        for key in (
                            "kind",
                            "task_id",
                            "status",
                            "reason",
                            "failure_class",
                        )
                        if row.get(key) is not None
                    }
                    for row in attention[:attention_limit]
                ],
                "attention_total": len(attention),
                "inbox": {
                    key: (observed.get("inbox") or {}).get(key)
                    for key in (
                        "count",
                        "unit",
                        "scope",
                        "oldest_at",
                        "acknowledge_command",
                    )
                    if (observed.get("inbox") or {}).get(key) is not None
                },
                "change_cursor": observed.get("change_cursor"),
            },
            "truncated": True,
            "details_omitted": True,
            "priority": "pending-decisions-and-blocked-failed-attention-first",
        }
        candidates.append(candidate)
        if (
            budget is None
            or len(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode())
            <= budget
        ):
            return candidate
    return candidates[-1]


def render(value, *, format="json", max_output_bytes=8192):
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 131072:
        raise ValueError("max_output_bytes must be between 1024 and 131072")
    if format not in {"json", "brief"}:
        raise ValueError("format must be json or brief")
    budget = max_output_bytes - 1  # reserve the CLI newline
    if format == "brief":
        text = "\n".join(_brief(value))
        if len(text.encode()) > budget:
            suffix = "\n[output truncated; narrow filters or increase max_output_bytes]"
            text = (
                text.encode()[: budget - len(suffix.encode())].decode("utf-8", "ignore")
                + suffix
            )
        return text
    view = copy.deepcopy(value)
    while True:
        text = json.dumps(view, ensure_ascii=False, sort_keys=True)
        if len(text.encode()) <= budget:
            return text
        if isinstance(view, dict) and isinstance(view.get("observed"), dict):
            view["details_omitted"] = True
            view["truncated"] = True
            if "snapshot_scene" in view:
                del view["snapshot_scene"]
                continue
            if "scene" in view:
                del view["scene"]  # Compatibility alias, not a second observation.
                continue
            observed = view["observed"]
            field = next(
                (
                    k
                    for k in (
                        "changes_since_handoff",
                        "recent_batches",
                        "approved_decisions",
                        "changed_cards",
                        "workers",
                        "native_workers",
                        "reports",
                        "drift",
                    )
                    if observed.get(k)
                ),
                None,
            )
            if field:
                # Plain history is discarded before attention. Blocked,
                # failed and report-without-card-update rows live in attention.
                if field in {
                    "changes_since_handoff",
                    "recent_batches",
                    "approved_decisions",
                }:
                    observed[field].clear()
                else:
                    observed[field].pop()
                continue
            critical = _critical_handoff(view, budget)
            if critical != view:
                view = critical
                continue
        if isinstance(view, list):
            view = {"items": view, "total_items": len(view), "truncated": True}
        elif isinstance(view, dict):
            key = next(
                (
                    k
                    for k in (
                        "items",
                        "tasks",
                        "changes",
                        "parameters",
                        "messages",
                        "matches",
                    )
                    if isinstance(view.get(k), list) and view[k]
                ),
                None,
            )
            if key:
                view.setdefault("total_" + key, len(view[key]))
                view[key].pop()
                view["truncated"] = True
                if key == "changes":
                    view["next_cursor"] = (
                        view[key][-1]["cursor"] if view[key] else view.get("since", 0)
                    )
            else:
                essential = {
                    k: v
                    for k, v in view.items()
                    if k
                    in {
                        "task_id",
                        "item_id",
                        "status",
                        "queue_id",
                        "project_id",
                        "next_cursor",
                        "total_items",
                        "total_tasks",
                        "total_changes",
                        "timed_out",
                        "revision",
                        "next_action",
                        "remote_revision",
                        "operation_id",
                        "issue_number",
                        "issue_url",
                        "assignee",
                        "authority",
                        "exclusive",
                        "claim_guarantee",
                        "ack_revision",
                    }
                }
                essential.update(
                    truncated=True,
                    details_omitted=True,
                    hint="Increase max_output_bytes for complete fields.",
                )
                if isinstance(essential.get("next_action"), str):
                    essential["next_action"] = essential["next_action"][:240]
                if essential == view:
                    raise ValueError("output identifiers exceed budget")
                view = essential
        else:
            view = {"truncated": True, "hint": "Increase max_output_bytes"}


def execute(operation, parameters=None, *, root=None, project_id=None, data_root=None):
    """Invoke an allowlisted metadata command in-process, never a shell/worker."""
    from .cli import main
    from .collaboration_registry import list_projects, resolve_project

    if operation not in OPERATIONS:
        raise ValueError(
            "This operation is CLI-only; planning MCP never starts providers or installs host configuration"
        )

    parser = _parser(operation)
    parameters = {str(k).replace("-", "_"): v for k, v in (parameters or {}).items()}
    if operation == "wait" and isinstance(parameters.get("event"), str):
        # Older MCP callers supplied one string before --event was repeatable.
        parameters["event"] = [parameters["event"]]
    if operation == "inbox.wait":
        if parameters.get("follow"):
            raise ValueError("inbox.wait --follow is CLI-only; MCP waits for one bounded message")
        timeout = parameters.get("timeout_seconds", 30)
        if timeout is None or type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("MCP inbox.wait timeout_seconds must be between 0 and 30")
        parameters["timeout_seconds"] = timeout
    if operation == "status-render" and parameters.get("output"):
        raise ValueError(
            "status file output is CLI-only; planning MCP returns the read-only projection"
        )
    if operation == "quota.return" and parameters.get("execute"):
        raise ValueError(
            "executing a provider handoff is CLI-only; planning MCP can preview it"
        )
    if "full" in parameters:
        full = parameters.pop("full")
        if type(full) is not bool:
            raise ValueError("full must be boolean")
        if "summary" in parameters and parameters["summary"] != (not full):
            raise ValueError("conflicting full and summary")
        parameters["summary"] = not full
    for key in ("root", "project_id", "data_root"):
        if key in parameters:
            value = parameters.pop(key)
            current = {"root": root, "project_id": project_id, "data_root": data_root}[
                key
            ]
            if current is not None and current != value:
                raise ValueError(f"conflicting {key}")
            if key == "root":
                root = value
            elif key == "project_id":
                project_id = value
            else:
                data_root = value
    if root is not None and (not isinstance(root, str) or not Path(root).is_absolute()):
        raise ValueError("root must be an absolute project path")
    if data_root is not None and (
        not isinstance(data_root, str) or not Path(data_root).is_absolute()
    ):
        raise ValueError("data_root must be an absolute path")
    registration = resolve_project(root, data_root) if root else None
    if project_id and registration and registration["project_id"] != project_id:
        raise ValueError("root and project_id refer to different projects")
    if project_id and root is None:
        registration = next(
            (p for p in list_projects(data_root) if p["project_id"] == project_id), None
        )
        root = registration["control_root"] if registration else None
    project_id = project_id or (registration or {}).get("project_id")
    if operation != "scenarios" and root is None:
        raise ValueError("supply root or a registered project_id")
    if operation == "project.register":
        existing = next(
            (p for p in list_projects(data_root) if p["project_id"] == project_id), None
        )
        if (
            existing
            and Path(existing["control_root"]).resolve() != Path(root).resolve()
        ):
            raise ValueError("project_id is already registered at a different root")
        if (
            parameters.get("control_root")
            and Path(parameters["control_root"]).resolve() != Path(root).resolve()
        ):
            raise ValueError("root and control_root refer to different projects")
    if (
        operation.startswith("todo.") is False
        and not operation.startswith("reference.")
        and not operation.startswith("usage.")
        and not operation.startswith("rules.")
        and not operation.startswith("worker.")
        and operation not in {"scenarios", "project.register", "whoami"}
        and project_id is None
    ):
        raise ValueError(
            "project is not registered; use project.register or supply project_id"
        )
    actions = {a.dest: a for a in parser._actions if a.dest != "help"}
    if "format" in actions:
        parameters.setdefault("format", "json")
    for key, value in (
        ("root", root),
        ("project_id", project_id),
        ("data_root", data_root),
    ):
        if value is not None and key in actions:
            if key in parameters and parameters[key] != value:
                raise ValueError(f"conflicting {key}")
            parameters[key] = value
    if operation == "project.register" and "control_root" not in parameters:
        parameters["control_root"] = root
    argv = list(OPERATIONS[operation])
    for key, value in parameters.items():
        key = PLURALS.get(key.replace("-", "_"), key.replace("-", "_"))
        if key not in actions:
            aliases = {
                flag[2:].replace("-", "_"): a.dest
                for a in parser._actions
                for flag in a.option_strings
                if flag.startswith("--")
            }
            key = aliases.get(key, key)
        if key not in actions:
            raise ValueError(
                f"unsupported parameter {key}; use continuity_help({operation})"
            )
        if value is None:
            continue
        action = actions[key]
        flag = next((f for f in action.option_strings if f.startswith("--")), None)
        if flag is None:
            raise ValueError(f"positional parameter {key} is not supported")
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
            if value == isinstance(action, argparse._StoreTrueAction):
                argv.append(flag)
        elif isinstance(action, argparse._AppendAction):
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
            for entry in value:
                if isinstance(entry, (dict, list)):
                    raise ValueError(f"{key} entries must be scalar")
                argv.append(f"{flag}={entry}")
        else:
            if isinstance(value, (dict, list)):
                raise ValueError(f"{key} must be scalar")
            argv.append(f"{flag}={value}")
    output, error = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        try:
            code = main(argv)
        except SystemExit as exc:
            raise ValueError(
                error.getvalue().strip() or f"invalid parameters for {operation}"
            ) from exc
    text = output.getvalue().strip()
    if code not in {0, 2}:
        raise ValueError(f"{operation} failed with code {code}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{operation} did not return JSON") from exc
    return value


def tool_definitions():
    scope = {
        "root": {"type": "string", "minLength": 1},
        "project_id": {"type": "string", "minLength": 1},
        "data_root": {"type": "string", "minLength": 1},
        "executor_id": {
            "type": "string",
            "minLength": 1,
            "description": "Stable planning identity across connections, not native account authentication",
        },
        "format": {"enum": ["brief", "json"]},
        "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": 131072},
    }
    return [
        {
            "name": "continuity_help",
            "description": "Discover exact planning parameters from the CLI contract. Start with overview, then a specific operation. Read-only; no project claim.",
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "topic": {"type": "string", "default": "overview"},
                    "format": scope["format"],
                    "max_output_bytes": scope["max_output_bytes"],
                },
            },
        },
        {
            "name": "continuity_plan",
            "description": "Manage tasks, Todo, collaboration and sources with CLI parity. Configured Gitea projects read/write shared Issues and real PR review requests using this member's local authentication; otherwise local metadata. No provider launch, merge or deployment. Discover parameters with continuity_help. Default brief output.",
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action"],
                "properties": {
                    **scope,
                    "action": {"enum": list(OPERATIONS)},
                    "parameters": {"type": "object", "default": {}},
                },
            },
        },
    ]


def extend_tools(legacy):
    for tool in legacy:
        if tool["name"] in ALIASES:
            properties = tool["inputSchema"]["properties"]
            properties.update(
                data_root={"type": "string"},
                project_id={"type": "string"},
                executor_id={"type": "string", "minLength": 1},
                format={"enum": ["json", "brief"]},
                max_output_bytes={
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 131072,
                },
            )
            tool["inputSchema"]["required"] = [
                k for k in tool["inputSchema"].get("required", []) if k != "root"
            ]
            tool["description"] += (
                " Full parameter help: continuity_help; root or project_id."
            )
            if tool["name"] == "continuity_todo_compile":
                properties["prompt"]["maxLength"] = 24000
            operation = ALIASES[tool["name"]]
            if operation is not None:
                for spec in help_for(operation)["parameters"]:
                    name = spec["name"]
                    if name in properties:
                        continue
                    types = {"int": "integer", "float": "number", "boolean": "boolean"}
                    schema = {"type": types.get(spec["type"], "string")}
                    if spec["choices"] is not None:
                        schema["enum"] = spec["choices"]
                    if spec["repeatable"]:
                        schema = {"type": "array", "items": schema}
                    properties[name] = schema
            if tool["name"] == "continuity_collaboration":
                annotations = dict(tool.get("annotations") or {})
                annotations["openWorldHint"] = True
                tool["annotations"] = annotations
                tool["description"] += (
                    " Configured Gitea projects read shared Issues via member-local tea."
                )
                properties.update(
                    status={"type": "string"},
                    lane_id={"type": "string"},
                    updated_after={"type": "string"},
                    all={"type": "boolean"},
                    audience={"type": "string"},
                    max_entities={"type": "integer", "minimum": 1, "maximum": 10},
                )
    for name, operation, needed in (
        ("continuity_todo_link", "todo.link", ["item_id", "task_id"]),
        ("continuity_todo_activate", "todo.activate", ["item_id", "reason"]),
    ):
        legacy.append(
            {
                "name": name,
                "description": f"Local {operation}; no State claim. Use continuity_help for all parameters.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": needed,
                    "properties": {
                        "root": {"type": "string"},
                        "project_id": {"type": "string"},
                        "data_root": {"type": "string"},
                        "executor_id": {"type": "string", "minLength": 1},
                        **(
                            {"session_id": {"type": "string"}}
                            if operation == "todo.activate"
                            else {}
                        ),
                        **{key: {"type": "string", "minLength": 1} for key in needed},
                        "format": {"enum": ["brief", "json"]},
                        "max_output_bytes": {
                            "type": "integer",
                            "minimum": 1024,
                            "maximum": 131072,
                        },
                    },
                },
            }
        )
    return legacy + tool_definitions()


def handle_tool(name, arguments):
    original = dict(arguments)
    arguments = dict(arguments)
    format = arguments.pop(
        "format", "brief" if name in {"continuity_help", "continuity_plan"} else "json"
    )
    default_budget = (
        65536
        if name == "continuity_plan"
        and arguments.get("action")
        in {"rules.export", "rules.history", "rules.attachment", "task.show"}
        else 8192
    )
    budget = arguments.pop("max_output_bytes", default_budget)
    render({}, format=format, max_output_bytes=budget)  # validate before any mutation
    if name == "continuity_help":
        value = help_for(arguments.pop("topic", "overview"))
        if arguments:
            raise ValueError("unsupported help arguments")
    else:
        root = arguments.pop("root", None)
        project_id = arguments.pop("project_id", None)
        data_root = arguments.pop("data_root", None)
        explicit_executor = arguments.pop("executor_id", None)
        executor = explicit_executor or os.environ.get("CONTINUITY_ASSIGNEE")
        if executor is not None:
            from .collaboration_registry import _ASSIGNEE_RE

            if not isinstance(executor, str) or not _ASSIGNEE_RE.fullmatch(executor):
                raise ValueError("executor_id must be a valid stable assignee")
        if name == "continuity_plan":
            operation = arguments.pop("action")
            parameters = arguments.pop("parameters", {})
            if arguments:
                raise ValueError("unsupported planning arguments")
        elif name == "continuity_collaboration":
            operation = {
                "packet": "packet",
                "changes": "changes",
                "rules": "rule.list",
                "commitments": "commitment.list",
                "tasks": "task.list",
            }.get(arguments.pop("action", None))
            parameters = arguments
            arguments = {}
        else:
            operation = ALIASES[name]
            parameters = arguments
            arguments = {}
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        parameters = dict(parameters)
        private_context = (
            operation.startswith("memory.")
            or operation.startswith("profile.")
            or operation == "controller.start"
            or (operation == "handoff.write" and parameters.get("capture_member"))
        )
        if private_context and not (
            operation in {"profile.show", "controller.start"}
            and parameters.get("public_only") is True
        ):
            if os.environ.get("CONTINUITY_PRIVATE_MEMORY_MCP") != "1":
                raise ValueError(
                    "private memory MCP is disabled; run on the owning host via CLI, or explicitly authorize this local stdio server with CONTINUITY_PRIVATE_MEMORY_MCP=1; never expose it across accounts"
                )
            from .controller_memory import member

            if any(
                parameters.get(k) not in {None, member()}
                for k in ("member", "capture_member")
            ):
                raise ValueError(
                    "private MCP context belongs to the configured local member"
                )
        # Caller identity and target assignment are different. Controllers may
        # create/delegate cards without becoming their executor. Explicit
        # executor_id retains the stricter worker identity contract.
        controller_delegation = (
            operation in {"task.add", "task.update"}
            and os.environ.get("CONTINUITY_ROLE") == "controller"
            and (
                explicit_executor is None
                or explicit_executor == os.environ.get("CONTINUITY_ASSIGNEE")
            )
        )
        from .gitea_cli import is_shared_project

        shared = is_shared_project(data_root, project_id, root)
        if executor and not shared and operation in {"task.add", "task.update"}:
            if parameters.get("actor") not in (None, executor):
                raise ValueError("actor conflicts with stable executor_id")
            parameters["actor"] = executor
        if (
            executor
            and not shared
            and not controller_delegation
            and operation in {"task.add", "task.update", "task.claim", "todo.activate"}
            and not (operation == "task.update" and parameters.get("unassign"))
        ):
            if parameters.get("assignee") not in {None, executor}:
                raise ValueError(
                    "assignee conflicts with stable executor_id; use an explicit audited CLI reassignment"
                )
            parameters["assignee"] = executor
        requested_format = parameters.pop("format", None)
        if requested_format is not None:
            if "format" in original and requested_format != format:
                raise ValueError("conflicting output format")
            format = requested_format
        if "max_output_bytes" in parameters:
            requested_budget = parameters.pop("max_output_bytes")
            render({}, max_output_bytes=requested_budget)
            budget = min(budget, requested_budget)
        render({}, format=format, max_output_bytes=budget)
        actions = {a.dest for a in _parser(operation)._actions}
        if "format" in actions:
            parameters["format"] = "json"
        if "max_output_bytes" in actions:
            parameters["max_output_bytes"] = budget
        value = execute(
            operation, parameters, root=root, project_id=project_id, data_root=data_root
        )
    text = render(value, format=format, max_output_bytes=budget)
    result = {
        "content": [{"type": "text", "text": text}],
        **(
            {"structuredContent": value}
            if format == "json"
            and len(json.dumps(value, ensure_ascii=False).encode()) < budget - 256
            else {}
        ),
    }
    try:
        from .usage_meter import record
        from .collaboration_registry import list_projects

        project_root = original.get("root")
        if not project_root and original.get("project_id"):
            project = next(
                (
                    p
                    for p in list_projects(original.get("data_root"))
                    if p["project_id"] == original["project_id"]
                ),
                None,
            )
            project_root = project["control_root"] if project else None
        if project_root:
            record(
                project_root,
                command="mcp:" + name,
                response_bytes=len(json.dumps(result, ensure_ascii=False).encode()),
                estimated_grep_bytes=0,
                data_root=original.get("data_root"),
                host="mcp",
            )
    except (OSError, ValueError):
        pass
    return result
