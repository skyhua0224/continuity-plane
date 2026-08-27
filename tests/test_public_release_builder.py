"""Fresh-history public release mirror tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from tools.build_public_release import build_public_release, scan_public_release


class PublicReleaseBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_a_release_scan_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("/home/example/private\n", encoding="utf-8")

            first = scan_public_release(root)
            second = scan_public_release(root)

            self.assertTrue(first)
            self.assertEqual(second, first)

    def test_build_creates_neutral_fresh_history_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            manifest = build_public_release(self.root, output, initialize_git=True)

            self.assertEqual(scan_public_release(output), [])
            self.assertFalse((output / "MASTER.md").exists())
            self.assertFalse((output / "STATUS.md").exists())
            self.assertFalse((output / "AGENTS.md").exists())
            self.assertTrue((output / "templates/MASTER.md").is_file())
            self.assertTrue((output / "templates/STATUS.md").is_file())
            self.assertTrue((output / "README.md").is_file())
            self.assertTrue((output / "README.en.md").is_file())
            public_plugin_manifest = output / "plugins/continuity-plane/.codex-plugin/plugin.json"
            public_plugin_marketplace = output / ".agents/plugins/marketplace.json"
            self.assertTrue(public_plugin_manifest.is_file())
            self.assertTrue(public_plugin_marketplace.is_file())
            self.assertTrue(
                (output / "plugins/continuity-plane/hooks/hooks.json").is_file()
            )
            self.assertTrue(
                (output / "plugins/continuity-plane/scripts/continuity-mcp-server.py").is_file()
            )
            self.assertEqual(
                json.loads(public_plugin_manifest.read_text(encoding="utf-8"))["version"],
                "0.1.0-alpha.7",
            )
            marketplace = json.loads(public_plugin_marketplace.read_text(encoding="utf-8"))
            self.assertEqual(marketplace["name"], "continuity-plane")
            self.assertEqual(
                marketplace["plugins"][0]["source"]["path"],
                "./plugins/continuity-plane",
            )
            self.assertIn("Codex plugin", (output / "README.md").read_text(encoding="utf-8"))
            self.assertIn("Codex plugin", (output / "README.en.md").read_text(encoding="utf-8"))
            self.assertIn("## 它解决哪些问题", (output / "README.md").read_text())
            self.assertIn("## Problems It Solves", (output / "README.en.md").read_text())
            public_readme = (output / "README.md").read_text(encoding="utf-8")
            self.assertIn("(docs/project-views.md)", public_readme)
            self.assertIn("(docs/visual-products.md)", public_readme)
            self.assertIn("(docs/use-cases.md)", public_readme)
            self.assertNotIn("(public/docs/", public_readme)
            self.assertLess(len(public_readme.encode("utf-8")), 10000)
            self.assertNotIn("本源码构建出的 alpha wheel", public_readme)
            self.assertNotIn("独立 verifier 使协作输入 token 增加", public_readme)
            self.assertIn("详情", public_readme)
            source_readme = (self.root / "README.md").read_text(encoding="utf-8")
            self.assertIn("(public/docs/project-views.md)", source_readme)
            self.assertIn("(public/docs/visual-products.md)", source_readme)
            self.assertIn("(public/docs/use-cases.md)", source_readme)
            source_readme_en = (self.root / "README.en.md").read_text(encoding="utf-8")
            self.assertIn("(public/docs/project-views.en.md)", source_readme_en)
            self.assertIn("(public/docs/visual-products.en.md)", source_readme_en)
            self.assertIn("(public/docs/use-cases.en.md)", source_readme_en)
            public_readme_en = (output / "README.en.md").read_text(encoding="utf-8")
            self.assertIn("(docs/project-views.en.md)", public_readme_en)
            self.assertIn("(docs/visual-products.en.md)", public_readme_en)
            self.assertIn("(docs/use-cases.en.md)", public_readme_en)
            self.assertNotIn("(public/docs/", public_readme_en)
            self.assertLess(len(public_readme_en.encode("utf-8")), 11000)
            self.assertNotIn("The alpha wheel built from this source", public_readme_en)
            self.assertTrue((output / "USAGE.md").is_file())
            self.assertTrue((output / "pyproject.toml").is_file())
            self.assertTrue((output / "NOTICE").is_file())
            self.assertTrue((output / "THIRD_PARTY_NOTICES.md").is_file())
            self.assertTrue((output / "BRANDING.md").is_file())
            for relative in (
                "USAGE.en.md",
                "BRANDING.en.md",
                "CONTRIBUTING.en.md",
                "SECURITY.en.md",
                "THIRD_PARTY_NOTICES.en.md",
                "CHANGELOG.en.md",
                "LICENSE.zh-CN.md",
                "docs/api.en.md",
                "docs/architecture.en.md",
                "docs/benchmarks.en.md",
                "docs/configuration.en.md",
                "docs/project-views.md",
                "docs/project-views.en.md",
                "docs/visual-products.md",
                "docs/visual-products.en.md",
                "docs/use-cases.md",
                "docs/use-cases.en.md",
                "templates/MASTER.en.md",
                "templates/STATUS.en.md",
            ):
                self.assertTrue((output / relative).is_file(), relative)
            self.assertTrue(
                (output / "docs/assets/managed-with-continuity-plane.svg").is_file()
            )
            self.assertTrue((output / "benchmarks/run_local_state.py").is_file())
            publish_workflow = output / ".github/workflows/publish.yml"
            self.assertTrue(publish_workflow.is_file())
            workflow_text = publish_workflow.read_text(encoding="utf-8")
            self.assertIn("id-token: write", workflow_text)
            self.assertIn("pypa/gh-action-pypi-publish", workflow_text)
            self.assertNotIn("PYPI_API_TOKEN", workflow_text)
            self.assertTrue(
                (output / "continuity_plane/postgres_state_store.py").is_file()
            )
            for module in (
                "decision_evidence_projection.py",
                "external_state_provider.py",
                "human_governance.py",
                "obsidian_vault.py",
                "project_graph_projection.py",
                "relationship_impact_projection.py",
            ):
                self.assertTrue((output / "continuity_plane" / module).is_file(), module)
            self.assertFalse(
                (output / "continuity_plane/context_health_projection.py").exists()
            )
            project_views = (output / "docs/project-views.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Context Health / Replay", project_views)
            self.assertIn("未进入 alpha wheel", project_views)
            self.assertIn("窗口有效利用率", (output / "README.md").read_text())
            self.assertTrue(
                (
                    output
                    / "continuity_plane/database/migrations/001_postgres_state.up.sql"
                ).is_file()
            )
            metadata = tomllib.loads((output / "pyproject.toml").read_text())
            self.assertEqual(metadata["project"]["name"], "continuity-plane")
            self.assertEqual(metadata["project"]["license"], "Apache-2.0")
            self.assertEqual(
                metadata["project"]["scripts"]["continuity"],
                "continuity_plane.cli:main",
            )
            self.assertIn("Apache License", (output / "LICENSE").read_text())
            public_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.is_file() and ".git" not in path.parts
            ).lower()
            for legacy in (
                "context control plane",
                "context-control-plane",
                "context_control_plane",
                ".context-control-plane",
            ):
                self.assertNotIn(legacy, public_text)
            public_markdown = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*.md")
                if ".git" not in path.parts
            )
            for internal_plan_marker in ("M10-", "M10_", "E0-E9"):
                self.assertNotIn(internal_plan_marker, public_markdown)
            changelog = (output / "CHANGELOG.md").read_text(encoding="utf-8")
            self.assertIn("28", changelog)
            self.assertIn("10,282", changelog)
            self.assertIn("标签生成之后", changelog)
            self.assertIn("## 未发布", changelog)
            self.assertGreater(manifest["file_count"], 20)
            count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=output,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "1")
            author = subprocess.run(
                ["git", "log", "-1", "--format=%an <%ae>"],
                cwd=output,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(
                author,
                "SkyHua <skyhua0224@users.noreply.github.com>",
            )

    def test_release_benchmark_contains_only_aggregate_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            build_public_release(self.root, output, initialize_git=False)
            receipt = json.loads(
                (output / "benchmarks/reference-results.json").read_text()
            )
            self.assertEqual(receipt["schema_version"], "context.public-benchmark/v1")
            self.assertNotIn("repository_revisions", receipt)
            self.assertNotIn("thread_id", receipt)
            self.assertEqual(receipt["quality_rate"], 1.0)
            self.assertEqual(receipt["sample_sizes"]["context_composition_per_arm"], 3)
            self.assertEqual(receipt["method"]["oracle"], "exact-required-facts")
            self.assertEqual(
                receipt["measurements"]["near_limit_packet_input_reduction_percent"],
                95.0625,
            )
            self.assertIn("public_reproduction_command", receipt)
            self.assertEqual(
                receipt["consistency_gates"],
                {
                    "authority_violations": 0,
                    "campaign_experiments_passed": 10,
                    "campaign_experiments_total": 10,
                    "dual_session_consistent": 1000,
                    "dual_session_total": 1000,
                    "duplicate_notifications_suppressed": 1000,
                    "duplicate_notifications_total": 1000,
                    "forced_faults_passed": 4,
                    "forced_faults_total": 4,
                    "offline_deliveries_recovered": 2000,
                    "offline_delivery_attempts": 2000,
                },
            )

    def test_release_wheel_installs_and_imports_all_packaged_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "public"
            dist = root / "dist"
            environment = root / "venv"
            build_public_release(self.root, output, initialize_git=False)
            subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
                cwd=output,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            wheel = next(dist.glob("*.whl"))
            subprocess.run(
                [str(python), "-m", "pip", "install", str(wheel)],
                check=True,
                capture_output=True,
                text=True,
            )
            script = """
import importlib
import pkgutil
import continuity_plane

failures = []
for module in pkgutil.walk_packages(
    continuity_plane.__path__, continuity_plane.__name__ + "."
):
    try:
        importlib.import_module(module.name)
    except Exception as exc:
        failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
if failures:
    raise SystemExit("\\n".join(failures))
"""
            subprocess.run(
                [str(python), "-c", script],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
