"""Metadata-only prompt assembly and integration/decision records."""

import hashlib
import json
import re
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from .collaboration_registry import (
    _load_ledger,
    _ledger_path,
    _locked,
    _atomic_write,
    _now,
    default_data_root,
    _record_change,
)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
    ):
        raise ValueError("invalid record identifier")
    return value


def _commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{7,64}", value):
        raise ValueError("commit must be a Git object identifier")
    return value


def prompt(
    data_root,
    *,
    project_id,
    task_id,
    known_rules_digest=None,
    assignee=None,
    audience="worker",
    expected_revision=None,
    controller=None,
):
    from .delivery_records import scope_hash

    data = Path(data_root or default_data_root())
    ledger = _load_ledger(data, project_id)
    task = next((t for t in ledger["tasks"] if t["task_id"] == task_id), None)
    if not task:
        raise ValueError("task_id does not exist")
    if assignee and task["assignee"] not in {None, assignee}:
        raise ValueError("task is assigned to a different executor")
    if expected_revision and task["updated_at"] != expected_revision:
        raise ValueError(
            "task changed since the requested revision; rebuild the prompt"
        )
    body = ledger.get("task_details", {}).get(task_id, {}).get("body")
    if not body or not body.strip():
        raise ValueError(
            "task body is empty; set task update --body before assembling a worker prompt"
        )
    from .rules_delivery import bundle

    rule_bundle = bundle(
        data, project_id=project_id, audience=audience, lane_id=task["lane_id"]
    )
    rules = rule_bundle["rules"]
    # All rules are supplied to a new worker. Audience labels remain visible.
    rules_text = "\n".join(
        f"[{r['audience']}] {r['rule_id']}: {r['text']}"
        for r in sorted(rules, key=lambda r: r["rule_id"])
    )
    digest = rule_bundle["rules_digest"]
    prefix = (
        f"Previously received rules unchanged: {digest}"
        if known_rules_digest == digest
        else "Project rules:\n" + rules_text
    )
    instructions = {
        "task_id": task_id,
        "assignee": assignee or task["assignee"],
        "controller": controller
        or os.environ.get("CONTINUITY_CONTROLLER")
        or os.environ.get("CONTINUITY_ASSIGNEE")
        or os.environ.get("CODEX_THREAD_ID")
        or "operator",
        "worktree": task["worktree"],
        "allowed_files": task["allowed_files"],
        "exit_criteria": task["exit_criteria"],
        "branch": task["branch"],
        "commit": task["commit"],
        "body": body,
        "continuity_cli": str(Path(sys.executable).parent / "continuity"),
        "policy_attachments": [
            {
                "rule_id": r["rule_id"],
                "scope": r.get("scope", "project"),
                **r["attachment"],
            }
            for r in rules
            if r.get("attachment")
        ],
    }
    if task.get("worktree"):
        from .task_shortcuts import infer_git
        import subprocess

        try:
            instructions["observed_checkout"] = infer_git(task["worktree"])
        except (OSError, subprocess.TimeoutExpired):
            instructions["observed_checkout"] = {"status": "not-observed"}
        instructions["checkout_note"] = (
            "Observed Git state is current; recorded commits may be earlier evidence. Honor explicit baselines and verify before switching branches."
        )
    from .collaboration_registry import list_projects

    registration = next(
        (p for p in list_projects(data) if p["project_id"] == project_id), None
    )
    if task.get("worktree") or registration:
        from .prompt_context import code_context

        target_exists = bool(task.get("worktree") and Path(task["worktree"]).is_dir())
        code_root = (
            task["worktree"]
            if target_exists
            else (registration or {}).get("control_root", task.get("worktree"))
        )
        instructions["code_context"] = code_context(
            code_root, body, task["allowed_files"]
        )
        instructions["code_context"]["source_basis"] = (
            "task-checkout"
            if target_exists
            else "registered-project-root; verify against the worker checkout before editing"
        )
    if registration:
        from .reference_library import hint

        preference = hint(registration["control_root"], data_root=data)
        if preference:
            instructions["source_preference"] = preference
    from .vocabulary_memory import resolve as resolve_vocabulary

    vocabulary_query = " ".join((body + " " + task["objective"]).split())
    vocabulary_query = vocabulary_query.encode()[:16384].decode("utf-8", "ignore")
    vocabulary = resolve_vocabulary(data, vocabulary_query, project_id=project_id)
    if vocabulary:
        instructions["relevant_vocabulary"] = [
            {k: r.get(k) for k in ("entity_id", "statement", "scope", "confidence")}
            for r in vocabulary[:6]
        ]
    text = (
        prefix
        + "\n\nAssigned work (metadata does not authorize merge/deploy):\n"
        + json.dumps(instructions, ensure_ascii=False, sort_keys=True)
    )
    text += "\nStart: read your packet and inbox. Finish: attach report and commit-bound verification, mark ready-for-review, and send an inbox message to your dispatcher. A queued card is not a running worker."
    text += "\nKeep recoverable work, reports, thread IDs and checkpoints in the persistent CONTINUITY_WORK_ROOT or Continuity data root, not temporary or memory-backed mounts. Only disposable reproducible buffers belong in temp directories. Authentication, account queries and native session resume stay on the owning host/profile; share authorized task artifacts or normalized observations, never migrate private profile/session state to query elsewhere."
    if audience == "reviewer" or task["mode"] == "scout":
        text += "\nIndependent repository review: identify correctness, authorization and error-handling failures using the declared code and authorized test fixtures. Preserve accurate technical facts and state any uncertainty or missing authorization; do not conceal the nature of the requested work."
    return dict(
        task_id=task_id,
        prompt=text,
        prompt_sha256=hashlib.sha256(text.encode()).hexdigest(),
        rules_digest=digest,
        rules_omitted=known_rules_digest == digest,
        task_revision=task["updated_at"],
        scope_sha256=scope_hash(task, body),
        rules_audience=audience,
    )


