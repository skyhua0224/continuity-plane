from __future__ import annotations

import json
import importlib.util
import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

from continuity_plane import codex_mcp_server
from continuity_plane.cli import main as cli_main
from continuity_plane.light_observability import (
    MAX_OBSERVATION_BYTES,
    MAX_REPORT_FILE_BYTES,
    PolicyConfigError,
    SessionProbe,
    _posix_process_memory,
    append_observation,
    build_observation_report,
    policy_sha256,
    prune_closed_observations,
    resolve_policy,
)


def _project(policy: object | None = None) -> dict:
    document = {
        "schema_version": "context.project/v1alpha1",
        "project_id": "sample-app",
        "display_name": "Sample App",
        "runtime_profile": "local-embedded",
        "state_store": {
            "adapter": "sqlite",
            "path": ".continuity/state.sqlite3",
        },
        "collaboration": {"mode": "solo", "shared_state": False},
        "governance": {
            "master": ".continuity/MASTER.md",
            "status": ".continuity/STATUS.current.md",
        },
        "authority": {"mode": "local"},
    }
    if policy is not None:
        document["continuity_policy"] = policy
    return document


def _write_project(root: Path, policy: object | None = None) -> None:
    control = root / ".continuity"
    control.mkdir(parents=True)
    (control / "project.yaml").write_text(
        yaml.safe_dump(_project(policy), sort_keys=False), encoding="utf-8"
    )


def _idle_envelope() -> str:
    return json.dumps(
        {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "sample-app",
            "active_work": None,
            "claim": None,
            "read_only": False,
            "source_fresh": True,
            "checkpoint_verified": True,
            "lease_valid": True,
            "next_action": "activate-next-work",
        }
    )


def _active_envelope() -> str:
    return json.dumps(
        {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "sample-app",
            "active_work": {"work_id": "work-1"},
            "claim": {"claim_id": "claim-1", "actor_ref": "actor-1"},
            "read_only": False,
            "source_fresh": True,
            "checkpoint_verified": True,
            "lease_valid": True,
            "next_action": "continue-active-work",
        }
    )


class PolicyTests(unittest.TestCase):
    def test_old_profile_uses_balanced_defaults(self) -> None:
        policy = resolve_policy(_project(), environment={})
        self.assertEqual(policy["preset"], "balanced")
        self.assertEqual(policy["observability"]["mode"], "minimal")
        self.assertTrue(policy["checkpoint"]["on_pre_compact"])
        self.assertEqual(policy["verification"]["deep_verify"], "manual")

    def test_preset_override_and_temporary_diagnostic_mode(self) -> None:
        policy = resolve_policy(
            _project(
                {
                    "preset": "reliability-first",
                    "checkpoint": {"min_interval_seconds": 15},
                }
            ),
            environment={"CONTINUITY_OBSERVABILITY_MODE": "diagnostic"},
        )
        self.assertEqual(policy["preset"], "reliability-first")
        self.assertEqual(policy["checkpoint"]["min_interval_seconds"], 15)
        self.assertTrue(policy["checkpoint"]["after_state_writes"])
        self.assertEqual(policy["observability"]["mode"], "diagnostic")

    def test_invalid_fields_bounds_and_safety_floor_are_rejected(self) -> None:
        invalid = (
            {"unknown": True},
            {"checkpoint": {"min_interval_seconds": -1}},
            {"checkpoint": {"on_pre_compact": False}},
            {"checkpoint": {"on_work_complete": False}},
            {"observability": {"slow_call_threshold_ms": True}},
            {"verification": {"startup_scope": "off"}},
            {1: "non-string-key"},
        )
        for configured in invalid:
            with self.subTest(configured=configured):
                with self.assertRaises(PolicyConfigError):
                    resolve_policy(_project(configured), environment={})

    def test_policy_digest_is_stable_across_input_order(self) -> None:
        left = resolve_policy(_project(), environment={})
        right = {key: left[key] for key in reversed(left)}
        self.assertEqual(policy_sha256(left), policy_sha256(right))

    def test_cli_accepts_optional_policy_and_rejects_invalid_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sys.stdout", new=StringIO()):
                self.assertEqual(
                    cli_main(["init", "--root", str(root), "--project-id", "sample-app"]),
                    0,
                )
            profile_path = root / ".continuity/project.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile["continuity_policy"] = {"preset": "diagnostic"}
            profile_path.write_text(
                yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
            )
            with patch("sys.stdout", new=StringIO()):
                self.assertEqual(cli_main(["verify", "--root", str(root)]), 0)
            profile["continuity_policy"] = {
                "checkpoint": {"on_work_complete": False}
            }
            profile_path.write_text(
                yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "required authority boundary"):
                cli_main(["verify", "--root", str(root)])


