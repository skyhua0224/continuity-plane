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


_TASK_MARK = "----- Task -----"

_HOW_TO_WORK = "How to work with Continuity (MCP tools from the `continuity` server).\n1. Start: call continuity_start once — it returns this card, unread messages and changes. Then verify integration_base and the observed checkout before editing.\n2. While working: continuity_progress(note=…) at each milestone; continuity_context_lookup(query=…) to find code before broad grep; continuity_inbox(action='send', recipient=<dispatcher>, body=…) for a question that blocks you.\n3. Finish: continuity_report_final(summary=…, checks=[{name, command, status, result}], unverified=[…], branch=…, commit=…). It saves the report, marks the card ready-for-review and tells your dispatcher; do not write report files or edit cards by hand.\n4. Review: continuity_review_request(brief=…) if the card asks for one.\nIf the MCP tools are not available in this session, use the CLI equivalents once (`continuity collaboration packet`, `continuity collaboration task update --status ready-for-review --report-file …`, `continuity collaboration inbox send …`) and mention in your report that MCP was unavailable.\nA queued card is not a running worker."

_PERSISTENCE = "Keep recoverable work, reports, thread IDs and checkpoints in the persistent CONTINUITY_WORK_ROOT or Continuity data root, not temporary or memory-backed mounts. Only disposable reproducible buffers belong in temp directories. Authentication, account queries and native session resume stay on the owning host/profile; share authorized task artifacts or normalized observations, never migrate private profile/session state to query elsewhere."

_REVIEWER = "Independent repository review: identify correctness, authorization and error-handling failures using the declared code and authorized test fixtures. Preserve accurate technical facts and state any uncertainty or missing authorization; do not conceal the nature of the requested work."


def _rule_lines(rules):
    ordered = sorted(
        rules,
        key=lambda rule: (
            rule["rule_id"],
            rule.get("scope") or "",
            rule.get("audience") or "",
        ),
    )
    return "\n".join(
        f"[{rule['audience']}] {rule['rule_id']}: {rule['text']}" for rule in ordered
    )


def _stable_block(rules_text, audience):
    """Audience prefix. No card ids, paths, timestamps or digests."""
    parts = ["Project rules:\n" + rules_text if rules_text else "Project rules:"]
    parts.append(_HOW_TO_WORK)
    parts.append(_PERSISTENCE)
    if audience == "reviewer":
        parts.append(_REVIEWER)
    return "\n\n".join(parts)


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
    ledger=None,
):
    from .delivery_records import scope_hash

    data = Path(data_root or default_data_root())
    if ledger is None:
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
    # Lane rules name one lane, so they stay behind the separator.
    # The leading block is then the same for every card of this audience.
    shared_rules = [
        rule
        for rule in rules
        if not str(rule.get("audience", "")).startswith("lane:")
    ]
    lane_rules = [
        rule
        for rule in rules
        if str(rule.get("audience", "")).startswith("lane:")
    ]
    rules_text = _rule_lines(shared_rules)
    lane_text = _rule_lines(lane_rules)
    digest = rule_bundle["rules_digest"]
    stable = _stable_block(rules_text, audience)
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
        "dependencies": task.get("dependencies", []),
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
        from .controller_memory import effective
        from .project_context import integration

        project_profile = effective(
            registration["control_root"],
            data,
            project_id,
            "controller",
            include_private=False,
        )
        configured_base = project_profile["values"].get("integration_base")
        if configured_base and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_./-]{0,255}", str(configured_base)
        ):
            raise ValueError(
                "project integration_base is not a valid Git branch or ref name"
            )
        selected_integration = integration(
            data, project_id, registration["control_root"]
        )
        base_ref = (
            configured_base
            if configured_base and str(configured_base).startswith("refs/")
            else "refs/heads/" + str(configured_base)
            if configured_base
            else selected_integration.get("ref")
        )
        instructions["integration_base"] = {
            "ref": base_ref,
            "head": (
                selected_integration.get("head")
                if base_ref == selected_integration.get("ref")
                else None
            ),
            "source": (
                ".continuity/controller-profile.json:integration_base"
                if configured_base
                else "registered project integration context"
            ),
            "authority": "default implementation starting base; not merge, push, deploy, or ownership authorization",
        }
    from .vocabulary_memory import resolve as resolve_vocabulary

    vocabulary_query = " ".join((body + " " + task["objective"]).split())
    vocabulary_query = vocabulary_query.encode()[:16384].decode("utf-8", "ignore")
    vocabulary = resolve_vocabulary(data, vocabulary_query, project_id=project_id)
    if vocabulary:
        instructions["relevant_vocabulary"] = [
            {k: r.get(k) for k in ("entity_id", "statement", "scope", "confidence")}
            for r in vocabulary[:6]
        ]
    task_parts = []
    if lane_text:
        task_parts.append("Lane rules:\n" + lane_text)
    if known_rules_digest == digest:
        task_parts.append(f"Previously received rules unchanged: {digest}")
    task_parts.append(
        "Assigned work (metadata does not authorize merge/deploy):\n"
        + json.dumps(instructions, ensure_ascii=False, sort_keys=True)
    )
    ids = f"project_id={project_id!r}, task_id={task_id!r}"
    if assignee:
        ids += f", assignee={assignee!r}"
    task_parts.append(f"Pass {ids} if a tool asks for them.")
    if audience != "reviewer" and task["mode"] in {"scout", "review"}:
        task_parts.append(_REVIEWER)
    text = stable + "\n\n" + _TASK_MARK + "\n\n" + "\n\n".join(task_parts)
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
            if value.get("commit") is not None:
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

    requested_output_bytes = args.max_output_bytes
    effective_output_bytes = (
        min(requested_output_bytes, 131072)
        if args.workflow_action == "batch-list"
        else requested_output_bytes
    )
    render({}, format=args.format, max_output_bytes=effective_output_bytes)
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
        items = sorted(
            ledger.get("workflow_records", {}).get(kind, {}).values(),
            key=lambda item: (item.get("recorded_at", ""), item.get("id", "")),
        )
        if kind == "batch":
            from .metadata_pages import records_page

            value = records_page(
                args.data_root,
                args.project_id,
                kind,
                items,
                budget=effective_output_bytes,
                cursor=args.cursor,
            )
            if requested_output_bytes > effective_output_bytes:
                value["requested_max_output_bytes"] = requested_output_bytes
                value["budget_capped_to"] = effective_output_bytes
        else:
            value = {"items": items}
    print(render(value, format=args.format, max_output_bytes=effective_output_bytes))
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
        if name == "batch-list":
            parser.add_argument(
                "--cursor", help="Continue an immutable batch-list snapshot"
            )
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
