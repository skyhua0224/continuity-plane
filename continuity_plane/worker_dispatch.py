"""Explicit provider adapters around prompt assembly, supervisor, session and report capture."""

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import hashlib
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    project_data_dir,
    resolve_project,
    list_tasks,
)


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        from .persistent_storage import sync_directory

        sync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Capture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.pending = b""
        self.log_bytes = 0
        self.log_truncated = False
        self.terminal_failure = None
        self.policy_seen = False
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "output.jsonl").open("ab")

    def __call__(self, chunk):
        if self.log_bytes + len(chunk) <= 16 * 1024 * 1024:
            self.log.write(chunk)
            self.log.flush()
            self.log_bytes += len(chunk)
        else:
            self.log_truncated = True
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if len(line) > 2097152:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "turn.failed" or (
                row.get("type") == "result" and row.get("is_error")
            ):
                error = str(
                    row.get("error") or row.get("result") or "provider-reported-failure"
                )
                from .retry_policy import policy_refusal

                self.policy_seen |= policy_refusal(error)
                self.terminal_failure = error[:8192]
            elif row.get("type") == "turn.completed" or (
                row.get("type") == "result" and not row.get("is_error")
            ):
                self.terminal_failure = None
            thread = (
                row.get("thread_id")
                if row.get("type") == "thread.started"
                else row.get("session_id")
            )
            if isinstance(thread, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", thread
            ):
                _write(self.directory / "thread", thread)
            report = None
            if (
                row.get("type") == "item.completed"
                and isinstance(row.get("item"), dict)
                and row["item"].get("type") == "agent_message"
            ):
                report = row["item"].get("text")
            if row.get("type") == "result" and isinstance(row.get("result"), str):
                report = row["result"]
            if isinstance(report, str) and 0 < len(report.encode()) <= 65536:
                _write(self.directory / "report.md", report)
        if len(self.pending) > 2097152:
            self.pending = b""

    def close(self):
        try:
            if self.pending:
                self(b"\n")
        except Exception:
            pass
        finally:
            try:
                self.log.close()
            except OSError:
                pass


def configure(
    data_root,
    *,
    model,
    provider,
    provider_model=None,
    executable=None,
    argv=None,
    resume_argv=None,
    profile_provider=None,
    official_subscription=False,
):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", model) or provider not in {
        "codex",
        "claude",
        "custom",
    }:
        raise ValueError("invalid model/provider adapter")
    if provider == "custom" and (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(v, str) and v for v in argv)
    ):
        raise ValueError("custom adapter needs a nonempty argv array")
    if profile_provider is not None and profile_provider not in {
        "codex",
        "claude",
        "custom",
    }:
        raise ValueError("profile-provider must identify a local client namespace")
    if provider != "custom" and profile_provider not in (None, provider):
        raise ValueError("native adapter profile must match its client provider")
    if profile_provider is None:
        name = Path(argv[0]).name if argv else ""
        profile_provider = (
            provider
            if provider != "custom"
            else name
            if name in {"codex", "claude"}
            else "custom"
        )
    path = Path(data_root or default_data_root()) / "executor-adapters.json"
    value = dict(
        model=model,
        provider=provider,
        provider_model=provider_model or model,
        executable=executable or ("codex" if provider == "codex" else "claude"),
        argv=argv,
        resume_argv=resume_argv,
        profile_provider=profile_provider,
        official_subscription=bool(official_subscription),
    )
    from .runtime_origin import current
    from .vocabulary_memory import _SECRET_RE

    if _SECRET_RE.search(json.dumps([argv, resume_argv])) or any(
        re.fullmatch(r"--(?:api[-_]key|token|password|secret)(?:=.*)?", v, re.I)
        for v in [*(argv or []), *(resume_argv or [])]
    ):
        raise ValueError(
            "adapter argv must not store credentials; use the provider's own host-local login/environment"
        )
    value["execution_origin"] = current(profile_provider)
    with _locked(path):
        profiles = json.loads(path.read_text()) if path.exists() else {}
        profiles[model] = value
        _atomic_write(path, profiles)
    return value


