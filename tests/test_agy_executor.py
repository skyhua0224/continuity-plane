"""Replay the native CLI's public output shapes with an isolated executable."""

import contextlib
import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from continuity_plane.cli import entrypoint
from continuity_plane.collaboration_registry import add_task, register_project
from continuity_plane.retry_policy import classify, provider_error_text
from continuity_plane.worker_dispatch import Capture


# These snapshots omit account information and use synthetic paths and IDs.
MODELS = (
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)\n"
    "gemini-4-argon-low\tGemini 4 Argon (Low)\n"
    "claude-opus-5-5-low\tClaude Opus 5.5 (Low)\n"
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
    "gemini-pro-orchestrator-g1-pro\tOrchestrator (Argon)\n"
)
UNKNOWN_MODEL = (
    'invalid model selection (--model "no-such-model" --effort ""): '
    "model no-such-model is not recognized as a known model or custom model in settings"
)


class NativeAgyTests(unittest.TestCase):
    def setUp(self):
        from continuity_plane.agy_probe import forget

        forget()
        self.addCleanup(forget)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.data = self.base / "data"
        self.root = self.base / "work"
        self.root.mkdir()
        self.agy = self.base / "agy"
        self.calls = self.base / "agy-calls.jsonl"
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CONTINUITY_") and key != "XDG_CONFIG_HOME"
        }
        env.update(
            CONTINUITY_DATA_ROOT=str(self.data),
            CONTINUITY_UPDATE_CHECK="0",
            TMPDIR=str(self.base),
            HOME=str(self.base),
            PATH=os.pathsep.join((str(self.base), os.environ["PATH"])),
            AGY_TEST_CALLS=str(self.calls),
        )
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Verify dispatch",
            objective="Replay a local session",
            next_action="Check the report",
            exit_criteria=["Report attaches"],
            assignee="worker",
            worktree=self.root,
            body="Write one line in the workspace and report it.",
        )
        now = datetime.now(UTC).replace(microsecond=0)
        self.reset_5h = (now + timedelta(hours=2)).isoformat()
        self.reset_weekly = (now + timedelta(days=2)).isoformat()
        usage = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini 4 Neon, Gemini 3.8 Flash",
                    "buckets": [
                        {"window": "weekly", "remaining_fraction": 0.6, "reset_time": self.reset_weekly},
                        {"window": "5h", "remaining_fraction": 0.25, "reset_time": self.reset_5h},
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "description": "For paid Pro plans, Argon quota is separate.",
                    "buckets": [
                        {"window": "weekly", "remaining_fraction": 0, "reset_time": self.reset_weekly},
                    ],
                },
            ],
        }
        slash = {
            "usage": usage,
            "model": {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)", "effort": "low"},
            "effort": {"adjustable": True, "current": "low", "available": ["low", "high"]},
            "config": {"config": {"model": "gemini-3.8-flash-low", "trustedWorkspaces": ["/work"]}},
            "hooks": {"hooks": {}},
            "skills": {"skills": [{"name": "guide", "path": "/opt/agy/skills/guide/SKILL.md", "builtin": True}]},
            "agents": {"agents": []},
            "credits": {"remaining_credits": 0},
        }
        self.agy.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"slash = {slash!r}\n"
            f"models = {MODELS!r}\n"
            f"unknown_model = {UNKNOWN_MODEL!r}\n"
            + """\
args = sys.argv[1:]
with open(os.environ["AGY_TEST_CALLS"], "a") as log:
    print(json.dumps({"args": args, "cwd": os.getcwd()}), file=log)
if args == ["--version"]:
    print("1.5.13")
elif args == ["models"]:
    print(models, end="")
elif args[:1] == ["-p"] and args[1].startswith("/"):
    name = args[1][1:]
    assert args[2:] == ["--output-format", "json"]
    data = slash[name]
    with open(os.environ["AGY_TEST_CALLS"]) as log:
        reads = sum(json.loads(line)["args"][1:2] == ["/usage"] for line in log)
    if name == "usage" and os.environ.get("AGY_TEST_STALE_USAGE") and reads == 1:
        data = json.loads(json.dumps(data))
        for group in data["groups"]:
            for bucket in group["buckets"]:
                bucket["remaining_fraction"] = 1
    print(json.dumps({"status": "SUCCESS", "num_turns": 0,
                      "command": {"name": name, "data": data}}))
else:
    assert args[:1] == ["-p"] and "stream-json" in args
    assert "--dangerously-skip-permissions" in args
    model = args[args.index("--model") + 1]
    if os.environ.get("AGY_TEST_FAIL"):
        print(json.dumps({"event": "result", "result": {
            "status": "ERROR", "response": "", "error": unknown_model,
        }}))
        sys.exit(1)
    conversation = "test-conversation"
    resume = "--conversation" in args
    if resume:
        assert args[args.index("--conversation") + 1] == conversation
    print(json.dumps({"event": "init", "conversation_id": conversation, "init": {
        "model": model, "cwd": "/work", "tools": [], "permission_mode": "bypassPermissions",
    }}))
    print(json.dumps({"event": "step_update", "step_update": {
        "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "Draft",
    }}))
    print(json.dumps({"event": "result", "result": {
        "conversation_id": conversation, "status": "SUCCESS",
        "response": "Resumed report" if resume else "Initial report",
        "duration_seconds": 0.1, "num_turns": 1,
    }}), end="" if resume else "\\n")
""",
            encoding="utf-8",
        )
        self.agy.chmod(0o755)

    def cli(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            actual = entrypoint(list(args))
        self.assertEqual(actual, code, out.getvalue() + err.getvalue())
        if code:
            return out.getvalue() + err.getvalue()
        return out.getvalue().strip()

    def configure(self, model="flash", provider_model="gemini-3.8-flash", *, code=0):
        return self.cli(
            "worker", "configure", "--data-root", str(self.data),
            "--model", model, "--provider", "antigravity", "--provider-model", provider_model,
            "--official-subscription", code=code,
        )

    def worker(self, action, *extra, code=0):
        result = self.cli(
            "worker", action, "--data-root", str(self.data), "--project-id", "sample",
            "--task-id", "task", "--model", "flash", "--rate-limit-retries", "0",
            *extra, code=code,
        )
        return json.loads(result)

    def test_dispatch_and_resume_capture_thread_and_report(self):
        self.configure(provider_model="gemini-3.8-flash-low")
        for action, report in (("dispatch", "Initial report"), ("resume", "Resumed report")):
            with self.subTest(action=action):
                result = self.worker(action)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(Path(result["thread_file"]).read_text(), "test-conversation")
                self.assertEqual(Path(result["report_file"]).read_text(), report)
                self.assertEqual(self.cli(
                    "worker", "report", "--data-root", str(self.data), "--project-id", "sample",
                    "--task-id", "task", "--full",
                ), report)
        calls = [
            call for call in map(json.loads, self.calls.read_text().splitlines())
            if "stream-json" in call["args"]
        ]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["cwd"], str(self.root))
            argv = call["args"]
            self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertNotIn("--conversation", calls[0]["args"])
        self.assertEqual(calls[1]["args"][-2:], ["--conversation", "test-conversation"])

    def test_maps_max_to_xhigh(self):
        self.configure(provider_model="gemini-4-neon")
        argv = self.worker("dispatch", "--effort", "max", "--dry-run")["command"]
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_effort_above_the_model_range_is_lowered_to_its_highest(self):
        from continuity_plane.agy_probe import clamp_effort

        adapter = json.loads(self.configure("opus", "claude-opus-5-5"))
        self.assertEqual(adapter["efforts"], ["low", "high"])
        self.worker("dispatch", "--model", "opus", "--effort", "max")
        launches = [json.loads(line)["args"] for line in self.calls.read_text().splitlines()]
        launch = next(args for args in launches if "stream-json" in args)
        self.assertEqual(launch[launch.index("--effort") + 1], "high")
        self.assertEqual(clamp_effort("medium", ["low", "high"]), "low")
        self.assertEqual(clamp_effort("low", ["high"]), "high")
        self.assertEqual(clamp_effort("xhigh", []), "xhigh")

    def test_conflicting_effort_is_refused_before_native_dispatch(self):
        self.configure(provider_model="gemini-3.8-flash-low")
        before = self.calls.read_text()
        error = self.cli(
            "worker", "dispatch", "--project-id", "sample", "--task-id", "task",
            "--model", "flash", "--effort", "high", code=2,
        )
        self.assertIn("requires effort 'low'", error)
        self.assertEqual(self.calls.read_text(), before)

    def test_dispatch_error_uses_real_provider_error_text(self):
        self.configure()
        with patch.dict(os.environ, {"AGY_TEST_FAIL": "1"}):
            result = self.worker("dispatch", code=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "permanent")
        self.assertFalse(result["retryable"])
        self.assertIn(UNKNOWN_MODEL.replace('"', '\\"'), Path(result["log_file"]).read_text())

    def test_capture_clears_terminal_failure_on_success(self):
        capture = Capture(self.base / "capture")
        self.addCleanup(capture.close)
        error = {"event": "result", "result": {"status": "ERROR", "error": UNKNOWN_MODEL}}
        output = json.dumps(error) + "\n"
        capture(output.encode())
        self.assertEqual(capture.terminal_failure, UNKNOWN_MODEL)
        self.assertEqual(provider_error_text(output), UNKNOWN_MODEL)
        self.assertEqual(classify(UNKNOWN_MODEL)["failure_class"], "permanent")
        response = {"event": "result", "result": {"status": "SUCCESS", "response": "Final"}}
        capture((json.dumps(response) + "\n").encode())
        self.assertIsNone(capture.terminal_failure)

    def test_validate_base_and_suffixed_model_without_saving_invalid_adapter(self):
        self.configure()
        self.configure("argon", "gemini-4-argon-low")
        message = self.configure("unknown", "no-such-model", code=2)
        self.assertIn("no-such-model", message)
        profiles = json.loads((self.data / "executor-adapters.json").read_text())
        self.assertEqual(set(profiles), {"flash", "argon"})
        self.assertEqual(profiles["flash"]["executable"], "agy")
        self.assertEqual(profiles["flash"]["profile_provider"], "antigravity")
        origin = profiles["flash"]["execution_origin"]
        profile = (self.base / ".gemini/antigravity-cli").resolve()
        identity = str(profile)
        if sys.platform.startswith("linux"):
            identity += "\0" + str((self.base / ".config/antigravity").resolve())
        self.assertEqual(origin["profile_sha256"], hashlib.sha256(identity.encode()).hexdigest())

    @unittest.skipUnless(sys.platform.startswith("linux"), "XDG credential scopes are Linux-specific")
    def test_linux_quota_cannot_query_an_adapter_bound_to_another_credential_scope(self):
        from continuity_plane.runtime_origin import current

        configured = json.loads(self.configure())["execution_origin"]
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / ".config")}):
            self.assertEqual(current("antigravity"), configured)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative-config"}):
            with self.assertRaisesRegex(ValueError, "XDG_CONFIG_HOME must be absolute"):
                current("antigravity")
        before = self.calls.read_text()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "other-config")}):
            self.assertNotEqual(current("antigravity"), configured)
            error = self.cli(
                "quota", "refresh", "--project-id", "sample", "--provider", "antigravity",
                "--model", "flash", code=2,
            )
        self.assertIn("runtime host/profile mismatch", error)
        self.assertEqual(self.calls.read_text(), before)

    def test_inspect_and_mcp_dry_run(self):
        result = json.loads(self.cli("agy", "inspect", "--json"))
        self.assertEqual(result["version"], "1.5.13")
        self.assertIn({"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)"}, result["models"])
        self.assertEqual(result["model"]["effort"], "low")
        self.assertEqual(result["effort"]["available"], ["low", "high"])
        self.assertEqual(result["config"]["config"]["trustedWorkspaces"], ["/work"])
        self.assertEqual(result["hooks"], {"hooks": {}})
        self.assertEqual(result["skills"]["skills"][0]["name"], "guide")
        self.assertEqual(result["agents"], {"agents": []})
        self.assertEqual(result["credits"]["remaining_credits"], 0)
        self.assertEqual(len(result["usage"]["groups"]), 2)
        before = self.calls.read_text()
        dry_run = self.cli("agy", "mcp-install", "--dry-run")
        self.assertEqual(shlex.split(dry_run), ["agy", "mcp", "add", "continuity", "continuity-mcp"])
        self.assertEqual(self.calls.read_text(), before)

    def test_quota_uses_tightest_window_and_matches_separate_argon_group(self):
        from continuity_plane.subscription_quota import automatic_available

        self.configure()
        self.configure("argon", "gemini-4-argon-low")
        for model, used, reset in (
            ("flash", 75, self.reset_5h), ("argon", 100, self.reset_weekly),
        ):
            with self.subTest(model=model):
                sample = json.loads(self.cli(
                    "quota", "refresh", "--data-root", str(self.data), "--project-id",
                    "sample", "--provider", "antigravity", "--model", model,
                ))
                self.assertEqual(sample["used_percent"], used)
                self.assertEqual(sample["resets_at"], reset)
                self.assertEqual(sample["source"], "agy-native:/usage")
                self.assertTrue(sample["official_subscription"])
                self.assertEqual(sample["collection_origin"]["provider"], "antigravity")
                self.assertEqual(automatic_available(self.data, "sample", model), used < 90)

    def test_quota_tick_refreshes_an_antigravity_adapter_through_agy(self):
        self.configure()
        result = json.loads(self.cli(
            "quota", "tick", "--data-root", str(self.data), "--project-id", "sample",
            "--refresh-model", "flash",
        ))
        self.assertIn("items", result)
        quota = json.loads(self.cli(
            "quota", "report", "--data-root", str(self.data), "--project-id", "sample",
        ))
        self.assertEqual(quota["models"]["flash"]["source"], "agy-native:/usage")

    def test_automatic_choice_refreshes_a_missing_agy_snapshot(self):
        from continuity_plane.subscription_quota import automatic_available

        self.configure()
        self.configure("argon", "gemini-4-argon-low")
        self.assertTrue(automatic_available(self.data, "sample", "flash"))
        self.assertFalse(automatic_available(self.data, "sample", "argon"))
        reads = [json.loads(line)["args"][1:2] for line in self.calls.read_text().splitlines()]
        self.assertEqual(reads.count(["/usage"]), 3, "both adapters share one set of reads")

    def test_finished_agy_run_refreshes_its_quota(self):
        self.configure()
        self.worker("dispatch")
        quota = json.loads(self.cli(
            "quota", "report", "--data-root", str(self.data), "--project-id", "sample",
        ))
        self.assertEqual(quota["models"]["flash"]["source"], "agy-native:/usage")
        self.assertEqual(quota["models"]["flash"]["used_percent"], 75)

    def test_quota_ignores_a_stale_usage_read(self):
        self.configure()
        with patch.dict(os.environ, {"AGY_TEST_STALE_USAGE": "1"}):
            sample = json.loads(self.cli(
                "quota", "refresh", "--data-root", str(self.data), "--project-id",
                "sample", "--provider", "antigravity", "--model", "flash",
            ))
        self.assertEqual(sample["used_percent"], 75)
        reads = [json.loads(line)["args"][1:2] for line in self.calls.read_text().splitlines()]
        self.assertEqual(reads.count(["/usage"]), 3)

    def test_controller_session_and_client_limits_accept_native_provider(self):
        from continuity_plane.controller_awareness import _bound_controller
        from continuity_plane.controller_lease import acquire
        from continuity_plane.host_capacity import set_limits

        self.cli(
            "session", "bind", "--project-id", "sample", "--task-id", "task",
            "--provider", "antigravity", "--session-id", "test-session",
        )
        self.assertTrue(_bound_controller(self.data, "sample", "test-session"))
        lease = acquire(
            self.data, "sample", owner="test-controller", client_limits={"antigravity": 2},
        )
        self.assertEqual(lease["client_limits"], {"antigravity": 2})
        self.assertEqual(set_limits(self.data, {"antigravity": 3})["limits"], {"antigravity": 3})