class ObservationTests(unittest.TestCase):
    def test_linux_rss_uses_current_resident_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            statm = Path(directory) / "statm"
            statm.write_text("100 5 2 1 0 0 0\n", encoding="ascii")
            with (
                patch(
                    "continuity_plane.light_observability.sys.platform", "linux"
                ),
                patch(
                    "continuity_plane.light_observability.os.sysconf",
                    return_value=4096,
                    create=True,
                ),
            ):
                self.assertEqual(
                    _posix_process_memory(statm_path=statm),
                    {"rss_bytes": 5 * 4096},
                )

    def test_minimal_reads_stay_in_memory_until_session_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            probe = SessionProbe(
                root, policy, data_root=data, session_id="session-minimal"
            )
            probe.record_call(
                "continuity_resume",
                duration_ms=10,
                success=True,
                request_bytes=10,
                response_bytes=20,
            )
            self.assertFalse((data / "live-events").exists())
            probe.close()
            lines = next((data / "live-events").glob("*.jsonl")).read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(lines), 1)
            summary = json.loads(lines[0])
            self.assertEqual(summary["event_type"], "session_end")
            self.assertEqual(summary["read_calls"], 1)

    def test_write_failure_slow_call_and_diagnostic_read_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            configured = _project({"preset": "diagnostic"})
            probe = SessionProbe(
                root,
                resolve_policy(configured, environment={}),
                data_root=data,
                session_id="session-diagnostic",
            )
            probe.record_call(
                "continuity_resume", duration_ms=5, success=True
            )
            probe.record_call(
                "continuity_checkpoint:create", duration_ms=7, success=False
            )
            probe.close()
            records = [
                json.loads(line)
                for line in next((data / "live-events").glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["event_type"] for record in records],
                ["tool_call", "state_write_failed", "session_end"],
            )
            self.assertIn("rss_bytes", records[0])
            self.assertIn("state_store_bytes", records[0])
            self.assertTrue(all(len((json.dumps(record) + "\n").encode()) <= MAX_OBSERVATION_BYTES for record in records))

    def test_every_call_resume_policy_does_not_flag_expected_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            policy = resolve_policy(
                _project({"resume": {"explicit_policy": "every_call"}}),
                environment={},
            )
            probe = SessionProbe(
                root, policy, data_root=base / "data", session_id="resume-every-call"
            )
            probe.record_call("continuity_resume", duration_ms=1, success=True)
            probe.record_call("continuity_resume", duration_ms=1, success=True)
            self.assertEqual(probe.duplicate_resumes, 0)
            probe.close()

    def test_disabled_probes_keep_only_mandatory_records_and_close_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(
                _project(
                    {
                        "observability": {
                            "probes_enabled": False,
                            "resource_sampling": "every_call",
                        }
                    }
                ),
                environment={},
            )
            probe = SessionProbe(
                root, policy, data_root=data, session_id="disabled-probes"
            )
            probe.record_call("continuity_resume", duration_ms=5000, success=True)
            self.assertFalse((data / "live-events").exists())
            probe.record_call("continuity_resume", duration_ms=1, success=False)
            probe.record_call(
                "continuity_checkpoint:create", duration_ms=1, success=True
            )
            probe.close()
            records = [
                json.loads(line)
                for line in next((data / "live-events").glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["event_type"] for record in records],
                ["tool_call_failed", "state_write_completed", "session_end"],
            )
            self.assertFalse(any("rss_bytes" in record for record in records))
            self.assertEqual(records[-1]["read_calls"], 0)
            self.assertEqual(records[-1]["write_calls"], 0)

    def test_observation_io_failure_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            not_a_directory = base / "data"
            not_a_directory.write_text("occupied", encoding="utf-8")
            policy = resolve_policy(_project(), environment={})
            self.assertFalse(
                append_observation(
                    data_root=not_a_directory,
                    session_id="session-io-error",
                    project_root=root,
                    policy=policy,
                    event_type="precompact",
                    success=True,
                )
            )
            probe = SessionProbe(
                root,
                policy,
                data_root=not_a_directory,
                session_id="session-io-error",
            )
            probe.boundary("precompact", success=True)
            self.assertTrue(probe.degraded)

    def test_report_tolerates_corruption_and_suggests_from_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            append_observation(
                data_root=data,
                session_id="session-report",
                project_root=root,
                policy=policy,
                event_type="resume",
                success=True,
                extra={"duplicate_resumes": 1},
            )
            path = next((data / "live-events").glob("*.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write("{broken\n")
            report = build_observation_report(root, data_root=data)
            self.assertEqual(report["corrupt_lines"], 1)
            self.assertEqual(report["duplicate_resumes"], 1)
            self.assertTrue(report["suggestions"])

    def test_report_reads_only_bounded_tail_of_large_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            append_observation(
                data_root=data,
                session_id="session-large-report",
                project_root=root,
                policy=policy,
                event_type="session_end",
                success=True,
            )
            path = next((data / "live-events").glob("*.jsonl"))
            valid_tail = path.read_bytes()
            path.write_bytes(b"x" * MAX_REPORT_FILE_BYTES + b"\n" + valid_tail)
            report = build_observation_report(root, data_root=data)
            self.assertEqual(report["truncated_files"], 1)
            self.assertEqual(report["event_counts"]["session_end"], 1)

    def test_retention_removes_only_closed_non_current_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            for session_id in ("closed-old", "closed-current"):
                probe = SessionProbe(
                    root, policy, data_root=data, session_id=session_id
                )
                probe.close()
            append_observation(
                data_root=data,
                session_id="orphan-active",
                project_root=root,
                policy=policy,
                event_type="precompact",
                success=True,
            )
            removed = prune_closed_observations(
                data,
                retention_max_bytes=1,
                current_session_id="closed-current",
            )
            self.assertEqual(removed, 1)
            names = {path.name for path in (data / "live-events").glob("*.jsonl")}
            self.assertIn(
                hashlib.sha256(b"closed-current").hexdigest() + ".jsonl",
                names,
            )
            self.assertIn(
                hashlib.sha256(b"orphan-active").hexdigest() + ".jsonl",
                names,
            )

    def test_retention_removes_old_unlocked_hook_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            append_observation(
                data_root=data,
                session_id="old-hook-orphan",
                project_root=root,
                policy=policy,
                event_type="precompact",
                success=True,
            )
            path = next((data / "live-events").glob("*.jsonl"))
            old = path.stat().st_mtime - 10
            os.utime(path, (old, old))
            removed = prune_closed_observations(
                data,
                retention_max_bytes=1,
                current_session_id="current-hook",
                orphan_after_seconds=1,
            )
            self.assertEqual(removed, 1)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".lock").exists())

    def test_concurrent_boundary_appends_remain_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            results = [False] * 64

            def write(index: int) -> None:
                results[index] = append_observation(
                    data_root=data,
                    session_id="session-concurrent",
                    project_root=root,
                    policy=policy,
                    event_type="precompact",
                    success=True,
                    extra={"sequence": index},
                )

            threads = [threading.Thread(target=write, args=(index,)) for index in range(64)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertTrue(all(results))
            lines = next((data / "live-events").glob("*.jsonl")).read_text(
                encoding="utf-8"
            ).splitlines()
            records = [json.loads(line) for line in lines]
            self.assertEqual(len(records), 64)
            self.assertEqual({record["sequence"] for record in records}, set(range(64)))


class MCPProbeTests(unittest.TestCase):
    def test_disabled_probes_do_not_persist_successful_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            _write_project(
                root, {"observability": {"probes_enabled": False}}
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": str(root)},
                },
            }
            with (
                patch.object(
                    codex_mcp_server.sys,
                    "stdin",
                    StringIO(json.dumps(request) + "\n"),
                ),
                patch.object(codex_mcp_server.sys, "stdout", StringIO()),
                patch.object(
                    codex_mcp_server,
                    "_run_cli_with_retry",
                    return_value=subprocess.CompletedProcess(
                        [], 0, _idle_envelope(), ""
                    ),
                ),
                patch.dict(os.environ, {"PLUGIN_DATA": str(data)}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            self.assertFalse((data / "live-events").exists())

    def test_non_object_requests_are_ignored_or_rejected_without_crashing(self) -> None:
        requests = [
            [],
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "continuity_resume", "arguments": []},
            },
        ]
        stdin = StringIO("".join(json.dumps(item) + "\n" for item in requests))
        stdout = StringIO()
        with (
            patch.object(codex_mcp_server.sys, "stdin", stdin),
            patch.object(codex_mcp_server.sys, "stdout", stdout),
        ):
            self.assertEqual(codex_mcp_server.main(), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(responses[2]["error"]["code"], -32602)

    def test_mcp_records_duplicate_resume_and_closes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            _write_project(root)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_resume",
                        "arguments": {"root": str(root)},
                    },
                }
                for index in (1, 2)
            ]
            stdin = StringIO("".join(json.dumps(item) + "\n" for item in requests))
            stdout = StringIO()
            completed = subprocess.CompletedProcess(
                ["continuity", "resume"], 0, _idle_envelope(), ""
            )
            with (
                patch.object(codex_mcp_server.sys, "stdin", stdin),
                patch.object(codex_mcp_server.sys, "stdout", stdout),
                patch.object(
                    codex_mcp_server,
                    "_run_cli_with_retry",
                    return_value=completed,
                ),
                patch.dict(os.environ, {"PLUGIN_DATA": str(data)}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            report = build_observation_report(root, data_root=data)
            self.assertEqual(report["event_counts"]["resume"], 2)
            self.assertEqual(report["event_counts"]["session_end"], 1)
            self.assertEqual(report["duplicate_resumes"], 1)

    def test_bound_read_reuses_cached_binding_without_internal_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            _write_project(root)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_resume",
                        "arguments": {"root": str(root)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_checkpoint",
                        "arguments": {"root": str(root), "action": "verify"},
                    },
                },
            ]
            outputs = [
                subprocess.CompletedProcess([], 0, _idle_envelope(), ""),
                subprocess.CompletedProcess([], 0, '{"status":"verified"}', ""),
            ]
            with (
                patch.object(
                    codex_mcp_server.sys,
                    "stdin",
                    StringIO("".join(json.dumps(item) + "\n" for item in requests)),
                ),
                patch.object(codex_mcp_server.sys, "stdout", StringIO()),
                patch.object(
                    codex_mcp_server, "_run_cli_with_retry", side_effect=outputs
                ) as run_cli,
                patch.object(codex_mcp_server, "_binding") as internal_binding,
                patch.dict(os.environ, {"PLUGIN_DATA": str(data)}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            self.assertEqual(run_cli.call_count, 2)
            internal_binding.assert_not_called()

    def test_invalid_resume_envelope_does_not_establish_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            _write_project(root)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_resume",
                        "arguments": {"root": str(root)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_checkpoint",
                        "arguments": {"root": str(root), "action": "verify"},
                    },
                },
            ]
            stdout = StringIO()
            with (
                patch.object(
                    codex_mcp_server.sys,
                    "stdin",
                    StringIO("".join(json.dumps(item) + "\n" for item in requests)),
                ),
                patch.object(codex_mcp_server.sys, "stdout", stdout),
                patch.object(
                    codex_mcp_server,
                    "_run_cli_with_retry",
                    return_value=subprocess.CompletedProcess([], 0, "{}", ""),
                ),
                patch.dict(
                    os.environ, {"PLUGIN_DATA": str(base / "data")}, clear=False
                ),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertTrue(responses[0]["result"]["isError"])
            self.assertEqual(responses[1]["error"]["code"], -32001)

    def test_successful_state_write_refreshes_binding_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            _write_project(root)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_resume",
                        "arguments": {"root": str(root)},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_work_complete",
                        "arguments": {
                            "root": str(root),
                            "work_id": "work-1",
                            "claim_id": "claim-1",
                            "actor_ref": "actor-1",
                            "evidence_files": ["evidence.json"],
                        },
                    },
                },
            ]
            outputs = [
                subprocess.CompletedProcess([], 0, _active_envelope(), ""),
                subprocess.CompletedProcess([], 0, '{"status":"completed"}', ""),
            ]
            with (
                patch.object(
                    codex_mcp_server.sys,
                    "stdin",
                    StringIO("".join(json.dumps(item) + "\n" for item in requests)),
                ),
                patch.object(codex_mcp_server.sys, "stdout", StringIO()),
                patch.object(
                    codex_mcp_server, "_run_cli_with_retry", side_effect=outputs
                ),
                patch.object(
                    codex_mcp_server,
                    "_binding",
                    return_value=json.loads(_idle_envelope()),
                ) as refresh_binding,
                patch.dict(os.environ, {"PLUGIN_DATA": str(base / "data")}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            refresh_binding.assert_called_once_with(root.resolve())

    def test_invalid_policy_fails_closed_before_cli_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            _write_project(root, {"checkpoint": {"on_pre_compact": False}})
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": str(root)},
                },
            }
            stdin = StringIO(json.dumps(request) + "\n")
            stdout = StringIO()
            with (
                patch.object(codex_mcp_server.sys, "stdin", stdin),
                patch.object(codex_mcp_server.sys, "stdout", stdout),
                patch.object(codex_mcp_server, "_run_cli_with_retry") as run_cli,
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            run_cli.assert_not_called()
            response = json.loads(stdout.getvalue())
            self.assertEqual(response["error"]["code"], -32004)


class HookProbeTests(unittest.TestCase):
    @staticmethod
    def _load_hook():
        path = (
            Path(__file__).parents[1]
            / "plugins/continuity-plane/scripts/continuity-hook.py"
        )
        spec = importlib.util.spec_from_file_location("continuity_hook_test", path)
        if spec is None or spec.loader is None:
            raise AssertionError("hook module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_minimal_hook_skips_successful_tool_detail_but_keeps_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            _write_project(root)
            hook = self._load_hook()
            payload = {"session_id": "hook-session", "turn_id": "turn-1"}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}, clear=False):
                hook._observe(
                    payload,
                    root,
                    event_type="pretooluse",
                    success=True,
                    decision="allow",
                )
                self.assertFalse((data / "live-events").exists())
                hook._observe(
                    payload,
                    root,
                    event_type="pretooluse",
                    success=False,
                    decision="deny-conflict",
                )
            record = json.loads(
                next((data / "live-events").glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .strip()
            )
            self.assertEqual(record["event_type"], "pretooluse")
            self.assertFalse(record["success"])

    def test_session_start_hook_runs_bounded_orphan_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            _write_project(root)
            hook = self._load_hook()
            payload = {"session_id": "hook-retention", "source": "startup"}
            with (
                patch.dict(os.environ, {"PLUGIN_DATA": str(data)}, clear=False),
                patch.object(hook, "prune_closed_observations") as prune,
            ):
                hook._observe(
                    payload,
                    root,
                    event_type="session-start",
                    success=True,
                )
            prune.assert_called_once_with(
                data,
                retention_max_bytes=64 * 1024 * 1024,
                current_session_id="hook-retention",
                orphan_after_seconds=24 * 60 * 60,
            )

    def test_invalid_hook_policy_stops_before_lifecycle_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            _write_project(root, {"verification": {"startup_scope": "off"}})
            hook = self._load_hook()
            payload = {
                "cwd": str(root),
                "session_id": "hook-invalid-policy",
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
            stdin = StringIO(json.dumps(payload))
            stdout = StringIO()
            with (
                patch.object(hook.sys, "stdin", stdin),
                patch.object(hook.sys, "stdout", stdout),
                patch.object(hook, "_command") as command,
            ):
                self.assertEqual(hook.main(), 0)
            command.assert_not_called()
            response = json.loads(stdout.getvalue())
            self.assertFalse(response["continue"])
            self.assertIn("policy is invalid", response["stopReason"])

    def test_hook_package_version_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            _write_project(root)
            hook = self._load_hook()
            payload = {
                "cwd": str(root),
                "session_id": "hook-version-mismatch",
                "hook_event_name": "PreCompact",
            }
            stdout = StringIO()
            with (
                patch.object(hook.sys, "stdin", StringIO(json.dumps(payload))),
                patch.object(hook.sys, "stdout", stdout),
                patch.object(hook, "load_policy", None),
                patch.object(hook, "append_observation", None),
                patch.object(hook, "_command") as command,
            ):
                self.assertEqual(hook.main(), 0)
            command.assert_not_called()
            self.assertIn("versions do not match", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