def _argv(profile, values, resume=False):
    if profile["provider"] == "custom":
        template = profile["resume_argv"] if resume else profile["argv"]
        if not template:
            raise ValueError(
                "adapter has no resume argv; completed progress will not be restarted"
            )
        result = []
        for part in template:
            for key, value in values.items():
                part = part.replace("{" + key + "}", str(value))
            result.append(part)
        return result
    exe = profile["executable"]
    model = profile["provider_model"]
    if profile["provider"] == "codex":
        common = (
            [exe, "exec"]
            + (["resume"] if resume else [])
            + [
                "--json",
                "--model",
                model,
                "-c",
                "model_reasoning_effort=" + json.dumps(values["effort"]),
            ]
        )
        return common + ([values["thread_id"]] if resume else []) + [values["prompt"]]
    return [
        exe,
        "-p",
        values["prompt"],
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--effort",
        values["effort"],
    ] + (["--resume", values["thread_id"]] if resume else [])


def require_policy_clear(data_root, project_id, task_id, scope):
    """An older thread is not an escape from a newer refusal on this scope."""
    directory = project_data_dir(Path(data_root or default_data_root()), project_id)
    from .collaboration_registry import list_changes
    from .execution_outbox import directory as outbox_directory

    def stop(kind):
        label = (
            "a provider policy refusal"
            if kind == "policy-review"
            else "unverified provider output"
        )
        raise ValueError(
            f"this task scope has {label}; automatic fresh-session/provider replay is disabled; inspect the original attempt before resume or quota return"
        )

    for pending in outbox_directory().glob("*.json"):
        try:
            row = json.loads(pending.read_text())
        except (OSError, ValueError):
            continue
        if (
            row.get("project_id") == project_id
            and row.get("task_id") == task_id
            and row.get("event")
            in {"dispatch.policy-stopped", "dispatch.review-required"}
            and Path(row.get("data_root", "")).resolve()
            == Path(data_root or default_data_root()).resolve()
            and row.get("fields", {}).get("scope_sha256") == scope
        ):
            stop(
                "policy-review"
                if row["event"] == "dispatch.policy-stopped"
                else "output-incomplete"
            )

    cursor = 0
    while True:
        page = list_changes(
            data_root, project_id=project_id, since=cursor, max_results=500
        )
        for row in page["changes"]:
            if (
                row.get("event")
                in {"dispatch.policy-stopped", "dispatch.review-required"}
                and row.get("task_id") == task_id
                and row.get("scope_sha256") == scope
            ):
                stop(
                    "policy-review"
                    if row["event"] == "dispatch.policy-stopped"
                    else "output-incomplete"
                )
        cursor = page["next_cursor"]
        if not page["truncated"]:
            break
    for path in (directory / "worker-jobs").glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
            if job.get("task_id") != task_id or job.get("scope_sha256") != scope:
                continue
            result_file = path.parent / "result.json"
            result = json.loads(result_file.read_text()) if result_file.exists() else {}
        except (OSError, ValueError):
            # Legacy directories can belong to unrelated tasks. New stops are
            # independently journaled with a task/scope binding above.
            continue
        if result.get("failure_class") in {"policy-review", "output-incomplete"}:
            stop(result["failure_class"])


