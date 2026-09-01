from __future__ import annotations

import json
import importlib.util
import hashlib
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator, ValidationError

from continuity_plane import codex_mcp_server, light_observability
from continuity_plane.cli import main as cli_main
from continuity_plane.light_observability import (
    MAX_OBSERVATION_BYTES,
    MAX_REPORT_FILE_BYTES,
    CORE_OBSERVATION_DIRECTORY,
    CORE_OBSERVATION_SCHEMA_VERSION,
    POLICY_FILENAME,
    POLICY_SCHEMA_VERSION,
    STATE_OBSERVATION_DIRECTORY,
    PolicyConfigError,
    SessionProbe,
    _posix_process_memory,
    append_observation,
    build_observation_report,
    load_policy,
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
        yaml.safe_dump(_project(), sort_keys=False), encoding="utf-8"
    )
    if policy is not None:
        policy_document = {"schema_version": POLICY_SCHEMA_VERSION, **policy}
        (control / POLICY_FILENAME).write_text(
            yaml.safe_dump(policy_document, sort_keys=False), encoding="utf-8"
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


def _stale_source_binding() -> dict:
    binding = codex_mcp_server._binding_from_output(_active_envelope())
    if binding is None:
        raise AssertionError("active binding fixture is invalid")
    binding["source_fresh"] = False
    return binding


def _expired_lease_binding() -> dict:
    binding = codex_mcp_server._binding_from_output(_active_envelope())
    if binding is None:
        raise AssertionError("active binding fixture is invalid")
    binding["lease_valid"] = False
    return binding


def _multiprocess_append_worker(
    data_root: str,
    project_root: str,
    session_id: str,
    worker: int,
    count: int,
) -> None:
    policy = resolve_policy(_project(), environment={})
    for sequence in range(count):
        for _ in range(1000):
            if append_observation(
                data_root=Path(data_root),
                session_id=session_id,
                project_root=Path(project_root),
                policy=policy,
                event_type="state_write_completed",
                success=True,
                extra={"tool_name": f"worker-{worker}-sequence-{sequence}"},
            ):
                break
            time.sleep(0.001)
        else:
            raise RuntimeError("observation append failed")


def _multiprocess_prune_worker(data_root: str, session_id: str) -> None:
    for _ in range(200):
        prune_closed_observations(
            Path(data_root),
            retention_max_bytes=1,
            current_session_id=session_id,
            orphan_after_seconds=0,
        )
        time.sleep(0.001)


def _multiprocess_hold_observation_lock(
    lock_path: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with Path(lock_path).open("a+b") as stream:
        if not light_observability._try_lock_file(stream):
            raise RuntimeError("failed to acquire observation lock")
        ready.set()
        if not release.wait(10):
            raise RuntimeError("lock release signal timed out")
        light_observability._unlock_file(stream)


class PolicyTests(unittest.TestCase):
    def test_policy_and_state_observation_schemas_are_registered(self) -> None:
        root = Path(__file__).parents[1]
        registry_path = root / "schemas/registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registered = {entry["path"]: entry["sha256"] for entry in registry["schemas"]}
        paths = (
            "schemas/m10-16/observability-policy.schema.json",
            "schemas/m10-16/state-mcp-observation.schema.json",
        )
        for relative in paths:
            with self.subTest(schema=relative):
                path = root / relative
                schema = json.loads(path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                self.assertEqual(
                    registered[relative],
                    hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                )

    def test_resolved_policy_matches_independent_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema = json.loads(
            (root / "schemas/m10-16/observability-policy.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(
            resolve_policy(_project(), environment={})
        )

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

    def test_independent_policy_does_not_change_project_profile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sys.stdout", new=StringIO()):
                self.assertEqual(
                    cli_main(["init", "--root", str(root), "--project-id", "sample-app"]),
                    0,
                )
            profile_path = root / ".continuity/project.yaml"
            original_profile = profile_path.read_bytes()
            policy_path = root / ".continuity" / POLICY_FILENAME
            policy_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": POLICY_SCHEMA_VERSION,
                        "preset": "diagnostic",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            with patch("sys.stdout", new=StringIO()):
                self.assertEqual(cli_main(["verify", "--root", str(root)]), 0)
            self.assertEqual(profile_path.read_bytes(), original_profile)
            self.assertEqual(load_policy(root, environment={})["preset"], "diagnostic")
            policy_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": POLICY_SCHEMA_VERSION,
                        "unknown": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PolicyConfigError, "unsupported fields"):
                load_policy(root, environment={})

    def test_legacy_project_policy_migrates_in_memory(self) -> None:
        policy = resolve_policy(
            _project({"preset": "diagnostic"}), environment={}
        )
        self.assertEqual(policy["schema_version"], POLICY_SCHEMA_VERSION)
        self.assertEqual(policy["preset"], "diagnostic")

    def test_legacy_wrapper_is_rejected_outside_project_profile(self) -> None:
        with self.assertRaisesRegex(PolicyConfigError, "unsupported fields"):
            resolve_policy(
                {
                    "schema_version": POLICY_SCHEMA_VERSION,
                    "continuity_policy": {"preset": "diagnostic"},
                },
                environment={},
            )


class ObservationTests(unittest.TestCase):
    def test_report_reuses_alpha10_core_lifecycle_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            live_events = data / CORE_OBSERVATION_DIRECTORY
            live_events.mkdir(parents=True)
            record = {
                "schema_version": CORE_OBSERVATION_SCHEMA_VERSION,
                "event_type": "postcompact",
                "session_sha256": hashlib.sha256(b"core-session").hexdigest(),
                "project_root_sha256": hashlib.sha256(
                    str(root.resolve()).encode()
                ).hexdigest(),
                "success": True,
            }
            (live_events / "core.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )
            report = build_observation_report(root, data_root=data)
            self.assertEqual(report["event_counts"], {"postcompact": 1})

    def test_report_applies_session_limit_after_pairing_core_and_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            project_digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()

            def state_record(session_id: str, event_type: str) -> Path:
                self.assertTrue(
                    append_observation(
                        data_root=data,
                        session_id=session_id,
                        project_root=root,
                        policy=policy,
                        event_type=event_type,
                        success=True,
                    )
                )
                return (
                    data
                    / STATE_OBSERVATION_DIRECTORY
                    / (hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl")
                )

            partial_path = state_record("partial-session", "partial-state")
            paired_state_path = state_record("paired-session", "paired-state")
            paired_digest = hashlib.sha256(b"paired-session").hexdigest()
            core_dir = data / CORE_OBSERVATION_DIRECTORY
            core_dir.mkdir(parents=True)
            paired_core_path = core_dir / f"{paired_digest}.jsonl"
            paired_core_path.write_text(
                json.dumps(
                    {
                        "schema_version": CORE_OBSERVATION_SCHEMA_VERSION,
                        "event_type": "paired-core",
                        "session_sha256": paired_digest,
                        "project_root_sha256": project_digest,
                        "success": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(partial_path, (1, 1))
            os.utime(paired_state_path, (2, 2))
            os.utime(paired_core_path, (2, 2))

            one = build_observation_report(root, data_root=data, session_limit=1)
            self.assertEqual(one["session_groups_selected"], 1)
            self.assertEqual(one["sessions_scanned"], 1)
            self.assertEqual(one["files_scanned"], 2)
            self.assertEqual(one["paired_sessions"], 1)
            self.assertEqual(one["partial_sessions"], 0)
            self.assertEqual(
                one["event_counts"], {"paired-core": 1, "paired-state": 1}
            )
            self.assertFalse(one["provider_usage_available"])
            self.assertNotIn("input_tokens_total", one)
            self.assertNotIn("token_reduction", one)

            two = build_observation_report(root, data_root=data, session_limit=2)
            self.assertEqual(two["session_groups_selected"], 2)
            self.assertEqual(two["sessions_scanned"], 2)
            self.assertEqual(two["files_scanned"], 3)
            self.assertEqual(two["paired_sessions"], 1)
            self.assertEqual(two["partial_sessions"], 1)

    def test_persisted_state_observation_matches_registered_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            self.assertTrue(
                append_observation(
                    data_root=data,
                    session_id="schema-validation",
                    project_root=root,
                    policy=policy,
                    event_type="state_write_completed",
                    success=True,
                    duration_ms=2.5,
                    extra={"tool_name": "continuity_checkpoint:create"},
                )
            )
            record = json.loads(
                next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .strip()
            )
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "schemas/m10-16/state-mcp-observation.schema.json"
                ).read_text(encoding="utf-8")
            )
            Draft202012Validator(schema).validate(record)

    def test_extra_fields_are_allowlisted_and_cannot_override_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            self.assertTrue(
                append_observation(
                    data_root=data,
                    session_id="reserved-fields",
                    project_root=root,
                    policy=policy,
                    event_type="state_write_completed",
                    success=True,
                    extra={
                        "schema_version": "untrusted/v1",
                        "event_type": "state_write_failed",
                        "success": False,
                        "prompt": "must-not-persist",
                        "raw_error": "must-not-persist",
                        "tool_name": "continuity_work_complete",
                        "input_tokens": 12,
                        "measurement_source": "host",
                    },
                )
            )
            record = json.loads(
                next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .strip()
            )
            self.assertEqual(
                record["schema_version"],
                light_observability.OBSERVATION_SCHEMA_VERSION,
            )
            self.assertEqual(record["event_type"], "state_write_completed")
            self.assertTrue(record["success"])
            self.assertNotIn("prompt", record)
            self.assertNotIn("raw_error", record)
            self.assertEqual(record["tool_name"], "continuity_work_complete")
            self.assertEqual(record["input_tokens"], 12)
            self.assertEqual(record["measurement_source"], "host")
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "schemas/m10-16/state-mcp-observation.schema.json"
                ).read_text(encoding="utf-8")
            )
            validator = Draft202012Validator(schema)
            validator.validate(record)
            with self.assertRaises(ValidationError):
                validator.validate({**record, "prompt": "must-not-be-schema-valid"})
            self.assertEqual(
                light_observability._safe_extra({"input_tokens": 99}),
                {},
            )
            self.assertEqual(
                light_observability._safe_extra(
                    {"tool_name": "raw tool response must not persist"}
                ),
                {},
            )
            self.assertEqual(
                light_observability._safe_extra(
                    {"tool_name": "", "latency_buckets": ""}
                ),
                {},
            )

    def test_write_all_retries_short_writes_and_rejects_zero_progress(self) -> None:
        with patch.object(
            light_observability.os, "write", side_effect=(2, 3)
        ) as write:
            light_observability._write_all(9, b"abcde")
        self.assertEqual(bytes(write.call_args_list[0].args[1]), b"abcde")
        self.assertEqual(bytes(write.call_args_list[1].args[1]), b"cde")
        with (
            patch.object(light_observability.os, "write", return_value=0),
            self.assertRaisesRegex(OSError, "incomplete"),
        ):
            light_observability._write_all(9, b"abc")

    def test_failed_partial_write_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})

            def partial_then_fail(descriptor: int, payload: bytes) -> None:
                light_observability.os.write(descriptor, b"{partial")
                raise OSError("simulated short-write failure")

            with patch.object(
                light_observability,
                "_write_all",
                side_effect=partial_then_fail,
            ):
                self.assertFalse(
                    append_observation(
                        data_root=data,
                        session_id="partial-write",
                        project_root=root,
                        policy=policy,
                        event_type="state_write_completed",
                        success=True,
                    )
                )
            path = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
            self.assertEqual(path.stat().st_size, 0)

    def test_in_process_lock_contention_degrades_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            self.assertTrue(light_observability._APPEND_LOCK.acquire())
            started = time.perf_counter()
            try:
                self.assertFalse(
                    append_observation(
                        data_root=data,
                        session_id="thread-lock-busy",
                        project_root=root,
                        policy=policy,
                        event_type="state_write_completed",
                        success=True,
                    )
                )
                self.assertEqual(
                    prune_closed_observations(data, retention_max_bytes=1),
                    0,
                )
            finally:
                light_observability._APPEND_LOCK.release()
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_cross_process_lock_contention_degrades_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            observation_dir = data / STATE_OBSERVATION_DIRECTORY
            observation_dir.mkdir(parents=True)
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_multiprocess_hold_observation_lock,
                args=(str(observation_dir / ".retention.lock"), ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(10))
            started = time.perf_counter()
            try:
                self.assertFalse(
                    append_observation(
                        data_root=data,
                        session_id="process-lock-busy",
                        project_root=root,
                        policy=policy,
                        event_type="state_write_completed",
                        success=True,
                    )
                )
                self.assertEqual(
                    prune_closed_observations(data, retention_max_bytes=1),
                    0,
                )
                elapsed = time.perf_counter() - started
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertLess(elapsed, 0.5)

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
            self.assertFalse((data / STATE_OBSERVATION_DIRECTORY).exists())
            probe.close()
            lines = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl")).read_text(
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
                for line in next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
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
            self.assertFalse((data / STATE_OBSERVATION_DIRECTORY).exists())
            probe.record_call("continuity_resume", duration_ms=1, success=False)
            probe.record_call(
                "continuity_checkpoint:create", duration_ms=1, success=True
            )
            probe.close()
            records = [
                json.loads(line)
                for line in next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
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
            path = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
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
            path = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
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
            names = {
                path.name
                for path in (data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl")
            }
            self.assertIn(
                hashlib.sha256(b"closed-current").hexdigest() + ".jsonl",
                names,
            )
            self.assertIn(
                hashlib.sha256(b"orphan-active").hexdigest() + ".jsonl",
                names,
            )

    def test_retention_removes_old_state_orphan_under_stable_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            append_observation(
                data_root=data,
                session_id="old-state-orphan",
                project_root=root,
                policy=policy,
                event_type="precompact",
                success=True,
            )
            path = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
            old = path.stat().st_mtime - 10
            os.utime(path, (old, old))
            removed = prune_closed_observations(
                data,
                retention_max_bytes=1,
                current_session_id="current-state-session",
                orphan_after_seconds=1,
            )
            self.assertEqual(removed, 1)
            self.assertFalse(path.exists())
            self.assertTrue(
                (data / STATE_OBSERVATION_DIRECTORY / ".retention.lock").exists()
            )

    def test_concurrent_boundary_appends_remain_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            results = [False] * 64

            def write(index: int) -> None:
                for _ in range(1000):
                    results[index] = append_observation(
                        data_root=data,
                        session_id="session-concurrent",
                        project_root=root,
                        policy=policy,
                        event_type="precompact",
                        success=True,
                        extra={"tool_name": f"thread-sequence-{index}"},
                    )
                    if results[index]:
                        return
                    time.sleep(0.001)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(64)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertTrue(all(results))
            lines = next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl")).read_text(
                encoding="utf-8"
            ).splitlines()
            records = [json.loads(line) for line in lines]
            self.assertEqual(len(records), 64)
            self.assertEqual(
                {record["tool_name"] for record in records},
                {f"thread-sequence-{index}" for index in range(64)},
            )

    def test_multiprocess_append_and_prune_share_stable_directory_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
            root.mkdir()
            policy = resolve_policy(_project(), environment={})
            self.assertTrue(
                append_observation(
                    data_root=data,
                    session_id="old-closed-session",
                    project_root=root,
                    policy=policy,
                    event_type="session_end",
                    success=True,
                )
            )
            context = multiprocessing.get_context("spawn")
            session_id = "shared-live-session"
            processes = [
                context.Process(
                    target=_multiprocess_append_worker,
                    args=(str(data), str(root), session_id, worker, 30),
                )
                for worker in range(4)
            ]
            processes.append(
                context.Process(
                    target=_multiprocess_prune_worker,
                    args=(str(data), session_id),
                )
            )
            for process in processes:
                process.start()
            for process in processes:
                process.join(30)
                self.assertEqual(process.exitcode, 0)
            directory_path = data / STATE_OBSERVATION_DIRECTORY
            shared_path = directory_path / (
                hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl"
            )
            records = [
                json.loads(line)
                for line in shared_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 120)
            self.assertEqual(
                {record["tool_name"] for record in records},
                {
                    f"worker-{worker}-sequence-{sequence}"
                    for worker in range(4)
                    for sequence in range(30)
                },
            )
            old_path = directory_path / (
                hashlib.sha256(b"old-closed-session").hexdigest() + ".jsonl"
            )
            self.assertFalse(old_path.exists())
            self.assertTrue((directory_path / ".retention.lock").exists())


class MCPProbeTests(unittest.TestCase):
    def test_cli_commands_use_current_interpreter_without_path_lookup(self) -> None:
        self.assertEqual(
            codex_mcp_server._cli_command("resume", "--root", "project"),
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "resume",
                "--root",
                "project",
            ],
        )

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
            self.assertFalse((data / STATE_OBSERVATION_DIRECTORY).exists())

    def test_invalid_request_shapes_return_json_rpc_errors(self) -> None:
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
            {"jsonrpc": "2.0", "id": 4, "method": "initialize"},
        ]
        stdin = StringIO("".join(json.dumps(item) + "\n" for item in requests))
        stdout = StringIO()
        with (
            patch.object(codex_mcp_server.sys, "stdin", stdin),
            patch.object(codex_mcp_server.sys, "stdout", stdout),
        ):
            self.assertEqual(codex_mcp_server.main(), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 1)
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertEqual(responses[3]["error"]["code"], -32602)
        self.assertEqual(responses[4]["id"], 4)
        self.assertEqual(responses[4]["error"]["code"], -32602)

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

    def test_successful_state_write_refreshes_binding_before_and_after(self) -> None:
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
                    return_value=codex_mcp_server._binding_from_output(
                        _active_envelope()
                    ),
                ) as refresh_binding,
                patch.dict(os.environ, {"PLUGIN_DATA": str(base / "data")}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            self.assertEqual(refresh_binding.call_count, 2)
            refresh_binding.assert_called_with(root.resolve())

    def test_checkpoint_create_rejects_binding_that_became_stale(self) -> None:
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
                        "arguments": {"root": str(root), "action": "create"},
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
                    return_value=subprocess.CompletedProcess(
                        [], 0, _active_envelope(), ""
                    ),
                ) as run_cli,
                patch.object(
                    codex_mcp_server, "_binding", return_value=_stale_source_binding()
                ),
                patch.dict(os.environ, {"PLUGIN_DATA": str(base / "data")}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(run_cli.call_count, 1)
            self.assertEqual(responses[1]["error"]["code"], -32002)

    def test_work_complete_rejects_binding_that_became_stale(self) -> None:
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
                    return_value=subprocess.CompletedProcess(
                        [], 0, _active_envelope(), ""
                    ),
                ) as run_cli,
                patch.object(
                    codex_mcp_server, "_binding", return_value=_expired_lease_binding()
                ),
                patch.dict(os.environ, {"PLUGIN_DATA": str(base / "data")}, clear=False),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(run_cli.call_count, 1)
            self.assertEqual(responses[1]["error"]["code"], -32002)

    def test_invalid_optional_policy_degrades_observation_but_resume_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            data = base / "data"
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
                patch.object(
                    codex_mcp_server,
                    "_run_cli_with_retry",
                    return_value=subprocess.CompletedProcess(
                        [], 0, _idle_envelope(), ""
                    ),
                ) as run_cli,
                patch.dict(
                    os.environ,
                    {
                        "CONTINUITY_EFFECT_POLICY": "auto",
                        "PLUGIN_DATA": str(data),
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(codex_mcp_server.main(), 0)
            run_cli.assert_called_once()
            response = json.loads(stdout.getvalue())
            self.assertFalse(response["result"]["isError"])
            records = [
                json.loads(line)
                for line in next((data / STATE_OBSERVATION_DIRECTORY).glob("*.jsonl"))
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["event_type"] for record in records],
                ["policy_degraded", "session_end"],
            )
            self.assertTrue(records[0]["observation_degraded"])
            self.assertTrue(records[-1]["observation_degraded"])

    def test_invalid_policy_is_rejected_only_in_explicit_strict_mode(self) -> None:
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
            stdout = StringIO()
            with (
                patch.object(
                    codex_mcp_server.sys,
                    "stdin",
                    StringIO(json.dumps(request) + "\n"),
                ),
                patch.object(codex_mcp_server.sys, "stdout", stdout),
                patch.object(codex_mcp_server, "_run_cli_with_retry") as run_cli,
                patch.dict(
                    os.environ,
                    {"CONTINUITY_EFFECT_POLICY": "strict"},
                    clear=False,
                ),
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

    def test_core_manifest_registers_lifecycle_hooks_only(self) -> None:
        root = Path(__file__).parents[1]
        path = root / "plugins/continuity-plane/hooks/hooks.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest["hooks"]), {"SessionStart", "PreCompact", "PostCompact"}
        )
        self.assertNotIn("PreToolUse", manifest["hooks"])
        self.assertNotIn("PostToolUse", manifest["hooks"])

    def test_alpha10_core_and_state_plugin_ownership_remains_split(self) -> None:
        root = Path(__file__).parents[1]
        core = root / "plugins/continuity-plane"
        search = root / "plugins/continuity-plane-search"
        state = root / "plugins/continuity-plane-state"
        core_manifest = json.loads(
            (core / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        state_manifest = json.loads(
            (state / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        search_manifest = json.loads(
            (search / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("mcpServers", core_manifest)
        self.assertNotIn("mcpServers", search_manifest)
        self.assertFalse((core / ".mcp.json").exists())
        self.assertFalse((search / ".mcp.json").exists())
        self.assertIn("mcpServers", state_manifest)
        self.assertTrue((state / ".mcp.json").is_file())
        self.assertFalse((core / "skills/continuity-plane-state").exists())

    def test_state_plugin_script_forwards_to_the_package_server_contract(self) -> None:
        root = Path(__file__).parents[1]
        state = root / "plugins/continuity-plane-state"
        script = state / "scripts/continuity-mcp-server.py"
        source = script.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 10)
        self.assertIn("from continuity_plane.codex_mcp_server import main", source)
        self.assertNotIn("subprocess", source)
        mcp_config = json.loads((state / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(
            mcp_config["mcpServers"]["continuity"],
            {"command": "continuity-mcp", "args": []},
        )
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        ) + "\n"
        outputs = []
        for command in (
            [sys.executable, str(script)],
            [sys.executable, "-m", "continuity_plane.codex_mcp_server"],
        ):
            completed = subprocess.run(
                command,
                cwd=root,
                input=request,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            outputs.append(json.loads(completed.stdout))
        self.assertEqual(outputs[0], outputs[1])

        release = json.loads(
            (root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )
        entries = {entry["path"]: entry["sha256"] for entry in release["files"]}
        relative = "plugins/continuity-plane-state/scripts/continuity-mcp-server.py"
        self.assertEqual(
            entries[relative],
            hashlib.sha256(script.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        )

    def test_auto_lifecycle_failures_never_stop_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            hook = self._load_hook()
            payload = {"session_id": "auto-failure", "source": "startup"}
            failed = subprocess.CompletedProcess([], 1, "", "failed")
            stdout = StringIO()
            with (
                patch.dict(
                    os.environ, {"CONTINUITY_EFFECT_POLICY": "auto"}, clear=False
                ),
                patch.object(hook.sys, "stdout", stdout),
                patch.object(hook, "_command", return_value=failed),
                patch.object(hook, "_observe"),
                patch.object(hook, "_write_cursor", return_value=None),
                patch.object(hook, "_skill_lock_path", return_value=None),
                patch.object(hook, "_cursor_path", return_value=None),
            ):
                self.assertEqual(hook._precompact(payload, root), 0)
                self.assertEqual(hook._postcompact(payload, root), 0)
                self.assertEqual(hook._session_start(payload, root), 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_observe_lifecycle_does_not_execute_checkpoint_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            hook = self._load_hook()
            payload = {"session_id": "observe-only", "source": "compact"}
            with (
                patch.dict(
                    os.environ, {"CONTINUITY_EFFECT_POLICY": "observe"}, clear=False
                ),
                patch.object(hook, "_command") as command,
            ):
                self.assertEqual(hook._precompact(payload, root), 0)
                self.assertEqual(hook._postcompact(payload, root), 0)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
