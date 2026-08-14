import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.document_lifecycle import (
    build_document_control_manifest,
)
from tools.verify_repository import validate_registered_instance, verify_repository


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
            "# Target State\n\ngovernance authority：`MASTER.md` revision 1\n",
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
            (step for step in steps if step.get("name") == "Set up isolated Python"),
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
            any(
                step.get("uses", "").startswith("actions/setup-python")
                for step in steps
            )
        )
        self.assertIn("python3 -m venv .venv", commands)
        self.assertIn(
            ".venv/bin/python -m pip install --requirement requirements-dev.txt",
            commands,
        )

    def test_repository_verification_checkout_includes_governed_history(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load(
            (root / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        checkout = workflow["jobs"]["repository-verification"]["steps"][0]

        self.assertEqual(checkout["uses"], "actions/checkout@v4")
        self.assertEqual(checkout.get("with", {}).get("fetch-depth"), 0)

        secret_scan_checkout = workflow["jobs"]["secret-scan"]["steps"][0]
        self.assertEqual(secret_scan_checkout["uses"], "actions/checkout@v4")
        self.assertEqual(secret_scan_checkout.get("with", {}).get("fetch-depth"), 0)

    def test_ci_supports_container_and_host_runner_postgres_networking(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load(
            (root / ".gitea" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["repository-verification"]
        steps = job["steps"]
        start = next(
            step for step in steps if step.get("name") == "Start isolated PostgreSQL"
        )
        cleanup = next(
            step for step in steps if step.get("name") == "Stop isolated PostgreSQL"
        )

        self.assertNotIn("services", job)
        self.assertIn(
            "postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
            start["run"],
        )
        self.assertIn("docker run --rm -d", start["run"])
        self.assertIn('docker inspect "${HOSTNAME}"', start["run"])
        self.assertIn('--network "${JOB_NETWORK}"', start["run"])
        self.assertIn('--publish "127.0.0.1::5432"', start["run"])
        self.assertIn('POSTGRES_HOST="${POSTGRES_CONTAINER}"', start["run"])
        self.assertIn('POSTGRES_HOST="127.0.0.1"', start["run"])
        self.assertIn('docker port "${POSTGRES_CONTAINER}" 5432/tcp', start["run"])
        self.assertIn("${POSTGRES_HOST}:${POSTGRES_PORT}/context_test", start["run"])
        self.assertIn("CONTEXT_TEST_POSTGRES_DSN", start["run"])
        self.assertIn("GITHUB_ENV", start["run"])
        self.assertIn("pg_isready", start["run"])
        self.assertEqual(cleanup["if"], "always()")
        self.assertIn("${CONTEXT_TEST_POSTGRES_CONTAINER:-}", cleanup["run"])
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

    def test_master_must_have_only_one_in_progress_leaf(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            (root / "MASTER.md").write_text(
                "# MASTER\n\n版本：revision 1\n\n"
                "| M1-05 | 🟡 | active work |\n"
                "| M1-06 | 🟡 | duplicate active work |\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MASTER must contain exactly one 🟡 task", result.stderr)

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
                "# Target State\n\ngovernance authority：`MASTER.md` revision 2\n",
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
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as directory,
            ):
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

    def test_repository_verifier_runs_document_lifecycle_gate(self):
        root = Path(__file__).parents[1]
        config = yaml.safe_load(
            (root / "profiles" / "document-control-config.yaml").read_text(
                encoding="utf-8"
            )
        )
        current_manifest = yaml.safe_load(
            (root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            build_document_control_manifest(root, config), current_manifest
        )

        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "verify_repository.py"),
                "--root",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repository_verifier_enforces_live_benchmark_receipt(self):
        root = Path(__file__).parents[1]
        with mock.patch(
            "tools.verify_repository.validate_document_lifecycle_benchmark_receipt",
            side_effect=ValueError("forced live benchmark failure"),
        ):
            errors = verify_repository(root)

        self.assertEqual(len(errors), 1)
        self.assertIn("forced live benchmark failure", errors[0])

    def test_repository_verifier_uses_governed_benchmark_baseline(self):
        root = Path(__file__).parents[1]
        alternate_ref = subprocess.run(
            ["git", "rev-parse", "HEAD^"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        config = {
            "schema_version": "context.document-lifecycle-benchmark-config/v1alpha1",
            "baseline_git_ref": alternate_ref,
            "validator_samples": 40,
            "validator_p95_limit_ms": 100,
        }
        with (
            mock.patch(
                "tools.verify_repository.load_document_lifecycle_benchmark_config",
                return_value=config,
                create=True,
            ),
            mock.patch(
                "tools.verify_repository.validate_document_lifecycle_benchmark_receipt"
            ) as validate_receipt,
        ):
            errors = verify_repository(root)

        self.assertEqual(errors, [])
        self.assertEqual(
            validate_receipt.call_args.kwargs["baseline_ref"], alternate_ref
        )

    def test_repository_verifier_uses_the_bounded_baseline_loader(self):
        root = Path(__file__).parents[1]
        config = {
            "schema_version": "context.document-lifecycle-benchmark-config/v1alpha1",
            "baseline_git_ref": "a" * 40,
            "validator_samples": 40,
            "validator_p95_limit_ms": 100,
        }
        with (
            mock.patch(
                "tools.verify_repository.load_document_lifecycle_benchmark_config",
                return_value=config,
            ),
            mock.patch(
                "tools.verify_repository.load_document_lifecycle_benchmark_baseline",
                return_value=(b"governed status", b"governed master"),
                create=True,
            ) as load_baseline,
            mock.patch(
                "tools.verify_repository.validate_document_lifecycle_benchmark_receipt"
            ) as validate_receipt,
        ):
            errors = verify_repository(root)

        self.assertEqual(errors, [])
        load_baseline.assert_called_once_with(root, "a" * 40)
        self.assertEqual(
            validate_receipt.call_args.kwargs["baseline_status"], b"governed status"
        )
        self.assertEqual(
            validate_receipt.call_args.kwargs["baseline_master"], b"governed master"
        )

    def test_repository_verifier_rejects_document_evidence_drift(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            evidence = root / "docs" / "architecture" / "evidence.md"
            evidence.write_text("verified evidence\n", encoding="utf-8")
            documents = []
            categories = {
                "MASTER.md": "governance",
                "STATUS.md": "routing",
                "docs/architecture/target-state.md": "architecture",
                "docs/architecture/evidence.md": "architecture",
            }
            for index, (path, category) in enumerate(categories.items(), start=1):
                documents.append(
                    {
                        "document_id": f"document-{index}",
                        "path": path,
                        "category": category,
                        "document_revision": 1,
                        "reference_depth": 0,
                        "authority": {
                            "governance_authority": category == "governance",
                            "active_state_authority": False,
                            "controlled_action_entry": False,
                        },
                        "change": {
                            "change_type": {
                                "governance": "decision",
                                "routing": "status",
                                "architecture": "decision",
                            }[category],
                            "authority_ref": "governance-event://master/revision/1",
                            "supersedes": None,
                            "affected_tasks": ["M1-05"],
                            "next_review": None,
                        },
                        "evidence_refs": (
                            [
                                {
                                    "ref_id": "current-evidence",
                                    "path": "docs/architecture/evidence.md",
                                    "valid_until": None,
                                }
                            ]
                            if path == "STATUS.md"
                            else []
                        ),
                    }
                )
            config = {
                "schema_version": "context.document-control-config/v1alpha1",
                "generated_at": "2026-08-12T00:00:00Z",
                "governance_revision": 1,
                "documents": documents,
            }
            manifest = build_document_control_manifest(root, config)
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "document-control-config.yaml").write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            (profiles / "document-control-manifest.yaml").write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            evidence.write_text("tampered evidence\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("document lifecycle verification failed", result.stderr)

    def test_registered_document_lifecycle_requires_config_and_manifest(self):
        script = Path(__file__).parents[1] / "tools" / "verify_repository.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_repository(root)
            lifecycle_schema = root / "schemas" / "document-lifecycle.json"
            lifecycle_schema.write_text("{}\n", encoding="utf-8")
            registry = root / "schemas" / "registry.yaml"
            registry.write_text(
                registry.read_text(encoding="utf-8")
                + "  - schema_id: context.document-control-manifest\n"
                + "    artifact_path: schemas/document-lifecycle.json\n"
                + f"    content_sha256: {hashlib.sha256(lifecycle_schema.read_bytes()).hexdigest()}\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "document lifecycle config and manifest are required", result.stderr
        )

    def test_registered_instance_uses_the_strict_json_schema(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["known"],
            "properties": {"known": {"const": True}},
        }
        invalid = {"known": True, "unexpected": True}
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

        with self.assertRaisesRegex(ValueError, "strict schema"):
            validate_registered_instance(invalid, schema, "test instance")


if __name__ == "__main__":
    unittest.main()