def dispatch(
    root,
    *,
    data_root=None,
    project_id=None,
    task_id,
    model=None,
    effort="high",
    assignee=None,
    dry_run=False,
    background=False,
    resume=False,
    continuation="Continue the assigned task; preserve completed progress.",
    retry=0,
    controller=None,
    audience="worker",
    fallback_models=(),
    handoff=None,
    resume_job=None,
):
    from .collaboration_workflow import prompt

    registration = resolve_project(root, data_root)
    project_id = project_id or (registration or {}).get("project_id")
    if not project_id:
        raise ValueError("registered project root or project-id is required")
    task = next(
        (
            t
            for t in list_tasks(data_root, project_id=project_id)
            if t["task_id"] == task_id
        ),
        None,
    )
    if task is None:
        raise ValueError("task_id does not exist")
    data = Path(data_root or default_data_root())
    directory = project_data_dir(data, project_id)
    index = directory / "worker-jobs.json"
    jobs = json.loads(index.read_text()) if index.exists() else {}
    previous = jobs.get(task_id)
    if resume_job:
        if not resume or not re.fullmatch(r"job-[0-9a-f]{16}", resume_job):
            raise ValueError(
                "resume-job must identify a recorded job and requires resume"
            )
        previous = json.loads(
            (directory / "worker-jobs" / resume_job / "job.json").read_text()
        )
        if (
            previous.get("task_id") != task_id
            or previous.get("project_id") != project_id
        ):
            raise ValueError("resume job belongs to a different task/project")
    if resume and not previous:
        raise ValueError("no recorded run exists for this task")
    if resume:
        if model and model != previous["model"]:
            raise ValueError(
                "resume must use the recorded adapter; use an explicit handoff for another model"
            )
        model = model or previous["model"]
        result_file = Path(previous["job_dir"]) / "result.json"
        if (
            result_file.exists()
            and json.loads(result_file.read_text()).get("failure_class")
            == "policy-review"
        ):
            raise ValueError(
                "previous session received a provider policy refusal; blind resume is disabled. Review the legitimate scope and provider feedback before further action."
            )
    if not model:
        raise ValueError("select a model explicitly or use collaboration recommend")
    profiles_path = data / "executor-adapters.json"
    profiles = json.loads(profiles_path.read_text()) if profiles_path.exists() else {}
    if model == "auto":
        from .executor_routing import for_task, _model_alias

        recommended = for_task(data, project_id, task_id)["candidates"]
        names = [
            name
            for alias in recommended
            for name in profiles
            if _model_alias(name) == alias
        ]
        from .subscription_quota import automatic_available

        names = [
            n
            for n in names
            if automatic_available(data, project_id, n, profile=profiles[n])
        ]
        if not names:
            raise ValueError("no configured and available adapter matches this task")
        model = names[0]
        fallback_models = tuple(fallback_models) or tuple(names[1:])
    if (
        not isinstance(fallback_models, (list, tuple))
        or len(fallback_models) > 5
        or any(not isinstance(v, str) or v not in profiles for v in fallback_models)
        or len(set((model, *fallback_models))) != len(fallback_models) + 1
    ):
        raise ValueError(
            "fallback models must be at most five distinct configured adapters"
        )
    if model not in profiles:
        raise ValueError(
            "model adapter is not configured; use continuity worker configure"
        )
    profile = profiles[model]
    from .runtime_origin import require_local

    if not profile.get("execution_origin"):
        raise ValueError(
            "adapter has no host/profile binding; reconfigure it on its owning machine rather than migrating private state"
        )
    require_local(
        profile["execution_origin"],
        profile.get("profile_provider", profile["provider"]),
    )
    if resume:
        if not previous.get("execution_origin"):
            raise ValueError(
                "session has no host/profile binding; do not reuse an unscoped or migrated thread"
            )
        require_local(
            previous["execution_origin"],
            profile.get("profile_provider", profile["provider"]),
        )
    if effort not in {"high", "max"}:
        raise ValueError("effort must be high or max")
    from .executor_routing import _model_alias

    if _model_alias(model) == "glm":
        effort = "max"
    actor = assignee or task.get("assignee") or ("worker-" + model)
    controller = (
        controller
        or os.environ.get("CONTINUITY_CONTROLLER")
        or os.environ.get("CONTINUITY_ASSIGNEE")
        or os.environ.get("CODEX_THREAD_ID")
        or "operator"
    )
    assembled = prompt(
        data,
        project_id=project_id,
        task_id=task_id,
        assignee=actor,
        controller=controller,
        audience=audience,
    )
    require_policy_clear(data, project_id, task_id, assembled["scope_sha256"])
    if previous and not resume:
        last_result = Path(previous["job_dir"]) / "result.json"
        if (
            last_result.exists()
            and json.loads(last_result.read_text()).get("failure_class")
            == "policy-review"
            and previous.get("scope_sha256") == assembled["scope_sha256"]
        ):
            raise ValueError(
                "this unchanged task received a provider policy refusal; automatic fresh-session/provider replay is disabled. Review the authorized scope and provider feedback first."
            )
    job_id = "job-" + uuid.uuid4().hex[:16]
    jobdir = directory / "worker-jobs" / job_id
    thread = ""
    if resume:
        thread = Path(previous["thread_file"]).read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", thread):
            raise ValueError("recorded thread id is invalid")
    values = {
        "prompt": continuation if resume else assembled["prompt"],
        "prompt_file": str(jobdir / "prompt.txt"),
        "report_file": str(jobdir / "report.md"),
        "thread_file": str(jobdir / "thread"),
        "thread_id": thread,
        "model": profile["provider_model"],
        "effort": effort,
    }
    context_sha = hashlib.sha256(assembled["prompt"].encode()).hexdigest()
    if handoff and not resume:
        values["prompt"] += (
            "\n\nContinuation from an interrupted executor; preserve existing work, inspect the current checkout and do not repeat completed steps:\n"
            + json.dumps(handoff, ensure_ascii=False)
        )
    if resume and (
        previous.get("rules_digest") != assembled["rules_digest"]
        or previous.get("task_context_sha256") != context_sha
    ):
        values["prompt"] = (
            continuation + "\n\nUpdated task/rules context:\n" + assembled["prompt"]
        )
    actual_prompt_sha = hashlib.sha256(values["prompt"].encode()).hexdigest()
    argv = _argv(profile, values, resume)
    plan = dict(
        task_id=task_id,
        model=model,
        assignee=actor,
        effort=effort,
        command=argv,
        prompt_sha256=actual_prompt_sha,
        task_context_sha256=context_sha,
        scope_sha256=assembled["scope_sha256"],
        command_receipt=[
            v.replace(values["prompt"], "<prompt:" + actual_prompt_sha + ">")
            for v in argv
        ],
        rules_digest=assembled["rules_digest"],
        rules_audience=assembled["rules_audience"],
        controller=controller,
        job_id=job_id,
        worktree=task.get("worktree") or str(root),
        job_dir=str(jobdir),
        thread_file=values["thread_file"],
        report_file=values["report_file"],
        resume=resume,
        retry=retry,
        fallback_models=list(fallback_models),
        control_root=str(root),
        execution_origin=profile["execution_origin"],
    )
    from .persistent_storage import describe

    plan["storage"] = describe(jobdir)
    if dry_run:
        return {**plan, "executed": False}
    from .supervisor import daemon_once
    from .collaboration_registry import _load_ledger

    daemon_once(data, project_id=project_id)
    binding = (
        _load_ledger(data, project_id).get("execution_bindings", {}).get(task_id, {})
    )
    if binding.get("status") == "running":
        raise ValueError(
            "this task already has a running worker; inspect it or use a separate isolated task/worktree"
        )
    if task["status"] in {"completed", "merged", "rejected", "superseded"}:
        raise ValueError(
            "closed card requires an explicit successor or metadata update before dispatch"
        )
    if resume:
        _write(jobdir / "thread", thread)
    _write(jobdir / "prompt.txt", values["prompt"])
    _atomic_write(
        jobdir / "job.json",
        {
            **plan,
            "data_root": str(data),
            "project_id": project_id,
            "profile": profile,
            "values": values,
        },
    )
    with _locked(index):
        current = json.loads(index.read_text()) if index.exists() else {}
        current[task_id] = plan
        _atomic_write(index, current)
    if background:
        with (jobdir / "supervisor.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", __name__, "--run-job", str(jobdir / "job.json")],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        return {
            k: v
            for k, v in {
                **plan,
                "executed": True,
                "supervisor_pid": process.pid,
            }.items()
            if k != "command"
        }
    return run_job(jobdir / "job.json")


