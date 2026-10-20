"""Reproduce adopter workflows with synthetic data and Python-only workers.

No native agent, account, transcript or network request is used. The workspace
must be new/empty; artifacts are retained. Only allowlisted aggregates are public.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def verify(workspace, cli):
    workspace = Path(workspace).resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError(
            "choose a new or empty owned workspace; nothing is overwritten"
        )
    workspace.mkdir(parents=True, exist_ok=True)
    root = workspace / "repository"
    root.mkdir()
    cli = shutil.which(cli)
    if not cli:
        raise ValueError(
            "install continuity-plane first or pass --cli /absolute/executable"
        )
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("GIT_"):
            env.pop(name)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for name in (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CONTINUITY_CONTROLLER_TOKEN",
        "CONTINUITY_TASK_ID",
    ):
        env.pop(name, None)
    env.update(
        CONTINUITY_DATA_ROOT=str(workspace / "data"),
        CONTINUITY_OUTBOX_ROOT=str(workspace / "outbox"),
        CONTINUITY_WORK_ROOT=str(workspace / "work"),
        XDG_CACHE_HOME=str(workspace / "cache"),
        CONTINUITY_ASSIGNEE="controller",
        CONTINUITY_CONTROLLER="controller",
        CONTINUITY_ROLE="controller",
        CONTINUITY_STATE_CHECKPOINTS="0",
    )
    cases, sizes = {}, {}

    def call(*argv, raw=False, expected=0):
        result = subprocess.run(
            [cli, *argv], cwd=root, env=env, capture_output=True, text=True, timeout=30
        )
        if result.returncode != expected:
            raise RuntimeError(
                f"fixture command failed ({result.returncode}): {argv[:3]}: {result.stderr[:600]}"
            )
        return result.stdout if raw else json.loads(result.stdout)

    def git(*argv):
        return subprocess.check_output(
            ["git", "-C", str(root), *argv],
            text=True,
            stderr=subprocess.DEVNULL,
            env=env,
        ).strip()

    (root / "demo.py").write_text(
        "def sample_symbol():\n    return 42\n", encoding="utf-8"
    )
    git("init", "-b", "main")
    hooks = workspace / "empty-hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    git("add", "demo.py")
    git(
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Synthetic fixture",
    )
    commit = git("rev-parse", "HEAD")
    version = call("--version", raw=True).strip()
    call("init", "--root", str(root), "--project-id", "demo")
    call(
        "collaboration",
        "register",
        "--project-id",
        "demo",
        "--control-root",
        str(root),
        "--repository-root",
        str(root),
    )
    scope = ["--project-id", "demo"]
    identity = call(
        "whoami",
        "--root",
        str(root),
        "--assignee",
        "controller",
        "--controller",
        "controller",
    )
    cases["bootstrap_identity"] = (
        identity["project_id"] == "demo" and identity["controller"] == "controller"
    )
    call("context", "index", "--root", str(root))
    lookup = call(
        "context",
        "lookup",
        "--root",
        str(root),
        "--query",
        "sample_symbol",
        "--format",
        "brief",
        "--max-output-bytes",
        "1024",
        raw=True,
    )
    sizes["lookup_bytes"] = len(lookup.encode())
    cases["brief_lookup_budget"] = (
        "sample_symbol" in lookup and sizes["lookup_bytes"] <= 1024
    )
    call(
        "rules",
        "update",
        *scope,
        "--rule-id",
        "scope",
        "--text",
        "Only modify demo.py; preserve verification evidence.",
    )
    exported = call(
        "rules", "export", *scope, "--audience", "worker", "--format", "text", raw=True
    )
    mirror = workspace / "rules.txt"
    mirror.write_text(exported, encoding="utf-8")
    cases["complete_rules_and_mirror"] = (
        call("rules", "check-mirror", *scope, "--file", str(mirror))["status"]
        == "current"
    )

    def card(name):
        call(
            "collaboration",
            "task",
            "add",
            *scope,
            "--task-id",
            name,
            "--lane-id",
            name,
            "--mode",
            "patch",
            "--priority",
            "p2",
            "--title",
            "Synthetic check",
            "--objective",
            "Verify the synthetic runner",
            "--next-action",
            "Check report",
            "--exit-criterion",
            "The fixture report is readable",
            "--assignee",
            "worker-" + name,
            "--worktree",
            str(root),
            "--body",
            "Inspect sample_symbol and return the fixture report",
            "--allowed-file",
            "demo.py",
        )

    for name in ("a", "b", "failed", "early"):
        card(name)
    call(
        "collaboration",
        "inbox",
        "send",
        *scope,
        "--message-id",
        "message",
        "--sender",
        "controller",
        "--recipient",
        "worker-a",
        "--body",
        "Check the fixture",
    )
    inbox = call("collaboration", "inbox", "list", *scope, "--recipient", "worker-a")
    call(
        "collaboration",
        "inbox",
        "ack",
        *scope,
        "--recipient",
        "worker-a",
        "--message-id",
        "message",
    )
    archived = call(
        "collaboration",
        "inbox",
        "archive",
        *scope,
        "--recipient",
        "worker-a",
    )
    empty = call("collaboration", "inbox", "list", *scope, "--recipient", "worker-a")
    cases["inbox_delivery_ack"] = (
        len(inbox["messages"]) == 1
        and archived["archive"]["archived"] == 1
        and empty["messages"] == []
    )
    packet = call("collaboration", "packet", *scope, "--assignee", "worker-a")
    cases["assigned_packet"] = packet["task"]["task_id"] == "a"
    prompt = call(
        "collaboration", "prompt", *scope, "--task-id", "a", "--assignee", "worker-a"
    )
    cases["prompt_from_rules_and_body"] = (
        "sample_symbol" in prompt["prompt"] and "Only modify" in prompt["prompt"]
    )
    code = 'import json; print(json.dumps({"type":"thread.started","thread_id":"synthetic-thread"})); print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Synthetic fixture report: PASS"}}))'
    for model, program in (
        ("fixture", code),
        ("fixture-failed", "raise SystemExit(3)"),
    ):
        call(
            "worker",
            "configure",
            "--model",
            model,
            "--provider",
            "custom",
            "--argv-json",
            json.dumps([sys.executable, "-c", program, "{effort}"]),
        )
    for name in ("a", "b"):
        result = call(
            "dispatch",
            *scope,
            "--task-id",
            name,
            "--model",
            "fixture",
            "--effort",
            "xhigh",
            "--timeout-seconds",
            "5",
            "--idle-timeout-seconds",
            "3",
        )
        assert result["status"] == "completed"
    cases["two_supervised_workers"] = True
    report = call("report", "a", *scope, raw=True)
    cases["final_report_readable"] = "Synthetic fixture report" in report
    failed = call(
        "dispatch",
        *scope,
        "--task-id",
        "failed",
        "--model",
        "fixture-failed",
        "--timeout-seconds",
        "5",
        "--idle-timeout-seconds",
        "3",
        expected=2,
    )
    cases["failed_exit_recorded_cli_nonzero"] = (
        failed["return_code"] == 3 and failed["status"] == "failed"
    )
    sizes["failed_worker_exit_code"] = failed["return_code"]
    sizes["failed_dispatch_cli_exit_code"] = 2
    early_code = (
        "import subprocess; subprocess.run("
        + repr(
            [
                cli,
                "collaboration",
                "task",
                "update",
                "--project-id",
                "demo",
                "--task-id",
                "early",
                "--status",
                "ready-for-review",
                "--assignee",
                "worker-early",
            ]
        )
        + ", check=True, stdout=subprocess.DEVNULL); "
        + code
    )
    call(
        "worker",
        "configure",
        "--model",
        "fixture-early",
        "--provider",
        "custom",
        "--argv-json",
        json.dumps([sys.executable, "-c", early_code]),
    )
    early = call(
        "dispatch",
        *scope,
        "--task-id",
        "early",
        "--model",
        "fixture-early",
        "--timeout-seconds",
        "5",
        "--idle-timeout-seconds",
        "3",
    )
    assert early["status"] == "completed"
    early_card = call("collaboration", "task", "show", *scope, "--task-id", "early")
    attached = bool(early_card.get("details", {}).get("report"))
    assert attached, "Supervisor must attach the report after an early card update"
    assert "Synthetic fixture report" in call("report", "early", *scope, raw=True)
    known = {"report_after_early_card_update": "attached"}
    call("todo", "from-cards", *scope, raw=True)
    verification = {
        "commit": commit,
        "actor": "controller",
        "checks": [
            {
                "name": "report",
                "command": "continuity report a --project-id demo",
                "result": "Synthetic fixture report: PASS",
                "status": "pass",
                "required": True,
            }
        ],
        "unverified": [],
    }
    merged = call(
        "collaboration",
        "task",
        "update",
        *scope,
        "--task-id",
        "a",
        "--status",
        "merged",
        "--branch",
        "main",
        "--commit",
        commit,
        "--verification-json",
        json.dumps(verification),
    )
    cases["single_update_merge_metadata"] = (
        merged["status"] == "merged" and git("rev-parse", "HEAD") == commit
    )
    call(
        "lesson",
        "add",
        *scope,
        "--lesson-id",
        "fixture",
        "--text",
        "Verify exact checks",
        "--source",
        "Synthetic workflow",
    )
    call(
        "handoff",
        "write",
        *scope,
        "--plan",
        "Review the fixture before another task",
        "--next-action",
        "Inspect reports",
    )
    cases["shared_handoff_and_lesson"] = (
        call("handoff", "show", *scope)["next_action"] == "Inspect reports"
    )
    profile_path = workspace / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "language": "zh",
                "report_format": "conclusion-first",
                "question_policy": "plain-if-native-unavailable",
            }
        ),
        encoding="utf-8",
    )
    call(
        "profile",
        "set",
        *scope,
        "--member",
        "controller",
        "--patch-file",
        str(profile_path),
        "--expected-revision",
        "0",
        "--format",
        "json",
    )
    call(
        "memory",
        "note",
        *scope,
        "--member",
        "controller",
        "--record-id",
        "report-style",
        "--kind",
        "preference",
        "--scope",
        "private-project",
        "--source",
        "synthetic-user-instruction",
        "--text",
        "Lead with the conclusion and separate verified facts.",
        "--format",
        "json",
    )
    call(
        "memory",
        "adopt",
        *scope,
        "--member",
        "controller",
        "--record-id",
        "report-style",
        "--expected-revision",
        "1",
        "--reason",
        "Synthetic approval",
        "--format",
        "json",
    )
    controller_start = call(
        "controller",
        "start",
        *scope,
        "--member",
        "controller",
        "--capability",
        "question",
        "--max-output-bytes",
        "4096",
        "--format",
        "json",
    )
    sizes["controller_start_json_bytes"] = len(
        json.dumps(controller_start, ensure_ascii=False, sort_keys=True).encode()
    )
    sizes["controller_start_budget_bytes"] = 4096
    cases["scoped_controller_start"] = (
        controller_start["packet_kind"] == "controller-start/v1"
        and controller_start["complete"]
        and controller_start["next_action"] == "Inspect reports"
        and controller_start["profile"]["values"]["language"] == "zh"
        and any(
            row["record_id"] == "report-style" for row in controller_start["memory"]
        )
        and controller_start["question_route"] == "native-if-actually-available"
    )
    lease = call(
        "controller", "acquire", *scope, "--owner", "controller", "--ttl-seconds", "300"
    )
    assert lease["token"]
    cases["second_controller_read_only"] = (
        call("controller", "status", *scope, "--owner", "observer")["read_only"] is True
    )
    cursor, count = 0, 0
    while True:
        page = call(
            "collaboration",
            "changes",
            *scope,
            "--since",
            str(cursor),
            "--max-results",
            "1",
        )
        count += len(page["changes"])
        previous, cursor = cursor, page["next_cursor"]
        if not page["truncated"]:
            break
        assert cursor > previous
    quiet = call("collaboration", "changes", *scope, "--since", str(cursor), raw=True)
    sizes["no_change_bytes"] = len(quiet.encode())
    event = call(
        "collaboration",
        "wait",
        *scope,
        "--since",
        "0",
        "--task-id",
        "a",
        "--event",
        "attempt.finished",
        "--event",
        "attempt.timeout",
        "--timeout-seconds",
        "1",
    )
    cases["pagination_and_multi_event_wait"] = (
        count > 0 and not json.loads(quiet)["changes"] and bool(event["changes"])
    )
    assert all(cases.values()), cases
    receipt = {
        "schema_version": "continuity.adopter-workflow-check/v1",
        "version": version,
        "dataset": "synthetic-only",
        "native_provider_requests": 0,
        "cases": cases,
        "measurements": sizes,
        "observed_boundaries": known,
        "limits": [
            "No real model/API, native host session, hardware or multi-machine field run.",
            "Byte counts describe this fixture, not a token or billing reduction.",
        ],
    }
    (workspace / "receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        required=True,
        help="New/empty persistent owned directory; retained",
    )
    parser.add_argument("--cli", default="continuity")
    args = parser.parse_args()
    print(json.dumps(verify(args.workspace, args.cli), indent=2))
