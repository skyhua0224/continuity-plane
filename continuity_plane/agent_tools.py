"""First-class MCP tools shaped around what agents actually do.

Before Alpha36 the planning MCP exposed most operations through one
``continuity_plan`` tool with a 117-value ``action`` enum and an untyped
``parameters`` object. Agents could not see which operations existed or what
they needed, so they used the CLI or nothing. This module exposes the
operations a controller or worker uses every hour as separate tools with typed
schemas generated from the CLI parser, descriptions that say when to call
them, and defaults so ``root``/``project_id`` can usually be omitted.

Every tool runs through the same CLI implementation and guards as before
(``planning_api.execute`` or the CLI ``main``), so MCP and CLI cannot drift.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

INSTRUCTIONS = """Continuity keeps shared task cards, worker runs, messages and handoffs for this project so work survives compaction, session changes and hand-offs between agents. Prefer these tools over running `continuity` in a shell.

Start of a session or a new assignment: continuity_start (your card, unread messages, what changed).
Planning work with more than one step, agent or session: continuity_task_add for each piece (body = what to do and how to check it), continuity_task_update as things change, continuity_task_list / continuity_task_show to look.
Sending work to another agent: continuity_worker_dispatch (preview first, then launch=true), then wait outside the model: run `continuity worker wait --project-id P --task-id T` as a background shell task and read continuity_worker_report when it exits. continuity_events_wait checks once and returns; never call it in a loop, every call re-reads your whole context. continuity_worker_report returns a short summary with headings; page with offset.
Working on a card: continuity_progress at milestones, continuity_report_final when done (it attaches the report, marks the card ready for review and tells the dispatcher), continuity_review_request to ask for a review, continuity_inbox to ask a question.
Before context runs out or you hand over: continuity_handoff_write; a new controller reads continuity_handoff_show.
Finding code: continuity_context_lookup (bounded results) before broad grep.
project_id and your identity default from the environment; pass project_id when you work on several projects. Every tool returns `next` with the call that usually follows."""


@dataclass
class AgentTool:
    name: str
    description: str
    operation: str | None = None
    defaults: dict = field(default_factory=dict)
    required: tuple[str, ...] | None = None
    hidden: tuple[str, ...] = ()
    # Parameters shown in the schema. Tool definitions are sent with every
    # model request on many hosts, so only everyday parameters are listed;
    # rarer ones remain reachable through continuity_plan.
    params: tuple[str, ...] | None = None
    read_only: bool = False
    roles: tuple[str, ...] = ("controller", "worker")


_SCOPE_KEYS = {"help", "root", "project_id", "data_root", "format", "max_output_bytes"}

TOOLS: list[AgentTool] = [
    AgentTool(
        "continuity_start",
        "Use at the start of a session, after compaction, or when you receive an assignment: returns your cards, unread messages for you, and recent changes in one call.",
        read_only=True,
    ),
    AgentTool(
        "continuity_task_add",
        "Use when work has more than one step, needs another agent, or must survive this session: creates a task card. Put what to do, scope and how to check it in `body`; assign with `assignee`.",
        "task.add",
        params=("task_id", "title", "body", "assignee", "priority", "lane_id", "mode", "objective", "next_action", "exit_criterion", "dependency", "allowed_file", "worktree", "branch"),
        defaults={
            "lane_id": "main",
            "mode": "patch",
            "priority": "p1",
            "next_action": "Read the card body and start",
            "exit_criterion": [
                "Report what changed, how it was checked, and what was not checked"
            ],
        },
        required=("task_id", "title", "body"),
        hidden=("operation_id", "expected_remote_revision", "pr"),
    ),
    AgentTool(
        "continuity_task_update",
        "Use when a card's status, next step, assignee, branch, commit or body changes. Omit status to keep it.",
        "task.update",
        params=("task_id", "status", "next_action", "assignee", "body", "title", "priority", "blocked_reason", "result", "branch", "commit", "report_file", "dependency", "worktree", "unassign"),
        required=("task_id",),
        hidden=("operation_id", "expected_remote_revision", "batch"),
    ),
    AgentTool(
        "continuity_task_show",
        "Use to read one card in full, including its report when include_report is true.",
        "task.show",
        required=("task_id",),
        read_only=True,
    ),
    AgentTool(
        "continuity_task_list",
        "Use to see cards by status, assignee, lane or keyword. Open cards only unless all=true.",
        "task.list",
        params=("status", "assignee", "lane_id", "query", "all", "dependencies_ready", "max_results", "cursor"),
        defaults={"summary": True},
        read_only=True,
    ),
    AgentTool(
        "continuity_handoff_write",
        "Use before compaction, before you stop, or when you hand the project to another controller: records the plan, next action and open decisions.",
        "handoff.write",
        params=("plan", "next_action", "pending", "batch_id"),
        required=("plan", "next_action"),
        hidden=("capture_member",),
    ),
    AgentTool(
        "continuity_handoff_show",
        "Use when you take over a project or resume after compaction: shows the controller plan, pending decisions and what changed since.",
        "handoff.show",
        read_only=True,
    ),
    AgentTool(
        "continuity_worker_dispatch",
        "Use to send a card to a worker model. By default only previews the prompt and checks (no worker starts); call again with launch=true to start it in the background, then continuity_events_wait.",
        "worker.dispatch",
        params=("task_id", "model", "effort", "assignee", "reassign", "fallback_model", "timeout_seconds", "idle_timeout_seconds", "continuation", "enqueue", "priority"),
        required=("task_id",),
        hidden=(
            "dry_run",
            "background",
            "controller_token",
            "resume_job",
        ),
    ),
    AgentTool(
        "continuity_worker_handoff",
        "Use to move a card to another model (resume cannot change model): the new worker gets the checkout, branch/HEAD, uncommitted changes and last report. Previews unless launch=true.",
        "worker.handoff",
        params=("task_id", "to_model", "continuation", "reason"),
        required=("task_id", "to_model"),
        hidden=("dry_run", "background", "controller_token"),
        roles=("controller",),
    ),
    AgentTool(
        "continuity_worker_jobs",
        "Use to list worker runs that are running or have ended (status=running|ended|failed…).",
        "worker.jobs",
        read_only=True,
    ),
    AgentTool(
        "continuity_worker_report",
        "Use after a worker ends to read its report for a card. Returns the first 2 KB, the headings with their offsets, the total size and next_offset_chars; pass offset to read further, or full=true for everything.",
        "worker.report",
        required=("task_id",),
        read_only=True,
    ),
    AgentTool(
        "continuity_events_wait",
        "Use once to check whether a worker started, finished, failed, was rate-limited or blocked, across all your projects; remembers where you stopped reading and returns immediately when events are waiting. It waits at most 50 seconds, so do not call it in a loop; for a longer wait run `continuity worker wait --project-id P --task-id T` as a background shell task.",
        read_only=True,
        roles=("controller",),
    ),
    AgentTool(
        "continuity_progress",
        "Use at milestones while working on a card: records the next step so a dispatcher or successor can see where you are.",
        roles=("worker",),
    ),
    AgentTool(
        "continuity_report_final",
        "Use when you finish a card: saves your report, records commits and checks, marks the card ready for review and messages the dispatcher. Call this instead of writing report files by hand.",
        roles=("worker",),
    ),
    AgentTool(
        "continuity_review_request",
        "Use to ask for a review of a finished card: creates a review card that points at it, optionally assigned to a reviewer.",
    ),
]
BY_NAME = {tool.name: tool for tool in TOOLS}

# Operations outside the planning allowlist that these tools may run, with argv.
CLI_OPERATIONS = {
    "worker.dispatch": ("dispatch",),
    "worker.handoff": ("worker", "handoff"),
    "worker.jobs": ("worker", "jobs"),
    "worker.report": ("worker", "report"),
}


# --- Schemas ----------------------------------------------------------------


def _parser(operation: str) -> argparse.ArgumentParser:
    from .cli import build_parser
    from .planning_api import OPERATIONS

    argv = CLI_OPERATIONS.get(operation) or OPERATIONS[operation]
    parser = build_parser()
    for part in argv:
        group = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        parser = group.choices[part]
    return parser


def _property(action: argparse.Action) -> dict:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        schema: dict = {"type": "boolean"}
    elif action.type is int:
        schema = {"type": "integer"}
    elif action.type is float:
        schema = {"type": "number"}
    else:
        schema = {"type": "string"}
    if action.choices is not None:
        schema["enum"] = list(action.choices)
    if isinstance(action, argparse._AppendAction):
        schema = {"type": "array", "items": schema}
    if action.help and action.help != argparse.SUPPRESS and len(action.help) <= 90:
        schema["description"] = action.help
    return schema


_COMMON = {
    "project_id": {"type": "string", "description": "Defaults from the environment."},
}
# JSON output matters only where callers post-process results.
_FORMAT_TOOLS = {"continuity_task_show", "continuity_task_list", "continuity_worker_jobs"}

_COMPOSITE_SCHEMAS = {
    "continuity_start": {
        "assignee": {"type": "string", "description": "Your stable name; defaults to CONTINUITY_ASSIGNEE."},
        "task_id": {"type": "string", "description": "Your current card; defaults to CONTINUITY_TASK_ID."},
    },
    "continuity_events_wait": {
        "project_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Projects to watch; defaults to every registered project.",
        },
        "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 50, "default": 30},
        "max_events": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "include_progress": {"type": "boolean", "default": False, "description": "Also return heartbeats."},
        "member": {"type": "string", "description": "Whose read position to use; defaults to CONTINUITY_ASSIGNEE or 'controller'."},
        "restart_from_now": {"type": "boolean", "default": False, "description": "Forget the saved position and only wait for new events."},
    },
    "continuity_progress": {
        "task_id": {"type": "string", "description": "Defaults to CONTINUITY_TASK_ID."},
        "assignee": {"type": "string", "description": "Your stable name when CONTINUITY_ASSIGNEE is not set."},
        "note": {"type": "string", "maxLength": 2000, "description": "Where you are and what comes next."},
    },
    "continuity_report_final": {
        "task_id": {"type": "string", "description": "Defaults to CONTINUITY_TASK_ID."},
        "summary": {"type": "string", "maxLength": 60000, "description": "The report: what changed, why, anything surprising."},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {"type": "string"},
                    "status": {"enum": ["pass", "fail", "not-run"]},
                    "result": {"type": "string"},
                    "required": {"type": "boolean", "default": True},
                },
                "required": ["name", "command", "status", "result"],
            },
            "description": "Checks you ran and their honest result.",
        },
        "unverified": {"type": "array", "items": {"type": "string"}, "description": "What you did not check."},
        "branch": {"type": "string"},
        "commit": {"type": "string", "description": "Final commit SHA for the card."},
        "status": {"enum": ["ready-for-review", "blocked", "completed"], "default": "ready-for-review"},
        "notify": {"type": "string", "description": "Who to message; defaults to CONTINUITY_CONTROLLER. Empty string to skip."},
        "assignee": {"type": "string", "description": "Your stable name when CONTINUITY_ASSIGNEE is not set."},
    },
    "continuity_review_request": {
        "task_id": {"type": "string", "description": "Card to review; defaults to CONTINUITY_TASK_ID."},
        "assignee": {"type": "string", "description": "Your stable name when CONTINUITY_ASSIGNEE is not set."},
        "brief": {"type": "string", "maxLength": 20000, "description": "What to review and what matters most."},
        "reviewer": {"type": "string", "description": "Assignee for the review card."},
        "priority": {"enum": ["p0", "p1", "p2"], "default": "p1"},
    },
}

_COMPOSITE_REQUIRED = {
    "continuity_report_final": ["summary"],
    "continuity_progress": ["note"],
    "continuity_review_request": ["brief"],
}


def definitions() -> list[dict]:
    tools = []
    for tool in TOOLS:
        properties = dict(_COMMON)
        required: list[str] = []
        if tool.operation:
            for action in _parser(tool.operation)._actions:
                if action.dest in _SCOPE_KEYS or action.dest in tool.hidden:
                    continue
                if tool.params is not None and action.dest not in tool.params:
                    continue
                properties[action.dest] = _property(action)
                if action.dest in tool.defaults:
                    properties[action.dest]["default"] = tool.defaults[action.dest]
                elif action.required:
                    required.append(action.dest)
            if tool.operation in {"worker.dispatch", "worker.handoff"}:
                properties["launch"] = {
                    "type": "boolean",
                    "default": False,
                    "description": "false previews only; true starts the worker in the background.",
                }
            if tool.required is not None:
                required = list(tool.required)
        else:
            properties.update(_COMPOSITE_SCHEMAS.get(tool.name, {}))
            required = _COMPOSITE_REQUIRED.get(tool.name, [])
        if tool.name in _FORMAT_TOOLS:
            properties["format"] = {"enum": ["brief", "json"]}
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "annotations": {
                    "readOnlyHint": tool.read_only,
                    "destructiveHint": False,
                    "openWorldHint": tool.name == "continuity_worker_dispatch",
                },
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": required,
                    "properties": properties,
                },
            }
        )
    return tools


# --- Project and identity defaults -----------------------------------------


def _registered(data_root=None) -> list[dict]:
    from .collaboration_registry import list_projects

    return list_projects(data_root)


def resolve_scope(arguments: dict) -> tuple[str | None, str | None]:
    """Fill project_id when the caller omitted both root and project_id."""
    root, project_id = arguments.get("root"), arguments.get("project_id")
    if root or project_id:
        return root, project_id
    env = os.environ.get("CONTINUITY_PROJECT_ID")
    if env:
        return None, env
    from .collaboration_registry import resolve_project

    for candidate in (os.environ.get("PWD"), os.getcwd()):
        if candidate:
            registration = resolve_project(candidate)
            if registration:
                return None, registration["project_id"]
    projects = [p["project_id"] for p in _registered()]
    if len(projects) == 1:
        return None, projects[0]
    raise ValueError(
        "pass project_id; registered projects: " + ", ".join(projects[:20])
        if projects
        else "no project is registered; run continuity collaboration register first"
    )


def _assignee(arguments: dict, key: str = "assignee") -> str | None:
    return arguments.get(key) or os.environ.get("CONTINUITY_ASSIGNEE")


def _task_id(arguments: dict) -> str:
    task_id = arguments.get("task_id") or os.environ.get("CONTINUITY_TASK_ID")
    if not task_id:
        raise ValueError("task_id is required (no CONTINUITY_TASK_ID in this session)")
    return task_id


# --- Execution --------------------------------------------------------------


def _run_cli(operation: str, parameters: dict, project_id: str | None, root: str | None):
    """Run an allowlisted CLI-only operation in-process; returns JSON or text."""
    from .cli import main

    parser = _parser(operation)
    actions = {a.dest: a for a in parser._actions if a.dest != "help"}
    argv = list(CLI_OPERATIONS[operation])
    if project_id and "project_id" in actions:
        argv.append(f"--project-id={project_id}")
    if root and "root" in actions:
        argv.append(f"--root={root}")
    for key, value in parameters.items():
        action = actions.get(key)
        if action is None or value is None:
            raise ValueError(f"unsupported parameter {key} for {operation}")
        flag = next(f for f in action.option_strings if f.startswith("--"))
        if isinstance(action, argparse._StoreTrueAction):
            if value is True:
                argv.append(flag)
        elif isinstance(value, list):
            argv += [f"{flag}={v}" for v in value]
        else:
            argv.append(f"{flag}={value}")
    output, error = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        try:
            code = main(argv)
        except SystemExit as exc:
            raise ValueError(error.getvalue().strip() or f"invalid parameters for {operation}") from exc
    text = output.getvalue().strip()
    if code not in {0, 2}:
        raise ValueError(error.getvalue().strip()[:2000] or f"{operation} failed with code {code}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _plan(
    operation: str,
    parameters: dict,
    project_id: str | None,
    root: str | None,
    *,
    identity: bool = True,
):
    from .planning_api import apply_identity, execute

    parameters = dict(parameters)
    if identity:
        apply_identity(
            operation,
            parameters,
            None,
            os.environ.get("CONTINUITY_ASSIGNEE"),
            None,
            project_id,
            root,
        )
    return execute(operation, parameters, root=root, project_id=project_id)


def _next(tool: str, value, project_id: str | None, arguments: dict) -> list[str]:
    pid = f"project_id={project_id!r}, " if project_id else ""
    task = arguments.get("task_id")
    hints = {
        "continuity_task_add": [f"continuity_worker_dispatch({pid}task_id={task!r})"],
        "continuity_worker_dispatch": (
            [f"continuity_events_wait({pid.rstrip(', ')})"]
            if arguments.get("launch")
            else [f"continuity_worker_dispatch({pid}task_id={task!r}, launch=true)"]
        ),
        "continuity_worker_jobs": [f"continuity_worker_report({pid}task_id=…)"],
        "continuity_worker_report": [
            f"continuity_task_update({pid}task_id={task!r}, status='completed'|'merged'|'blocked')",
            f"continuity_review_request({pid}task_id={task!r}, brief=…)",
        ],
        "continuity_handoff_show": ["continuity_task_list(status='active')"],
        "continuity_progress": ["continuity_report_final(summary=…) when done"],
        "continuity_report_final": ["stop, or continuity_start for the next card"],
    }
    return hints.get(tool, [])


def _configured_models() -> list[str]:
    from .collaboration_registry import default_data_root

    path = default_data_root() / "executor-adapters.json"
    try:
        return sorted(json.loads(path.read_text()))
    except (OSError, ValueError):
        return []


def _check_dispatch_model(parameters: dict, project_id: str | None) -> None:
    """Fail with the choices instead of a bare CLI error the agent cannot act on."""
    models = _configured_models()
    model = parameters.get("model")
    recommend = (
        f"continuity_plan(action='recommend', project_id={project_id!r}, "
        "parameters={'scenario': 'quick-triage'})"
    )
    if not models:
        raise ValueError(
            "no worker models are configured on this host; a person must run "
            "`continuity worker configure --model NAME --provider codex|claude …` once"
        )
    if not model:
        raise ValueError(
            "choose model: one of " + ", ".join(models) + f"; or ask {recommend}"
        )
    if model not in models:
        raise ValueError(
            f"model {model!r} is not configured; choose one of " + ", ".join(models)
        )


def handle(name: str, arguments: dict) -> dict:
    """Run one first-class tool; returns a JSON-serialisable value."""
    tool = BY_NAME[name]
    arguments = dict(arguments)
    arguments.pop("format", None)
    if name == "continuity_events_wait":
        # Watching spans projects; only an explicit project narrows it.
        root, project_id = arguments.get("root"), arguments.get("project_id")
    else:
        root, project_id = resolve_scope(arguments)
    arguments.pop("root", None)
    arguments.pop("project_id", None)
    if tool.operation:
        parameters = {**tool.defaults, **arguments}
        if tool.operation == "task.add" and not str(parameters.get("body") or "").strip():
            raise ValueError("body is required: say what to do, the scope, and how to check it")
        if tool.operation == "task.add":
            parameters.setdefault("objective", parameters.get("title"))
            if os.environ.get("CONTINUITY_ASSIGNEE"):
                parameters.setdefault("actor", os.environ["CONTINUITY_ASSIGNEE"])
        if tool.operation in {"worker.dispatch", "worker.handoff"}:
            if tool.operation == "worker.dispatch":
                _check_dispatch_model(parameters, project_id)
            launch = parameters.pop("launch", False)
            parameters["background" if launch else "dry_run"] = True
            if os.environ.get("CONTINUITY_CONTROLLER_TOKEN"):
                parameters.setdefault("controller_token", os.environ["CONTINUITY_CONTROLLER_TOKEN"])
        if tool.operation in CLI_OPERATIONS:
            value = _run_cli(tool.operation, parameters, project_id, root)
        else:
            value = _plan(tool.operation, parameters, project_id, root)
    else:
        value = globals()["_" + name.removeprefix("continuity_")](
            arguments, project_id, root
        )
    if tool.operation == "worker.report" and isinstance(value, dict):
        # Surface final/job_status as fields so a caller need not parse the header.
        header = str(value.get("text", "")).split("\n", 1)[0]
        state = dict(re.findall(r"\b(final|job_status)=(\S+)", header))
        if "final" in state:
            value = {
                "final": state["final"] == "true",
                "job_status": None if state["job_status"] == "none" else state["job_status"],
                **value,
            }
    hints = _next(name, value, project_id, arguments)
    if isinstance(value, dict):
        value = {**value, **({"next": hints} if hints else {})}
    else:
        value = {"result": value, **({"next": hints} if hints else {})}
    return value


# --- Composite tools ---------------------------------------------------------


def _start(arguments: dict, project_id, root):
    assignee = _assignee(arguments)
    result: dict = {"project_id": project_id, "assignee": assignee}
    params = {"assignee": assignee} if assignee else {}
    result["packet"] = _plan("packet", params, project_id, root)
    if assignee:
        result["inbox"] = _plan(
            "inbox.list", {"recipient": assignee}, project_id, root
        )
    task_id = arguments.get("task_id") or os.environ.get("CONTINUITY_TASK_ID")
    if task_id:
        result["task"] = _plan("task.show", {"task_id": task_id}, project_id, root)
    role = os.environ.get("CONTINUITY_ROLE") or ("worker" if task_id else "controller")
    result["role"] = role
    result["next"] = (
        ["work on the card; continuity_progress at milestones; continuity_report_final when done"]
        if role == "worker"
        else ["continuity_handoff_show()", "continuity_task_list(status='active')", "continuity_events_wait()"]
    )
    return result


def _progress(arguments: dict, project_id, root):
    task_id = _task_id(arguments)
    note = str(arguments.get("note") or "").strip()
    if not note:
        raise ValueError("note is required")
    return _plan(
        "task.update",
        {"task_id": task_id, "next_action": note[:2000]},
        project_id,
        root,
    )


def _report_dir() -> Path:
    from .collaboration_registry import default_data_root

    base = os.environ.get("CONTINUITY_WORK_ROOT")
    return (Path(base) if base else default_data_root()) / "agent-reports"


def _report_final(arguments: dict, project_id, root):
    task_id = _task_id(arguments)
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required")
    checks = arguments.get("checks") or []
    unverified = arguments.get("unverified") or []
    lines = [f"# Report: {task_id}", "", summary, ""]
    if checks:
        lines += ["## Checks", ""]
        lines += [
            f"- {c.get('status', 'not-run')}: {c.get('name')} — `{c.get('command')}` → {c.get('result')}"
            for c in checks
        ]
        lines.append("")
    if unverified:
        lines += ["## Not verified", ""] + [f"- {item}" for item in unverified] + [""]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_id)
    path = _report_dir() / (project_id or "project") / f"{safe}-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    update: dict = {
        "task_id": task_id,
        "status": arguments.get("status") or "ready-for-review",
        "report_file": str(path),
    }
    for key in ("branch", "commit"):
        if arguments.get(key):
            update[key] = arguments[key]
    actor = os.environ.get("CONTINUITY_ASSIGNEE") or arguments.get("assignee")
    if checks and arguments.get("commit") and actor:
        update["verification_json"] = json.dumps(
            {
                "commit": arguments["commit"],
                "actor": actor,
                "checks": [
                    {
                        "name": c.get("name"),
                        "command": c.get("command"),
                        "status": c.get("status"),
                        "result": c.get("result"),
                        "required": c.get("required", True),
                    }
                    for c in checks
                ],
                "unverified": unverified,
            },
            ensure_ascii=False,
        )
    result = {"report_file": str(path), "card": _plan("task.update", update, project_id, root)}
    notify = arguments.get("notify")
    if notify is None:
        notify = os.environ.get("CONTINUITY_CONTROLLER")
    if notify:
        first = summary.splitlines()[0][:300]
        result["notified"] = _plan(
            "inbox.send",
            {
                "sender": actor or "worker",
                "recipient": notify,
                "task_id": task_id,
                "body": f"{task_id} → {update['status']}: {first}",
            },
            project_id,
            root,
        )
    return result


def _review_request(arguments: dict, project_id, root):
    task_id = _task_id(arguments)
    brief = str(arguments.get("brief") or "").strip()
    if not brief:
        raise ValueError("brief is required")
    review_id = f"review-{task_id}-{datetime.now(timezone.utc):%m%d%H%M%S}"
    params = {
        "task_id": review_id,
        "lane_id": "review",
        "mode": "scout",
        "priority": arguments.get("priority") or "p1",
        "title": f"Review {task_id}",
        "objective": f"Review card {task_id}",
        "next_action": f"Read card {task_id} and its report, then review",
        "exit_criterion": [
            "Verdict (accept / changes needed) with findings ranked by severity"
        ],
        "body": f"Review card `{task_id}` in this project.\n\n{brief}",
    }
    if arguments.get("reviewer"):
        params["assignee"] = arguments["reviewer"]
    if os.environ.get("CONTINUITY_ASSIGNEE") or arguments.get("assignee"):
        params["actor"] = os.environ.get("CONTINUITY_ASSIGNEE") or arguments["assignee"]
    # A review card is meant for someone else, so the worker identity guard
    # (cards assigned to the caller only) does not apply; actor keeps the audit.
    return {
        "review_task_id": review_id,
        "card": _plan("task.add", params, project_id, root, identity=False),
    }


# --- Waiting -----------------------------------------------------------------

_TERMINAL_KINDS = {
    "completed",
    "failed",
    "interrupted",
    "timeout",
    "rate-limited",
    "blocked",
    "finished",
    "started",
}


def _cursor_path(member: str) -> Path:
    from .collaboration_registry import default_data_root

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", member)[:128]
    return default_data_root() / "agent-cursors" / f"{safe}.json"


def _high_water(project_id: str) -> int:
    from .collaboration_registry import default_data_root

    path = default_data_root() / "projects" / project_id / "changes.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 65536))
            lines = stream.read().splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            return int(json.loads(line)["cursor"])
        except (ValueError, KeyError, TypeError):
            continue
    return 0


def _events_wait(arguments: dict, project_id, root):
    from .collaboration_stream import worker_events

    if project_id is None and root:
        from .collaboration_registry import resolve_project

        project_id = (resolve_project(root) or {}).get("project_id")
    projects = arguments.get("project_ids") or (
        [project_id] if project_id else [p["project_id"] for p in _registered()]
    )
    unknown = sorted(set(projects) - {p["project_id"] for p in _registered()})
    if unknown:
        raise ValueError("unknown project_ids: " + ", ".join(unknown))
    member = arguments.get("member") or os.environ.get("CONTINUITY_ASSIGNEE") or "controller"
    timeout = min(max(int(arguments.get("timeout_seconds", 30)), 0), 50)
    limit = min(max(int(arguments.get("max_events", 20)), 1), 50)
    include_progress = bool(arguments.get("include_progress"))
    path = _cursor_path(member)
    try:
        saved = json.loads(path.read_text()) if not arguments.get("restart_from_now") else {}
    except (OSError, ValueError):
        saved = {}
    cursors = {p: int(saved[p]) if p in saved else _high_water(p) for p in projects}
    deadline = time.monotonic() + timeout
    events: list[dict] = []
    while True:
        for event in worker_events(
            None,
            project_ids=projects,
            since_cursors=dict(cursors),
            follow=False,
            poll_seconds=1.0,
            max_events=None,
        ):
            cursors[event["project_id"]] = max(
                cursors[event["project_id"]], int(event["cursor"])
            )
            if event["kind"] == "progress" and not include_progress:
                continue
            event.pop("stream_cursor", None)
            event.pop("schema_version", None)
            events.append(event)
            if len(events) >= limit:
                break
        if events or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    for p in projects:
        cursors[p] = max(cursors[p], int(saved.get(p, 0)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**saved, **cursors}))
    hints = []
    for event in events:
        if event["kind"] in {"completed", "failed", "interrupted", "timeout", "finished"}:
            hints.append(
                f"continuity_worker_report(project_id={event['project_id']!r}, task_id={event.get('task_id')!r})"
            )
    return {
        "member": member,
        "events": events,
        "timed_out": not events,
        "watching": projects,
        "next": hints or ["continuity_events_wait() again, or continue other work"],
    }


def result(name: str, arguments: dict) -> dict:
    """MCP tools/call result: brief text by default, structured JSON on request."""
    from .planning_api import render

    format = arguments.get("format") or "brief"
    value = handle(name, arguments)
    budget = 65536 if name in {"continuity_task_show", "continuity_worker_report"} else 16384
    text = render(value, format=format, max_output_bytes=budget)
    out = {"content": [{"type": "text", "text": text}]}
    if format == "json" and len(json.dumps(value, ensure_ascii=False).encode()) < budget - 256:
        out["structuredContent"] = value
    return out
