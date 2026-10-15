"""Verify real host event bridges and signed byte accounting in isolated projects."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from continuity_plane.lifecycle import handle_event
from continuity_plane.usage_meter import record, report
from continuity_plane.cli import main
from continuity_plane.executor_routing import recommend


class LifecycleParityTests(unittest.TestCase):
    def test_installed_host_entry_routes_config_before_reading_native_stdin(self):
        from continuity_plane.lifecycle import host_main

        for arguments in (
            ["--config", "/workspace/bridge.json"],
            ["--config=/workspace/bridge.json"],
        ):
            with (
                patch("sys.argv", ["continuity-host-hook", *arguments]),
                patch(
                    "continuity_plane.remote_hook.main", return_value=0
                ) as bridge,
                patch("continuity_plane.lifecycle.json.load") as local_read,
            ):
                self.assertEqual(host_main(), 0)
                bridge.assert_called_once_with(arguments)
                local_read.assert_not_called()

    def test_both_mcp_handshakes_report_the_cli_version(self):
        from continuity_plane.cli import VERSION

        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        for module in ("codex_mcp_server", "context_mcp_server"):
            result = subprocess.run(
                [sys.executable, "-m", f"continuity_plane.{module}"],
                input=request + "\n",
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            reply = json.loads(result.stdout)
            self.assertEqual(reply["result"]["serverInfo"]["version"], VERSION)

    def test_both_mcp_servers_detect_upgrade_without_freezing_project_work(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from continuity_plane import (
            codex_mcp_server,
            context_mcp_server,
            release_version,
        )

        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for server in (codex_mcp_server, context_mcp_server):
            output = StringIO()
            with (
                patch.object(release_version, "_STARTED_PACKAGE_VERSION", "0.1.0a22"),
                patch.object(
                    release_version, "package_version", return_value="0.1.0a23"
                ),
                patch("sys.stdin", StringIO(request + "\n")),
                redirect_stdout(output),
            ):
                self.assertEqual(server.main(), 0)
            reply = json.loads(output.getvalue())
            self.assertIn("restart this MCP process", reply["error"]["message"])
            self.assertIn(
                "ordinary project work remains allowed", reply["error"]["message"]
            )

    def test_same_version_upgrade_detects_changes_in_lazy_planning_modules(self):
        from continuity_plane import release_version

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lazy = root / "card_todo.py"
            lazy.write_text("VALUE = 1\n")
            with patch.object(
                release_version, "__file__", str(root / "release_version.py")
            ):
                original = release_version._code_fingerprint()
                with (
                    patch.object(
                        release_version, "_STARTED_CODE_FINGERPRINT", original
                    ),
                    patch.object(
                        release_version, "_installed_version", return_value=None
                    ),
                ):
                    self.assertIsNone(release_version.runtime_version_issue())
                    lazy.write_text("VALUE = 2\n")
                    self.assertIn(
                        "restart this MCP process",
                        release_version.runtime_version_issue(),
                    )

    def test_saved_return_point_restored_once_each_compaction_on_both_hosts(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "project"
                root.mkdir()
                data = Path(directory) / "data"
                payload = {
                    "cwd": str(root),
                    "session_id": "session-a",
                    "next_action": "Run the pending regression, then implement the failing edge.",
                }
                for _ in range(2):
                    handle_event(
                        {**payload, "hook_event_name": "PreCompact"},
                        host=host,
                        data_root=data,
                    )
                    restored = handle_event(
                        {
                            **payload,
                            "hook_event_name": "SessionStart",
                            "source": "compact",
                        },
                        host=host,
                        data_root=data,
                    )
                    context = restored["hookSpecificOutput"]["additionalContext"]
                    self.assertIn(payload["next_action"], context)
                    self.assertLessEqual(len(context.encode()), 4096)
                    repeated = handle_event(
                        {**payload, "hook_event_name": "PostCompact"},
                        host=host,
                        data_root=data,
                    )
                    self.assertEqual(repeated["injected_bytes"], 0)
                meter = report(root, data_root=data)
                self.assertEqual(meter["calls"], 2)
                self.assertLess(meter["estimated_saved_bytes"], 0)

    def test_no_cross_session_or_wrong_root_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            payload = {
                "cwd": directory,
                "session_id": "owner",
                "next_action": "Private return point",
            }
            handle_event(
                {**payload, "hook_event_name": "PreCompact"},
                host="codex",
                data_root=data,
            )
            other = handle_event(
                {**payload, "session_id": "other"}, host="codex", data_root=data
            )
            self.assertNotIn("Private return point", json.dumps(other))

    def test_byte_net_is_signed_and_concurrent_records_survive(self):
        with tempfile.TemporaryDirectory() as directory:

            def emit(i):
                record(
                    directory,
                    command="lookup",
                    response_bytes=100,
                    estimated_grep_bytes=150,
                    injected_bytes=60,
                    session_id=str(i % 2),
                    data_root=directory,
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(emit, range(24)))
            meter = report(directory, data_root=directory)
            self.assertEqual(meter["calls"], 24)
            self.assertEqual(meter["estimated_saved_bytes"], -240)
            self.assertEqual(meter["hook_injected_bytes"], 1440)
            self.assertEqual(len(meter["by_session"]), 2)

    def test_claude_real_script_handles_precompact_and_compact_events(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "integrations/claude/continuity-plane/scripts/continuity-claude-hook.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            data = Path(directory) / "data"
            from contextlib import redirect_stdout
            from io import StringIO

            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "fixture"])
            env = {
                **os.environ,
                "CONTINUITY_DATA_ROOT": str(data),
                "PYTHONPATH": str(script.parents[4]),
            }
            for event in ("PreCompact", "PostCompact", "SessionStart"):
                result = subprocess.run(
                    [sys.executable, str(script)],
                    input=json.dumps(
                        {
                            "cwd": str(root),
                            "session_id": "real-host",
                            "hook_event_name": event,
                            "source": "compact",
                            "next_action": "Continue precise test",
                        }
                    ),
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if event == "SessionStart":
                    output = json.loads(result.stdout)
                    self.assertEqual(set(output), {"hookSpecificOutput"})
                    injected = len(
                        output["hookSpecificOutput"]["additionalContext"].encode()
                    )
                    self.assertIn(
                        "Continue precise test",
                        output["hookSpecificOutput"]["additionalContext"],
                    )
                elif event == "PostCompact":
                    self.assertEqual(result.stdout, "")
            meter = report(root, data_root=data)
            self.assertEqual(meter["hook_injected_bytes"], injected)
            self.assertIn("checkpoint:session-handoff:create:ok", meter["by_command"])
            self.assertIn("checkpoint:session-handoff:verify:ok", meter["by_command"])

    def test_executor_preferences_match_requested_modes(self):
        self.assertEqual(recommend()["candidates"][:2], ["sonnet", "sol"])
        self.assertEqual(
            recommend(complexity="high", urgency="low")["candidates"][:2],
            ["astra", "opus"],
        )
        self.assertEqual(recommend(urgency="high")["candidates"][0], "grok")
        self.assertEqual(
            recommend(blocked_model="capacity", parallel_safe=True)["candidates"][0],
            "sonnet",
        )

    def test_purpose_effort_and_unavailable_model_are_host_independent(self):
        for controller in ("sonnet", "opus", "sol", "astra"):
            for host in ("codex", "claude"):
                with patch.dict(
                    os.environ,
                    {"CONTINUITY_CONTROLLER": controller, "CONTINUITY_HOST": host},
                ):
                    routine = recommend()
                    self.assertEqual(routine["candidates"][:2], ["sonnet", "sol"])
                    self.assertEqual(routine["thinking_levels"]["glm"], "max")
                    self.assertTrue(
                        all(
                            routine["thinking_levels"][m] == "high"
                            for m in ("sonnet", "sol", "grok")
                        )
                    )
        security = recommend(purpose="security", complexity="high")
        self.assertEqual(security["candidates"][0], "glm")
        review = recommend(purpose="adversarial-review", thinking="max")
        self.assertEqual(review["candidates"][:2], ["astra", "opus"])
        self.assertEqual(review["thinking_levels"]["astra"], "high")
        self.assertEqual(review["thinking_levels"]["opus"], "high")
        self.assertEqual(review["thinking_levels"]["sol"], "max")
        from continuity_plane.executor_routing import _model_alias

        for model in (
            "sonnet-high",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "opus",
            "glm-5.3",
            "grok",
        ):
            value = recommend(blocked_model=model, purpose="adversarial-review")
            self.assertNotIn(_model_alias(model), value["candidates"])
        self.assertEqual(recommend(blocked_model="gpt-6.1-sol")["candidates"][0], "glm")
        self.assertEqual(
            recommend(urgency="high", thinking="max")["thinking_levels"]["grok"], "max"
        )
        for arguments in ({"purpose": "unknown"}, {"thinking": "ultra"}):
            with self.assertRaises(ValueError):
                recommend(**arguments)

    def test_saved_allocations_balance_atomically_and_reads_are_free(self):
        from continuity_plane.executor_routing import save_for_task, for_task
        from continuity_plane.collaboration_registry import (
            register_project,
            add_task,
        )

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            register_project(data, project_id="fixture", control_root=directory)
            for i in range(12):
                add_task(
                    data,
                    project_id="fixture",
                    task_id=f"t{i}",
                    lane_id="scope",
                    mode="patch",
                    priority="p1",
                    title="Bounded change",
                    objective="Test routing",
                    next_action="Run test",
                    exit_criteria=["pass"],
                )

            def allocate(i):
                preference = recommend() if i < 8 else recommend(complexity="high")
                return save_for_task(data, "fixture", f"t{i}", preference)

            with ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(allocate, range(12)))
            primaries = [v["candidates"][0] for v in values]
            for model in ("sonnet", "sol", "grok", "glm", "astra", "opus"):
                self.assertEqual(primaries.count(model), 2)
            path = data / "projects/fixture/executor-routing.json"
            original = path.read_bytes()
            for i in range(12):
                self.assertEqual(for_task(data, "fixture", f"t{i}"), values[i])
                self.assertEqual(allocate(i), values[i])
            self.assertEqual(path.read_bytes(), original)

    def test_four_model_balance_repairs_five_one_one_zero_without_routine_sota(self):
        from collections import Counter
        from continuity_plane.executor_routing import save_for_task
        from continuity_plane.collaboration_registry import (
            register_project,
            add_task,
        )

        with tempfile.TemporaryDirectory() as directory:
            register_project(directory, project_id="fixture", control_root=directory)
            historical = ["sonnet"] * 5 + ["sol", "grok"]
            counts = Counter()
            scenarios = [
                "implementation",
                "quick-triage",
                "security-boundary",
                "regression-verification",
            ]
            for i in range(20):
                add_task(
                    directory,
                    project_id="fixture",
                    task_id=f"t{i}",
                    lane_id="scope",
                    mode="patch",
                    priority="p1",
                    title="Change",
                    objective="Test",
                    next_action="Run",
                    exit_criteria=["pass"],
                )
                preference = (
                    recommend(eligible_models=[historical[i]])
                    if i < 7
                    else recommend(scenario=scenarios[i % 4])
                )
                chosen = save_for_task(directory, "fixture", f"t{i}", preference)
                model = chosen["candidates"][0]
                self.assertNotIn(model, ("astra", "opus"))
                if i >= 7:
                    self.assertEqual(
                        counts[model],
                        min(counts[m] for m in ("sonnet", "sol", "grok", "glm")),
                    )
                    if i == 7:
                        self.assertEqual(model, "glm")
                counts[model] += 1
            self.assertEqual(dict(counts), {"sonnet": 5, "sol": 5, "grok": 5, "glm": 5})

    def test_balance_does_not_override_task_suitability(self):
        self.assertEqual(recommend(eligible_models=["sol"])["candidates"], ["sol"])
        self.assertEqual(
            recommend(purpose="adversarial-review", eligible_models=["opus"])[
                "preferred_pool"
            ],
            ["opus"],
        )
        self.assertEqual(recommend(eligible_models=["astra"])["candidates"], [])

    def test_routing_cli_saves_balanced_return_value(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from continuity_plane.collaboration_registry import (
            register_project,
            add_task,
        )

        with tempfile.TemporaryDirectory() as directory:
            register_project(directory, project_id="fixture", control_root=directory)
            for i, expected in enumerate(("sonnet", "sol")):
                add_task(
                    directory,
                    project_id="fixture",
                    task_id=f"t{i}",
                    lane_id="scope",
                    mode="patch",
                    priority="p1",
                    title="Change",
                    objective="Test",
                    next_action="Run",
                    exit_criteria=["pass"],
                )
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "collaboration",
                                "recommend",
                                "--data-root",
                                directory,
                                "--project-id",
                                "fixture",
                                "--task-id",
                                f"t{i}",
                                "--save",
                                "--purpose",
                                "general",
                                "--thinking",
                                "max",
                            ]
                        ),
                        0,
                    )
                value = json.loads(output.getvalue())
                self.assertEqual(value["candidates"][0], expected)
                self.assertEqual(value["thinking_levels"][expected], "max")

    def test_unsaved_packet_recommendations_do_not_always_prefer_one_model(self):
        from continuity_plane.executor_routing import for_task
        from continuity_plane.intent_todo import compile_queue

        with tempfile.TemporaryDirectory() as directory:
            values = [for_task(directory, "fixture", f"task-{i}") for i in range(32)]
            self.assertEqual(
                {v["candidates"][0] for v in values}, {"sonnet", "sol", "grok", "glm"}
            )
            self.assertEqual(for_task(directory, "fixture", "task-0"), values[0])
            self.assertFalse(
                (Path(directory) / "projects/fixture/executor-routing.json").exists()
            )
            queue = compile_queue(
                directory,
                "1. Implement the focused fix\n2. Run the regression",
                data_root=directory,
            )
            self.assertEqual(
                queue["execution_preferences"]["thinking_levels"]["glm"], "max"
            )
            self.assertEqual(
                queue["execution_preferences"]["thinking_levels"]["sol"], "high"
            )

    def test_scenarios_are_overridable_and_congestion_can_change_pools(self):
        from continuity_plane.executor_routing import SCENARIOS

        for scenario in SCENARIOS:
            value = recommend(scenario=scenario)
            self.assertTrue(value["adaptation"]["preset_is_advisory"])
            self.assertFalse(value["dispatch_authority"])
            self.assertFalse(value["parallel_allowed"])
        self.assertEqual(recommend(scenario="quick-triage")["candidates"][0], "grok")
        self.assertEqual(
            recommend(scenario="deep-design")["candidates"][:2], ["astra", "opus"]
        )
        override = recommend(
            scenario="deep-design", complexity="normal", urgency="normal"
        )
        self.assertEqual(override["candidates"][:2], ["sonnet", "sol"])
        self.assertEqual(recommend(busy_models=["sonnet"])["candidates"][0], "sol")
        switched = recommend(scenario="security-boundary", busy_models=["glm"])
        self.assertEqual(switched["preferred_pool"], ["sonnet", "sol", "grok"])
        self.assertEqual(switched["candidates"][-1], "glm")
        self.assertTrue(switched["ordinary_work_continues"])
        specialists_busy = recommend(
            scenario="deep-design", busy_models=["astra", "opus"]
        )
        self.assertEqual(
            specialists_busy["preferred_pool"], ["sonnet", "sol", "grok", "glm"]
        )
        unavailable = recommend(
            unavailable_models=["sonnet", "gpt-6.1-sol", "glm-5.3", "grok"]
        )
        self.assertEqual(unavailable["candidates"], [])
        self.assertTrue(unavailable["ordinary_work_continues"])
        self.assertIn(
            "non-overlapping-writes", switched["adaptation"]["split_requires"]
        )
        with self.assertRaises(ValueError):
            recommend(scenario="nonexistent")

    def test_scenario_cli_and_saved_congestion_updates(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from continuity_plane.executor_routing import save_for_task
        from continuity_plane.collaboration_registry import (
            register_project,
            add_task,
        )

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["collaboration", "scenarios"]), 0)
        self.assertIn("deep-design", json.loads(output.getvalue())["scenarios"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "collaboration",
                        "recommend",
                        "--scenario",
                        "security-boundary",
                        "--busy-model",
                        "glm",
                        "--unavailable-model",
                        "sonnet",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["candidates"][0], "sol")
        with tempfile.TemporaryDirectory() as directory:
            register_project(directory, project_id="fixture", control_root=directory)
            add_task(
                directory,
                project_id="fixture",
                task_id="switch",
                lane_id="scope",
                mode="patch",
                priority="p1",
                title="Change",
                objective="Test",
                next_action="Run",
                exit_criteria=["pass"],
            )
            initial = save_for_task(directory, "fixture", "switch", recommend())
            self.assertEqual(initial["candidates"][0], "sonnet")
            changed = save_for_task(
                directory, "fixture", "switch", recommend(busy_models=["sonnet"])
            )
            self.assertEqual(changed["candidates"][0], "sol")
            self.assertEqual(
                changed,
                save_for_task(
                    directory, "fixture", "switch", recommend(busy_models=["sonnet"])
                ),
            )

    def test_saved_task_preferences_follow_packet_and_compaction(self):
        from continuity_plane.executor_routing import save_for_task
        from continuity_plane.collaboration_registry import (
            register_project,
            add_task,
        )
        from continuity_plane.collaboration_packet import (
            compose_collaboration_packet,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            data = Path(directory) / "data"
            register_project(data, project_id="fixture", control_root=root)
            add_task(
                data,
                project_id="fixture",
                task_id="deep",
                lane_id="transport",
                mode="patch",
                priority="p1",
                title="Deep investigation",
                objective="Verify a bounded design",
                next_action="Run the design regression",
                exit_criteria=["focused tests pass"],
                assignee="worker",
            )
            save_for_task(
                data,
                "fixture",
                "deep",
                recommend(complexity="high", urgency="low", parallel_safe=True),
            )
            packet = compose_collaboration_packet(
                data, project_id="fixture", assignee="worker"
            )
            self.assertEqual(
                packet["execution_preferences"]["candidates"][:2], ["astra", "opus"]
            )
            self.assertLessEqual(len(json.dumps(packet).encode()), 8192)
            for host in ("codex", "claude"):
                with patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "worker"}):
                    payload = {
                        "cwd": str(root),
                        "session_id": f"worker-{host}",
                        "hook_event_name": "PreCompact",
                    }
                    handle_event(payload, host=host, data_root=data)
                    restored = handle_event(
                        {
                            **payload,
                            "hook_event_name": "SessionStart",
                            "source": "compact",
                        },
                        host=host,
                        data_root=data,
                    )
                context = restored["hookSpecificOutput"]["additionalContext"]
                self.assertIn('"executor_candidates":["astra","opus"', context)
                self.assertIn('"executor_thinking":{"astra":"high"', context)
                self.assertIn("independent scoped subtasks", context)
                self.assertLessEqual(len(context.encode()), 4096)