def run_job(file):
    from .supervisor import run_attempt

    job = json.loads(Path(file).read_text())
    from .runtime_origin import require_local

    if not job.get("execution_origin"):
        raise ValueError(
            "job has no host/profile binding; run it through its owning runtime"
        )
    require_local(
        job["execution_origin"],
        job["profile"].get("profile_provider", job["profile"]["provider"]),
    )
    require_policy_clear(
        job["data_root"], job["project_id"], job["task_id"], job["scope_sha256"]
    )
    capture = Capture(job["job_dir"])
    values = {
        **job["values"],
        "thread_id": "{thread_id}",
        "prompt": "Continue the assigned task; preserve completed progress.",
    }
    resume_command = _argv(job["profile"], values, True) if job["retry"] else None
    try:
        result = run_attempt(
            job["data_root"],
            project_id=job["project_id"],
            task_id=job["task_id"],
            assignee=job["assignee"],
            command=job["command"],
            model=job["model"],
            attempt_id=job["job_id"],
            report_file=job["report_file"],
            thread_file=job["thread_file"],
            resume_command=resume_command,
            retry=job["retry"],
            workdir=job["worktree"],
            on_output=capture,
            rules_digest=job["rules_digest"],
            rules_audience=job["rules_audience"],
            controller=job.get("controller"),
            command_receipt=job.get("command_receipt"),
        )
    finally:
        capture.close()
    value = {
        **result,
        "job_id": job["job_id"],
        "thread_file": job["thread_file"],
        "report_file": job["report_file"],
        "log_file": str(Path(job["job_dir"]) / "output.jsonl"),
        "log_truncated": capture.log_truncated,
        "executed": True,
    }
    if result.get("failure_class") in {"policy-review", "output-incomplete"}:
        from .execution_outbox import change as record_stop

        record_stop(
            Path(job["data_root"]),
            job["project_id"],
            "dispatch.policy-stopped"
            if result["failure_class"] == "policy-review"
            else "dispatch.review-required",
            job["task_id"],
            job_id=job["job_id"],
            attempt_id=job["job_id"],
            scope_sha256=job["scope_sha256"],
        )
    try:
        _atomic_write(Path(job["job_dir"]) / "result.json", value)
    except OSError:
        value["metadata_warning"] = (
            "Run finished, but the local result file could not be saved; inspect the execution outbox/events"
        )
    if (
        result.get("retryable")
        and result.get("failure_class") != "policy-review"
        and job.get("fallback_models")
    ):
        from .collaboration_registry import _record_change
        from .runtime_routing import statistics
        from .executor_routing import _model_alias
        from .subscription_quota import automatic_available

        availability = statistics(job["data_root"], job["project_id"])
        candidates = [
            m
            for m in job["fallback_models"]
            if not availability.get(_model_alias(m), {}).get("unavailable_until")
            and automatic_available(job["data_root"], job["project_id"], m)
        ]
        if candidates:
            next_model, *remaining = candidates
            _record_change(
                Path(job["data_root"]),
                job["project_id"],
                "dispatch.fallback",
                job["task_id"],
                from_model=job["model"],
                to_model=next_model,
                reason=result.get("failure_class"),
                previous_job=job["job_id"],
            )
            next_result = dispatch(
                job["control_root"],
                data_root=job["data_root"],
                project_id=job["project_id"],
                task_id=job["task_id"],
                model=next_model,
                effort=job["effort"],
                assignee=job["assignee"],
                retry=job["retry"],
                controller=job.get("controller"),
                audience=job["rules_audience"],
                fallback_models=remaining,
                handoff={
                    "previous_job": job["job_id"],
                    "reason": result.get("failure_class"),
                    "report_file": job["report_file"],
                    "checkout": job["worktree"],
                },
            )
            value = {
                **next_result,
                "previous_jobs": [job["job_id"], *next_result.get("previous_jobs", [])],
            }
            _atomic_write(Path(job["job_dir"]) / "result.json", value)
    return value


