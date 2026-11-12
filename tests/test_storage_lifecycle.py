"""Storage lifecycle registry, capacity, and temporary-directory GC tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_registry import add_task, update_task
from continuity_plane.storage_lifecycle import (
    StorageLifecycleError,
    _safe_for_removal,
    gc_storage,
    load_registry,
    load_tier_config,
    register_location,
    report_storage,
    validate_tier_config,
)


class StorageLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.hot = self.root / "hot"
        self.hot.mkdir()
        self.config = {
            "schema_version": "context.storage-tiers/v1alpha1",
            "project_id": "sample",
            "archive_tier": "archive",
            "machines": {
                "machine-a": {
                    "local_tiers": ["hot"],
                    "archive_access": ["archive"],
                    "ssh_host_ref": "local-test",
                }
            },
            "tiers": {
                "hot": {
                    "kind": "filesystem",
                    "uri_template": str(self.hot),
                    "declared_roots": [str(self.hot)],
                    "writable": True,
                    "durability": "persistent",
                },
                "archive": {
                    "kind": "filesystem",
                    "uri_template": str(self.root / "archive"),
                    "declared_roots": [str(self.root / "archive")],
                    "writable": True,
                    "durability": "archive",
                },
            },
            "mover": {"default": None, "allowed": [], "require_verified_copy": True},
            "retention": {"idle_days": 14, "package_keep_last": 3},
            "capacity": {
                "high_watermark_fraction": 0.85,
                "evidence_min_free_bytes": None,
                "package_min_free_bytes": None,
                "scratch_min_free_bytes": None,
                "job_output_trim_days": 1,
            },
            "automation": {"auto_apply": [], "grace_period": "24h", "daily_report": False},
        }
        self.add_card("task", status="completed")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_card(self, task_id: str, *, status: str = "queued", worktree: Path | None = None, branch: str | None = None) -> None:
        add_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Test card",
            objective="Test storage lifecycle",
            next_action="test",
            exit_criteria=["pass"],
        )
        update_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            status=status,
            worktree=worktree,
            branch=branch,
        )

    def register(self, path: Path, *, task_id: str = "task", kind: str = "scratch", retention: str = "scratch", operation: str | None = None, adopt: bool = False, package_name: str | None = None):
        return register_location(
            self.data,
            project_id="sample",
            task_id=task_id,
            machine_id="machine-a",
            uri=path.resolve().as_uri(),
            location_kind=kind,
            retention_class=retention,
            tier="hot",
            operation_id=operation,
            config=self.config,
            adopt=adopt,
            package_name=package_name,
        )

    def test_shipped_default_is_non_destructive_and_has_no_concrete_roots(self):
        config = load_tier_config()
        self.assertEqual(config["automation"]["auto_apply"], [])
        self.assertEqual(config["machines"], {})
        self.assertEqual(config["tiers"], {})

    def test_minimal_project_config_takes_defaults_and_errors_name_fields(self):
        minimal = {
            "schema_version": "context.storage-tiers/v1alpha1",
            "machines": {"m": {"local_tiers": ["hot"]}},
            "tiers": {"hot": {"kind": "filesystem", "declared_roots": [str(self.hot)]}},
        }
        config = validate_tier_config(minimal)
        self.assertEqual(config["automation"]["auto_apply"], [])
        self.assertEqual(config["retention"]["idle_days"], 14)
        with self.assertRaisesRegex(StorageLifecycleError, "unknown fields: colour"):
            validate_tier_config({**minimal, "colour": 1})
        with self.assertRaisesRegex(StorageLifecycleError, "missing: tiers"):
            validate_tier_config({k: v for k, v in minimal.items() if k != "tiers"})
        with self.assertRaisesRegex(StorageLifecycleError, "undefined tiers: cold"):
            validate_tier_config({**minimal, "machines": {"m": {"local_tiers": ["cold"]}}})

    def test_tier_validator_rejects_absolute_path_in_shipped_shape(self):
        config = load_tier_config()
        config["tiers"] = {"bad": {"declared_roots": ["/owner/private"]}}
        with self.assertRaises(StorageLifecycleError):
            validate_tier_config(config, shipped=True)

    def test_register_is_idempotent_by_operation_id(self):
        path = self.hot / "scratch"
        path.mkdir()
        first = self.register(path, operation="op-register")
        second = self.register(path, operation="op-register")
        self.assertEqual(first, second)
        journal = (self.data / "projects/sample/storage/journal.jsonl").read_text()
        self.assertEqual(journal.count('"operation_id": "op-register"'), 1)

    def test_corrupt_or_missing_snapshot_replays_journal(self):
        path = self.hot / "scratch"
        path.mkdir()
        self.register(path, operation="op-replay")
        registry_path = self.data / "projects/sample/storage/registry.json"
        registry_path.write_text("{corrupt", encoding="utf-8")
        replayed = load_registry(self.data, "sample")
        self.assertEqual(replayed["locations"][0]["uri"], path.resolve().as_uri())
        self.assertEqual(json.loads(registry_path.read_text())["revision"], 1)

    def test_root_containment_and_symlink_escape_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.hot / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageLifecycleError):
            self.register(link)

    def test_gc_blocks_location_inside_git_tree(self):
        repo = self.hot / "repo"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        (repo / "README").write_text("base")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-m", "base")
        nested = repo / "nested"
        nested.mkdir()
        (nested / "tracked.txt").write_text("committed")
        self.git(repo, "add", "nested/tracked.txt")
        self.git(repo, "commit", "-m", "nested")
        untracked = repo / "build"
        untracked.mkdir()
        (untracked / "out.o").write_text("rebuildable")
        self.add_card("task-inside", status="completed")
        self.register(nested, task_id="task-inside", operation="op-inside")
        self.register(untracked, task_id="task-inside", operation="op-untracked")
        result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertEqual([item["reason"] for item in result["blocked"]], ["inside-git-tree"])
        self.assertTrue((nested / "tracked.txt").exists(), "tracked files are never removed")
        self.assertFalse(untracked.exists(), "untracked content in a repository is ordinary scratch")

    def test_gc_apply_persists_deleted_state_from_fresh_registry(self):
        path = self.hot / "fresh"
        path.mkdir()
        location = self.register(path, operation="op-fresh")
        result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertEqual(result["applied"][0]["location_id"], location["location_id"])
        persisted = load_registry(self.data, "sample")
        current = next(item for item in persisted["locations"] if item["location_id"] == location["location_id"])
        self.assertEqual(current["state"], "deleted")

    def test_gc_apply_rechecks_safety_under_lock(self):
        path = self.hot / "race"
        path.mkdir()
        self.register(path, operation="op-race")
        with patch(
            "continuity_plane.storage_lifecycle._recheck_location",
            side_effect=[(path, True, "safe"), (path, False, "outside-declared-tier-root")],
        ):
            result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertEqual(result["blocked"][0]["reason"], "outside-declared-tier-root")
        self.assertTrue(path.exists())

    def test_gc_uses_live_card_update_and_skips_active_cards(self):
        path = self.hot / "live"
        path.mkdir()
        self.add_card("task-live", status="queued")
        location = self.register(path, task_id="task-live", operation="op-live")
        registry_path = self.data / "projects/sample/storage/registry.json"
        registry = json.loads(registry_path.read_text())
        registry["locations"][0]["last_card_update_at"] = "2020-01-01T00:00:00+00:00"
        registry["locations"][0]["last_job_activity_at"] = "2020-01-01T00:00:00+00:00"
        registry_path.write_text(json.dumps(registry))
        update_task(self.data, project_id="sample", task_id="task-live", status="queued")
        current = datetime.now(UTC) + timedelta(days=1)
        result = gc_storage(self.data, project_id="sample", config=self.config, now=current)
        self.assertEqual(result["candidates"], [])
        update_task(self.data, project_id="sample", task_id="task-live", status="active", assignee="worker")
        result = gc_storage(self.data, project_id="sample", config=self.config, now=current + timedelta(days=30))
        self.assertEqual(result["candidates"], [])
        self.assertTrue(path.exists())
        self.assertEqual(location["task_id"], "task-live")

    def test_gc_skips_locations_with_running_jobs(self):
        path = self.hot / "running"
        path.mkdir()
        self.register(path, operation="op-running")
        jobs = self.data / "projects/sample/worker-jobs.json"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        # Real index entries carry no status; the job's own job.json does.
        job_dir = self.data / "projects/sample/worker-jobs/job-1"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({"job_id": "job-1", "status": "running", "job_status": "running"}))
        jobs.write_text(json.dumps({"task": {"job_id": "job-1", "job_dir": str(job_dir)}}))
        result = gc_storage(self.data, project_id="sample", config=self.config, now=datetime.now(UTC) + timedelta(days=30))
        self.assertEqual(result["candidates"], [])

    def test_running_job_output_is_not_trimmed(self):
        job = self.hot / "running-job"
        job.mkdir()
        (job / "job.json").write_text(json.dumps({"status": "running"}))
        (job / "output.jsonl").write_bytes(b"1\n" * 100)
        self.register(job, kind="job", operation="op-running-job")
        result = gc_storage(
            self.data,
            project_id="sample",
            config=self.config,
            apply=True,
            now=datetime.now(UTC) + timedelta(days=2),
            job_tail_bytes=16,
        )
        self.assertEqual(result["applied"], [])
        self.assertEqual((job / "output.jsonl").read_bytes(), b"1\n" * 100)

    def test_orphan_discovery_only_checks_immediate_children(self):
        child = self.hot / "orphan"
        nested = child / "nested"
        nested.mkdir(parents=True)
        (nested / "file").write_text("payload")
        with patch("continuity_plane.storage_lifecycle._git_state") as git_probe:
            git_probe.return_value = {"branch": None}
            report = report_storage(self.data, project_id="sample", machine_id="machine-a", config=self.config, include_orphans=True)
        orphan_uris = [item["uri"] for item in report["orphans"]]
        self.assertEqual(orphan_uris, [child.resolve().as_uri()])
        self.assertEqual(git_probe.call_count, 1)

    def test_scratch_registration_records_size_without_reading_content(self):
        path = self.hot / "size-only"
        path.mkdir()
        (path / "large").write_bytes(b"payload")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("scratch content was read")):
            location = self.register(path, operation="op-size-only")
        self.assertEqual(location["size_bytes"], len(b"payload"))
        self.assertIsNone(location["content_sha256"])

    def test_journal_stores_operations_not_full_registry_snapshots(self):
        path = self.hot / "journal"
        path.mkdir()
        self.register(path, operation="op-journal")
        gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        journal_lines = (self.data / "projects/sample/storage/journal.jsonl").read_text().splitlines()
        self.assertTrue(journal_lines)
        self.assertTrue(all("\"registry\"" not in line for line in journal_lines))
        registry_path = self.data / "projects/sample/storage/registry.json"
        registry_path.write_text("corrupt")
        replayed = load_registry(self.data, "sample")
        self.assertEqual(replayed["locations"][0]["state"], "deleted")

    def test_report_contains_capacity_and_high_watermark_ordering(self):
        first = self.hot / "first"
        second = self.hot / "second"
        first.mkdir()
        second.mkdir()
        (first / "large").write_bytes(b"x" * 100)
        (second / "small").write_bytes(b"x")
        self.register(first, operation="op-first")
        self.register(second, operation="op-second")
        with patch("continuity_plane.storage_lifecycle.os.statvfs", create=True) as statvfs:
            statvfs.return_value.f_blocks = 100
            statvfs.return_value.f_bavail = 10
            statvfs.return_value.f_bfree = 10
            statvfs.return_value.f_frsize = 1024
            report = report_storage(self.data, project_id="sample", machine_id="machine-a", config=self.config)
        self.assertEqual(report["tiers"][0]["free_bytes"], 10240)
        self.assertTrue(report["tiers"][0]["high_watermark"])
        self.assertEqual(report["locations"][0]["location_id"], "loc-" + report["locations"][0]["location_id"][4:])
        self.assertGreaterEqual(report["locations"][0]["size_bytes"], report["locations"][1]["size_bytes"])

    def test_evidence_allocator_rejects_memory_backed_tier(self):
        evidence = self.hot / "evidence"
        evidence.mkdir()
        with patch(
            "continuity_plane.storage_lifecycle.describe",
            return_value={"memory_backed": True, "temporary_location": True},
        ):
            with self.assertRaisesRegex(StorageLifecycleError, "capacity safety"):
                self.register(evidence, kind="evidence", retention="evidence", operation="op-memory")

    def test_orphan_report_suggests_card_by_worktree_and_branch(self):
        worktree = self.hot / "worktree"
        worktree.mkdir()
        orphan = worktree / "evidence"
        orphan.mkdir()
        self.add_card("task-orphan", worktree=worktree, branch="refs/heads/feature")
        report = report_storage(self.data, project_id="sample", machine_id="machine-a", config=self.config, include_orphans=True)
        suggestions = {item["suggested_task_id"] for item in report["orphans"]}
        self.assertIn("task-orphan", suggestions)

    def test_gc_defaults_to_dry_run(self):
        path = self.hot / "scratch"
        path.mkdir()
        self.register(path, operation="op-dry")
        result = gc_storage(self.data, project_id="sample", config=self.config)
        self.assertTrue(result["dry_run"])
        self.assertTrue(path.exists())
        self.assertTrue(any(item["decision"] == "candidate-delete" for item in result["candidates"]))

    def test_gc_apply_deletes_non_git_scratch_only_in_temp_fixture(self):
        path = self.hot / "scratch"
        path.mkdir()
        self.register(path, operation="op-delete")
        result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertFalse(path.exists())
        self.assertEqual(result["applied"][0]["action"], "delete")
        journal = (self.data / "projects/sample/storage/journal.jsonl").read_text()
        self.assertIn('"phase": "prepare"', journal)
        self.assertIn('"phase": "delete-intent"', journal)
        self.assertIn('"phase": "committed"', journal)

    def test_safe_for_removal_blocks_each_git_boundary(self):
        cases = {
            "dirty-or-untracked": {"dirty": True},
            "dirty-or-untracked-untracked": {"untracked_files": 1},
            "git-operation-in-progress": {"in_progress_operation": "merge"},
            "worktree-locked": {"locked": True},
            "detached-head-unreachable": {"topology": "linked-worktree", "detached_head_reachable": False},
            "standalone-local-only-ref": {"topology": "standalone-clone", "origin_refs_complete": False},
            "git-state-unknown": {"observation_status": "unknown"},
        }
        for reason, changes in cases.items():
            state = {
                "dirty": False,
                "untracked_files": 0,
                "in_progress_operation": "none",
                "locked": False,
                "topology": "none",
                "detached_head_reachable": True,
                "origin_refs_complete": True,
                "unpushed_commits": 0,
            }
            state.update(changes)
            expected = "dirty-or-untracked" if reason == "dirty-or-untracked-untracked" else reason
            self.assertEqual(_safe_for_removal({"git": state}), (False, expected))

    def test_linked_worktree_removal_keeps_branch(self):
        repo = self.hot / "repo"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        (repo / "README").write_text("base")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-m", "base")
        linked = self.hot / "linked"
        self.git(repo, "worktree", "add", "-b", "feature", str(linked))
        self.add_card("task-linked", status="completed")
        self.register(linked, task_id="task-linked", kind="workspace", operation="op-linked")
        result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertEqual(result["applied"][0]["action"], "delete")
        self.assertFalse(linked.exists())
        self.assertEqual(self.git(repo, "show-ref", "--verify", "refs/heads/feature").returncode, 0)

    def test_standalone_clone_with_local_only_ref_is_blocked(self):
        origin = self.root / "origin.git"
        self.git(self.root, "init", "--bare", str(origin))
        clone = self.hot / "clone"
        self.git(self.root, "clone", str(origin), str(clone))
        self.git(clone, "config", "user.email", "test@example.invalid")
        self.git(clone, "config", "user.name", "Test")
        (clone / "README").write_text("base")
        self.git(clone, "add", "README")
        self.git(clone, "commit", "-m", "base")
        self.git(clone, "push", "origin", "HEAD:main")
        self.git(clone, "fetch", "origin")
        self.git(clone, "checkout", "-b", "local-only")
        (clone / "LOCAL").write_text("local")
        self.git(clone, "add", "LOCAL")
        self.git(clone, "commit", "-m", "local")
        self.add_card("task-clone", status="completed")
        self.register(clone, task_id="task-clone", kind="workspace", operation="op-clone")
        result = gc_storage(self.data, project_id="sample", config=self.config)
        self.assertEqual(result["blocked"][0]["reason"], "standalone-local-only-ref")
        self.assertEqual(result["blocked"][0]["decision"], "blocked")

    def test_job_trim_preserves_required_metadata(self):
        job = self.hot / "job"
        job.mkdir()
        for name in ("job.json", "result.json", "report.md"):
            (job / name).write_text(name)
        (job / "output.jsonl").write_bytes(b"1\n" * 100)
        self.register(job, kind="job", operation="op-job")
        result = gc_storage(
            self.data,
            project_id="sample",
            config=self.config,
            apply=True,
            now=datetime.now(UTC) + timedelta(days=2),
            job_tail_bytes=16,
        )
        self.assertEqual(result["applied"][0]["action"], "trim")
        self.assertTrue(all((job / name).exists() for name in ("job.json", "result.json", "report.md")))
        self.assertLessEqual((job / "output.jsonl").stat().st_size, 16)

    def test_gc_does_not_delete_evidence_or_package_in_stage_one(self):
        evidence = self.hot / "evidence"
        evidence.mkdir()
        package = self.hot / "package"
        package.mkdir()
        with patch(
            "continuity_plane.storage_lifecycle.describe",
            return_value={"memory_backed": False, "temporary_location": False},
        ):
            self.register(evidence, kind="evidence", retention="evidence", operation="op-evidence")
            self.register(package, kind="package", retention="package", operation="op-package", adopt=True, package_name="pkg")
        result = gc_storage(self.data, project_id="sample", config=self.config, apply=True)
        self.assertEqual(result["applied"], [])
        self.assertTrue(evidence.exists())
        self.assertTrue(package.exists())

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
