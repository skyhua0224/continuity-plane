import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


class RepositoryVerificationCliTests(unittest.TestCase):
    def _write_valid_repository(self, root: Path) -> None:
        (root / "docs" / "architecture").mkdir(parents=True)
        (root / "schemas").mkdir()
        artifact = root / "schemas" / "example.txt"
        artifact.write_text("versioned schema artifact\n", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (root / "schemas" / "registry.yaml").write_text(
            "schemas:\n"
            "  - schema_id: context.example\n"
            "    artifact_path: schemas/example.txt\n"
            f"    content_sha256: {digest}\n",
            encoding="utf-8",
        )
        (root / "MASTER.md").write_text(
            "# MASTER\n\n版本：revision 1\n\n| M1-05 | 🟡 | active work |\n",
            encoding="utf-8",
        )
        (root / "STATUS.md").write_text(
            "# STATUS\n\n版本：revision 1\n\n"
            "| active work | M1-05：fault coverage（🟡） |\n",
            encoding="utf-8",
        )
        (root / "docs" / "architecture" / "target-state.md").write_text(
            "# Target State\n\n"
            "governance authority：`MASTER.md` revision 1\n",
            encoding="utf-8",
        )

    def test_script_exposes_a_help_entrypoint(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "tools" / "verify_repository.py"), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verify repository governance and admission gates", result.stdout)

    def test_ci_virtualenv_installs_dev_requirements(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load(
            (root / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["repository-verification"]["steps"]
        setup_python = next(
            (
                step
                for step in steps
                if step.get("name") == "Set up isolated Python"
            ),
            None,
        )

        self.assertIsNotNone(setup_python)
        self.assertIn("python3 -m venv .venv", setup_python["run"])
        self.assertIn(
            ".venv/bin/python -m pip install --requirement requirements-dev.txt",
            setup_python["run"],
        )

    def test_ci_uses_runner_python_inside_a_project_virtualenv(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load(
            (root / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["repository-verification"]["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)

        self.assertFalse(
            any(step.get("uses", "").startswith("actions/setup-python") for step in steps)
        )
        self.assertIn("python3 -m venv .venv", commands)
        self.assertIn(
            ".venv/bin/python -m pip install --requirement requirements-dev.txt",
            commands,
        )

    def test_ci_provisions_pinned_postgres_for_m2_03_integration(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load(
            (root / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["repository-verification"]
        steps = job["steps"]
        start = next(step for step in steps if step.get("name") == "Start isolated PostgreSQL")
        cleanup = next(step for step in steps if step.get("name") == "Stop isolated PostgreSQL")

        self.assertNotIn("services", job)
        self.assertIn(
            "postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
            start["run"],
        )
        self.assertIn("docker run --rm -d", start["run"])
        self.assertIn("127.0.0.1::5432", start["run"])
        self.assertIn("docker port", start["run"])
        self.assertIn("CONTEXT_TEST_POSTGRES_DSN", start["run"])
        self.assertIn("GITHUB_ENV", start["run"])
        self.assertIn("pg_isready", start["run"])
        self.assertEqual(cleanup["if"], "always()")
        self.assertIn("docker stop", cleanup["run"])

    def test_clean_repository_passes_all_gates(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("repository verification: passed", result.stdout)

    def test_revision_mismatch_is_rejected(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "STATUS.md").write_text(
                "# STATUS\n\n版本：revision 2\n\n"
                "| active work | M1-05：fault coverage（🟡） |\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MASTER/STATUS revision mismatch", result.stderr)

    def test_status_active_leaf_must_exist_in_master(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "STATUS.md").write_text(
                "# STATUS\n\n版本：revision 1\n\n"
                "| active work | M1-99：unknown work（🟡） |\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active leaf M1-99 is missing from MASTER", result.stderr)

    def test_active_leaf_must_be_in_progress(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "MASTER.md").write_text(
                "# MASTER\n\n版本：revision 1\n\n| M1-05 | ⏳ | active work |\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active leaf M1-05 must be 🟡", result.stderr)

    def test_schema_registry_hash_must_match_artifact(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            registry = root / "schemas" / "registry.yaml"
            registry.write_text(
                registry.read_text(encoding="utf-8").replace(
                    hashlib.sha256(
                        (root / "schemas" / "example.txt").read_bytes()
                    ).hexdigest(),
                    "0" * 64,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema registry hash mismatch", result.stderr)

    def test_raw_transcript_filename_is_rejected(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "provider-session.jsonl").write_text(
                '{"type":"message"}\n', encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raw transcript path is forbidden", result.stderr)

    def test_broken_local_markdown_link_is_rejected(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "README.md").write_text(
                "# Repository\n\n[Missing policy](docs/policies/missing.md)\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broken local Markdown link", result.stderr)

    def test_normative_documentation_rejects_rhetorical_contrast(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            policy_dir = root / "docs" / "policies"
            policy_dir.mkdir()
            (policy_dir / "example.md").write_text(
                "# Policy\n\n这不是旧方案而是新方案。\n", encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("documentation style violation", result.stderr)

    def test_style_policy_can_quote_its_own_prohibited_examples(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            policy_dir = root / "docs" / "policies"
            policy_dir.mkdir()
            (policy_dir / "documentation-style.md").write_text(
                "# Style Policy\n\n禁止使用“不是 X 而是 Y”和“第一刀”。\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_target_state_revision_must_match_master(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            target_state = root / "docs" / "architecture" / "target-state.md"
            target_state.write_text(
                "# Target State\n\n"
                "governance authority：`MASTER.md` revision 2\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MASTER/target-state revision mismatch", result.stderr)

    def test_invalid_json_or_yaml_is_rejected(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        invalid_documents = {
            "broken.json": '{"missing": }\n',
            "broken.yaml": "missing: [closing\n",
        }
        for filename, content in invalid_documents.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write_valid_repository(root)
                (root / filename).write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(script), "--root", str(root)],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid structured data", result.stderr)


if __name__ == "__main__":
    unittest.main()