def command(args):
    from .planning_api import render

    if args.worker_action == "configure":
        value = configure(
            args.data_root,
            model=args.model,
            provider=args.provider,
            profile_provider=args.profile_provider,
            official_subscription=args.official_subscription,
            provider_model=args.provider_model,
            executable=args.executable,
            argv=json.loads(args.argv_json) if args.argv_json else None,
            resume_argv=json.loads(args.resume_argv_json)
            if args.resume_argv_json
            else None,
        )
    elif args.worker_action == "report":
        from .delivery_records import detail_view

        project = resolve_project(args.root, args.data_root)
        value = detail_view(
            args.data_root,
            args.project_id or (project or {})["project_id"],
            args.task_id,
            True,
        )
        if not value.get("report"):
            raise ValueError("no final report is attached yet")
        report = value["report"]
        text = report["text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        if report.get("sha256") and report["sha256"] != digest:
            raise ValueError("report integrity mismatch")
        if not 1024 <= args.max_output_bytes <= 131072 or not 0 <= args.offset <= len(
            text
        ):
            raise ValueError("invalid report output budget/range")
        if args.full and len(text.encode()) + 1 > args.max_output_bytes:
            raise ValueError(
                "full report exceeds budget; increase max-output-bytes or read explicit pages"
            )
        if args.full:
            print(text)
            return 0
        segment = (
            text[args.offset :]
            .encode()[: args.max_output_bytes - 256]
            .decode("utf-8", "ignore")
        )
        end = args.offset + len(segment)
        print(
            f"report_sha256={digest} bytes={len(text.encode())} next_offset_chars={end if end < len(text) else 'none'}\n"
            + segment
        )
        return 0
    else:
        value = dispatch(
            args.root,
            data_root=args.data_root,
            project_id=args.project_id,
            task_id=args.task_id,
            model=args.model,
            effort=args.effort,
            assignee=args.assignee,
            dry_run=args.dry_run or args.worker_action == "prepare",
            background=args.background,
            resume=args.worker_action == "resume",
            continuation=args.continuation,
            retry=args.retry,
            controller=args.controller,
            audience=args.audience,
            fallback_models=args.fallback_model,
            resume_job=args.resume_job,
        )
    print(render(value, format="json", max_output_bytes=65536))
    return 2 if value.get("status") in {"failed", "interrupted"} else 0


def _run_parser(parser, action):
    parser.set_defaults(handler=command, worker_action=action)
    for field in ("project-id", "data-root", "model", "assignee"):
        parser.add_argument("--" + field)
    parser.add_argument("--root", default=".")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--effort", choices=("high", "max"), default="high")
    parser.add_argument("--retry", type=int, default=0)
    parser.add_argument(
        "--fallback-model",
        action="append",
        default=[],
        help="Explicitly allowed adapter after a transient failure; never used for authentication or policy refusal",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument(
        "--resume-job",
        help="Original recorded job id, including a primary session preserved before failover",
    )
    parser.add_argument("--controller")
    parser.add_argument("--audience", default="worker")
    parser.add_argument(
        "--continuation",
        default="Continue the assigned task; preserve completed progress.",
    )


def register_cli(commands):
    parser = commands.add_parser(
        "worker", help="Configure adapters, dispatch, resume and read final reports"
    )
    sub = parser.add_subparsers(dest="worker_action", required=True)
    for action in ("dispatch", "resume", "prepare"):
        _run_parser(sub.add_parser(action), action)
    configure_parser = sub.add_parser("configure")
    configure_parser.set_defaults(handler=command)
    configure_parser.add_argument("--data-root")
    configure_parser.add_argument("--model", required=True)
    configure_parser.add_argument(
        "--official-subscription",
        action="store_true",
        help="Declare this adapter uses the native client's official subscription; do not apply that account's quota to unrelated API/gateway routes",
    )
    configure_parser.add_argument(
        "--provider", choices=("codex", "claude", "custom"), required=True
    )
    configure_parser.add_argument(
        "--profile-provider",
        choices=("codex", "claude", "custom"),
        help="Owning native client namespace for a custom wrapper; no profile copying",
    )
    for field in ("provider-model", "executable", "argv-json", "resume-argv-json"):
        configure_parser.add_argument("--" + field)
    report = sub.add_parser("report")
    report.set_defaults(handler=command)
    report.add_argument("--task-id", required=True)
    report.add_argument("--root", default=".")
    report.add_argument("--project-id")
    report.add_argument("--data-root")
    report_alias = commands.add_parser(
        "report", help="Read the attached final worker report"
    )
    report_alias.set_defaults(handler=command, worker_action="report")
    report_alias.add_argument("task_id")
    report_alias.add_argument("--root", default=".")
    report_alias.add_argument("--project-id")
    report_alias.add_argument("--data-root")
    for reader in (report, report_alias):
        reader.add_argument("--max-output-bytes", type=int, default=8192)
        reader.add_argument(
            "--offset",
            type=int,
            default=0,
            help="Character offset from the previous report page",
        )
        reader.add_argument(
            "--full",
            action="store_true",
            help="Return the complete report or fail explicitly if it exceeds the budget",
        )
    _run_parser(
        commands.add_parser(
            "dispatch",
            help="Assemble and supervise an explicitly selected worker; --dry-run previews it",
        ),
        "dispatch",
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run-job":
        result = run_job(sys.argv[2])
        print(json.dumps(result))
        raise SystemExit(2 if result.get("status") in {"failed", "interrupted"} else 0)