def _record_ledger(
    data_root,
    *,
    project_id,
    kind,
    record_id,
    value,
    controller=None,
    controller_token=None,
):
    if kind not in {"decision", "batch"}:
        raise ValueError("invalid workflow record kind")
    _identifier(record_id)
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 32768:
        raise ValueError("record must be a JSON object within 32768 bytes")
    if kind == "batch" and "metrics" in value:
        metrics = value["metrics"]
        if not isinstance(metrics, dict) or any(
            not isinstance(key, str) or not key.strip() or len(key) > 128
            for key in metrics
        ):
            raise ValueError(
                "batch metrics must be an object with nonempty names of at most 128 characters"
            )
        try:
            json.dumps(metrics, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("batch metrics must contain finite JSON values") from exc
    data = Path(data_root or default_data_root())
    path = _ledger_path(data, project_id)
    with _locked(path):
        ledger = _load_ledger(data, project_id)
        if kind == "batch":
            from .controller_lease import _require
            from .project_context import _path, _load

            # The caller holds the context lock before the ledger lock. Check
            # again after waiting: expiry itself can invalidate the holder.
            _require(
                _load(_path(data, project_id)),
                controller,
                controller_token,
                required=ledger.get("controller_fencing_required", False),
            )
        records = ledger.setdefault("workflow_records", {}).setdefault(kind, {})
        if record_id in records:
            if records[record_id]["value"] == value:
                return records[record_id], False
            raise ValueError(
                "record id already exists with different content; create a correction record"
            )
        if kind == "decision":
            if any(
                not isinstance(value.get(k), str) or not value[k].strip()
                for k in ("actor", "source", "text")
            ):
                raise ValueError(
                    "decision requires actor, source locator and text; approval must refer to actual user authorization"
                )
            if type(value.get("approved", False)) is not bool:
                raise ValueError("approved must be boolean")
            if value.get("approved"):
                _commit(value.get("commit"))
        else:
            _commit(value.get("baseline"))
            _commit(value.get("result_commit"))
            tasks = value.get("task_ids")
            if (
                not isinstance(tasks, list)
                or not tasks
                or any(not isinstance(t, str) for t in tasks)
            ):
                raise ValueError("batch requires task_ids")
            known = {t["task_id"] for t in ledger["tasks"]}
            if not set(tasks) <= known:
                raise ValueError("batch contains unknown task ids")
            if not isinstance(value.get("verification"), dict):
                raise ValueError("batch requires a commit-bound verification record")
            from .delivery_records import verification_record

            verification_record(value["verification"], value["result_commit"])
            exceptions = value.get("exception_decisions", [])
            if not isinstance(exceptions, list):
                raise ValueError("exception_decisions must be a list")
            for exception in exceptions:
                decision = (
                    ledger.get("workflow_records", {})
                    .get("decision", {})
                    .get(exception, {})
                    .get("value", {})
                )
                if (
                    not decision.get("approved")
                    or decision.get("commit") != value["result_commit"]
                ):
                    raise ValueError(
                        "exception requires an explicit approved decision bound to the batch result commit"
                    )
        row = {
            "id": record_id,
            "kind": kind,
            "value": value,
            "recorded_at": _now(),
            "authority": "declared-metadata-not-execution-permission",
        }
        records[record_id] = row
        _atomic_write(path, ledger)
    return row, True


def record(
    data_root,
    *,
    project_id,
    kind,
    record_id,
    value,
    controller=None,
    controller_token=None,
):
    from .controller_lease import require
    from .project_context import _path

    data = Path(data_root or default_data_root())
    # acquire() uses this same context -> ledger order. A fence cannot rotate
    # between validation and the authoritative batch write.
    with _locked(_path(data, project_id)) if kind == "batch" else nullcontext():
        if kind == "batch":
            require(data, project_id, controller, controller_token)
        row, created = _record_ledger(
            data,
            project_id=project_id,
            kind=kind,
            record_id=record_id,
            value=value,
            controller=controller,
            controller_token=controller_token,
        )
    if created:
        _record_change(data, project_id, kind + ".recorded", None, record_id=record_id)
    # Retry this projection even for an identical record after a prior I/O or
    # lease failure. Never acquire the context lock recursively.
    if kind == "batch":
        from .project_context import handoff
        from .collaboration_registry import list_projects

        project = next(
            (p for p in list_projects(data) if p["project_id"] == project_id), None
        )
        if project:
            handoff(
                project["control_root"],
                data,
                project_id,
                completed_batch=record_id,
                controller=controller,
                controller_token=controller_token,
            )
    return row


def command(args):
    from .planning_api import render

    render({}, format=args.format, max_output_bytes=args.max_output_bytes)
    if args.workflow_action == "prompt":
        value = prompt(
            args.data_root,
            project_id=args.project_id,
            task_id=args.task_id,
            known_rules_digest=args.known_rules_digest,
            assignee=args.assignee,
            audience=args.audience,
            expected_revision=args.expected_revision,
            controller=args.controller,
        )
        if (
            len(json.dumps(value, ensure_ascii=False).encode()) + 1
            > args.max_output_bytes
        ):
            raise ValueError(
                "assembled prompt exceeds budget; increase --max-output-bytes or shorten task/rules (prompt was not truncated)"
            )
    elif args.workflow_action.endswith("-add"):
        record_value = json.loads(args.record_json)
        if args.workflow_action == "batch-add" and args.metrics_json is not None:
            metrics = json.loads(args.metrics_json)
            if not isinstance(record_value, dict):
                raise ValueError("record must be a JSON object")
            if "metrics" in record_value and record_value["metrics"] != metrics:
                raise ValueError(
                    "conflicting metrics in --record-json and --metrics-json"
                )
            record_value = {**record_value, "metrics": metrics}
        value = record(
            args.data_root,
            project_id=args.project_id,
            kind=args.workflow_action.split("-")[0],
            record_id=args.record_id,
            value=record_value,
            controller=getattr(args, "controller", None),
            controller_token=getattr(args, "controller_token", None),
        )
    else:
        kind = args.workflow_action.split("-")[0]
        ledger = _load_ledger(
            Path(args.data_root or default_data_root()), args.project_id
        )
        value = {
            "items": list(ledger.get("workflow_records", {}).get(kind, {}).values())
        }
    print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    for name in ("prompt", "batch-add", "batch-list", "decision-add", "decision-list"):
        parser = commands.add_parser(
            name,
            help="Assemble task instructions or record integration evidence; never execute external effects",
        )
        parser.set_defaults(handler=command, workflow_action=name)
        parser.add_argument("--root", default=".")
        parser.add_argument("--project-id")
        parser.add_argument("--data-root")
        parser.add_argument("--format", choices=("brief", "json"), default="json")
        parser.add_argument("--max-output-bytes", type=int, default=32768)
        if name == "prompt":
            parser.add_argument("--task-id", required=True)
            parser.add_argument("--assignee")
            parser.add_argument("--audience", default="worker")
            parser.add_argument("--expected-revision")
            parser.add_argument(
                "--controller", help="Dispatcher identity for worker replies"
            )
            parser.add_argument(
                "--known-rules-digest",
                help="Only supply when this worker already received the full rules",
            )
        elif name.endswith("-add"):
            parser.add_argument("--record-id", required=True)
            if name == "batch-add":
                parser.add_argument("--controller")
                parser.add_argument("--controller-token")
                parser.add_argument(
                    "--metrics-json",
                    help='Optional metrics object, e.g. {"runtime_links_module":{"before":13,"after":6}}; baseline remains a commit',
                )
            parser.add_argument(
                "--record-json",
                required=True,
                help="Decision: actor/source/text/approved/commit; batch: task_ids/baseline/result_commit/verification/exception_decisions/optional metrics",
            )
