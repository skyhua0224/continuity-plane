"""Host/profile identity and recovery-storage boundaries; no real account calls."""

import sys
import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import UTC, datetime, timedelta

from tests import test_adoption_workflows


def origin(host="a", profile="b", provider="codex"):
    return {
        "provider": provider,
        "host_sha256": host * 64,
        "profile_sha256": profile * 64,
    }


class RuntimeBoundaryTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def test_foreign_origin_quota_never_becomes_local_availability(self):
        from continuity_plane.subscription_quota import record, report

        with patch(
            "continuity_plane.runtime_origin.current", return_value=origin()
        ):
            record(
                self.data,
                "sample",
                "sol",
                used_percent=100,
                resets_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                source="test-only:native",
                official_subscription=True,
            )
            self.assertEqual(
                report(self.data, "sample")["models"]["sol"]["status"], "exhausted"
            )
        with patch(
            "continuity_plane.runtime_origin.current", return_value=origin("c")
        ):
            result = report(self.data, "sample")
            self.assertEqual(result["models"], {})
            self.assertEqual(result["other_or_unscoped_origins_ignored"], 1)

    def test_other_profile_account_query_is_rejected_before_subprocess(self):
        from continuity_plane.codex_account_probe import read_limits

        with (
            patch(
                "continuity_plane.runtime_origin.current", return_value=origin()
            ),
            patch("subprocess.Popen") as spawn,
        ):
            with self.assertRaisesRegex(ValueError, "owning host and profile"):
                read_limits(expected_origin=origin(profile="c"))
            spawn.assert_not_called()

    def test_relative_native_profile_cannot_change_identity_with_worker_cwd(self):
        from continuity_plane.runtime_origin import current
        from continuity_plane.codex_account_probe import read_limits

        with (
            patch.dict("os.environ", {"CODEX_HOME": ".codex"}),
            patch("subprocess.Popen") as spawn,
        ):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                read_limits()
            spawn.assert_not_called()
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "~/.claude-other"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                current("claude")

    def test_claude_quota_never_silently_queries_codex_account(self):
        from continuity_plane.subscription_quota import refresh

        with patch("subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "never substitute a Codex account"):
                refresh(self.data, "sample", "sonnet", provider="claude")
            spawn.assert_not_called()

    def test_adapter_cannot_be_moved_to_another_host_or_profile(self):
        from continuity_plane.worker_dispatch import configure, dispatch

        with patch(
            "continuity_plane.runtime_origin.current",
            return_value=origin(provider="custom"),
        ):
            configure(
                self.data,
                model="sol",
                provider="custom",
                argv=[sys.executable, "-c", "pass"],
            )
        with patch(
            "continuity_plane.runtime_origin.current",
            return_value=origin("c", provider="custom"),
        ):
            with self.assertRaisesRegex(ValueError, "runtime host/profile mismatch"):
                dispatch(self.root, data_root=self.data, task_id="task", model="sol")

    def test_adapter_configuration_does_not_persist_secret_arguments(self):
        from continuity_plane.worker_dispatch import configure

        with self.assertRaisesRegex(ValueError, "must not store credentials"):
            configure(
                self.data,
                model="sol",
                provider="custom",
                argv=["client", "--api-key", "not-a-real-secret"],
            )
        self.assertFalse((self.data / "executor-adapters.json").exists())

    def test_custom_native_wrapper_tracks_the_declared_profile_family(self):
        from continuity_plane.worker_dispatch import configure, dispatch

        first = configure(
            self.data,
            model="sonnet",
            provider="custom",
            profile_provider="claude",
            argv=[sys.executable, "-c", "pass"],
        )
        self.assertEqual(first["execution_origin"]["provider"], "claude")
        with patch.dict(
            "os.environ",
            {"CLAUDE_CONFIG_DIR": str(self.root / "another-claude-profile")},
        ):
            with self.assertRaisesRegex(ValueError, "runtime host/profile mismatch"):
                dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        from continuity_plane.subscription_quota import refresh

        with patch("subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "another native profile family"):
                refresh(self.data, "sample", "sonnet", provider="codex")
            spawn.assert_not_called()

    def test_foreign_usage_is_labeled_and_deduplicated_not_used_as_auth(self):
        from continuity_plane.provider_usage import import_snapshot, report

        snapshot = {
            "provider": "claude",
            "session": "f" * 64,
            "collection_origin": origin("c", provider="claude"),
            "tokens": {"input_tokens": 10, "output_tokens": 2},
        }
        import_snapshot(self.root, snapshot, self.data)
        import_snapshot(self.root, snapshot, self.data)
        result = report(self.root, self.data)
        self.assertEqual(result["by_provider"]["claude"]["tokens"]["input_tokens"], 10)
        self.assertIn("c" * 64, str(result["by_origin"]))
        self.assertFalse(result["cost_available"])

    def test_legacy_usage_snapshot_does_not_double_count_after_origin_upgrade(self):
        from continuity_plane.provider_usage import import_snapshot, report

        snapshot = {
            "provider": "claude",
            "session": "f" * 64,
            "tokens": {"input_tokens": 10, "output_tokens": 2},
        }
        import_snapshot(self.root, snapshot, self.data)
        import_snapshot(
            self.root,
            {**snapshot, "collection_origin": origin(provider="claude")},
            self.data,
        )
        value = report(self.root, self.data)
        self.assertEqual(value["by_provider"]["claude"]["tokens"]["input_tokens"], 10)
        self.assertEqual(value["superseded_unscoped_observations"], 1)

    def test_memory_backed_work_root_is_detected_even_outside_tmp(self):
        from continuity_plane.persistent_storage import describe

        mounts = "1 0 8:1 / / rw - ext4 /dev/disk rw\n2 1 0:1 / /workspaces/work rw - tmpfs tmpfs rw\n"
        value = describe("/workspaces/work/tasks", mountinfo=mounts)
        self.assertTrue(value["memory_backed"])
        self.assertFalse(value["recovery_suitable"])
        self.assertTrue(
            describe("/workspaces/persistent/tasks", mountinfo=mounts)[
                "recovery_suitable"
            ]
        )

    def test_worker_gets_persistent_scratch_not_process_tmp(self):
        from continuity_plane.supervisor import run_attempt
        from continuity_plane.persistent_storage import workspace

        expected, _ = workspace(self.data)

        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[
                sys.executable,
                "-c",
                'import os;print(os.environ.get("CONTINUITY_WORK_ROOT","unavailable"));print(os.environ.get("TMPDIR","unavailable"))',
            ],
        )
        self.assertEqual(result["status"], "completed")
        # Output tail is in the durable finish event; no session/profile files copied.
        from continuity_plane.collaboration_registry import list_changes

        ended = [
            r
            for r in list_changes(self.data, project_id="sample")["changes"]
            if r["event"] == "attempt.finished"
        ][-1]
        if expected:
            self.assertIn(str(expected), ended["output_tail"])
        else:
            self.assertFalse(result["storage"]["recovery_suitable"])

    def test_volatile_fallback_is_never_reported_as_persistent(self):
        from continuity_plane.persistent_storage import workspace

        with (
            patch.dict("os.environ", {"CONTINUITY_WORK_ROOT": "/tmp/task-work"}),
            patch(
                "continuity_plane.persistent_storage.describe",
                return_value={
                    "path": "/memory-backed-home",
                    "recovery_suitable": False,
                    "memory_backed": True,
                },
            ),
            patch("pathlib.Path.mkdir") as create,
        ):
            path, info = workspace(self.data)
        self.assertIsNone(path)
        self.assertFalse(info["recovery_suitable"])
        self.assertIn("cannot be guaranteed", info["warning"])
        create.assert_not_called()

    def test_disposable_root_uses_verified_persistent_fallback(self):
        from continuity_plane.persistent_storage import workspace, describe

        fallback = self.root / ".local/state/continuity/work"

        def simulated_storage(path):
            # Model a persistent home independently of where disposable test
            # fixtures live. tmpfs/prefix detection has separate real tests.
            if Path(path) == fallback:
                return {
                    "path": str(fallback),
                    "filesystem": "fixture-persistent",
                    "recovery_suitable": True,
                }
            return describe(path)

        with (
            patch.dict("os.environ", {"CONTINUITY_WORK_ROOT": "/tmp/task-work"}),
            patch("pathlib.Path.home", return_value=self.root),
            patch(
                "continuity_plane.persistent_storage.describe",
                side_effect=simulated_storage,
            ),
        ):
            path, info = workspace(self.data)
        self.assertEqual(path, self.root / ".local/state/continuity/work")
        self.assertTrue(info["recovery_suitable"])
        self.assertEqual(info["fallback_from"], "/tmp/task-work")

    def test_unwritable_scratch_does_not_block_the_ordinary_command(self):
        from continuity_plane.supervisor import run_attempt

        with patch(
            "continuity_plane.persistent_storage.workspace",
            return_value=(
                None,
                {"recovery_suitable": False, "warning": "unavailable"},
            ),
        ):
            value = run_attempt(
                self.data,
                project_id="sample",
                task_id="task",
                assignee="worker",
                command=[sys.executable, "-c", "print('ordinary work continues')"],
            )
        self.assertEqual(value["status"], "completed")
        self.assertFalse(value["storage"]["recovery_suitable"])

    def test_macos_temp_paths_and_escaped_linux_mounts(self):
        from continuity_plane.persistent_storage import describe

        self.assertFalse(
            describe("/private/var/folders/test/work")["recovery_suitable"]
        )
        mounts = "1 0 8:1 / / rw - ext4 /dev/disk rw\n2 1 0:1 / /workspaces/work\\040space rw - tmpfs tmpfs rw\n3 1 0:2 / /bad rw - \n"
        self.assertTrue(
            describe("/workspaces/work space/tasks", mountinfo=mounts)["memory_backed"]
        )

    def test_linked_checkout_identity_follows_current_git_markers(self):
        from continuity_plane.repository_identity import matches

        common = self.root / "canonical" / ".git"
        gitdir = common / "worktrees" / "worker"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        checkout = self.root / "worker"
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
        self.assertTrue(matches(checkout, "canonical"))
        self.assertFalse(matches(checkout, "unrelated"))
        (gitdir / "commondir").write_text("/not-a-repository\n")
        self.assertFalse(matches(checkout, "canonical"))

    def test_relative_storage_inspection_uses_the_selected_project_root(self):
        from continuity_plane.persistent_storage import command

        with (
            patch(
                "continuity_plane.persistent_storage.describe", return_value={}
            ) as inspect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                command(argparse.Namespace(path="scratch", root=str(self.root))), 0
            )
        inspect.assert_called_once_with(self.root / "scratch")


if __name__ == "__main__":
    unittest.main()
