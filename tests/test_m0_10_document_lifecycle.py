import copy
import hashlib
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.document_lifecycle import (
    DocumentLifecycleError,
    _managed_markdown_paths,
    build_document_control_manifest,
    canonical_manifest_bytes,
    validate_document_control_manifest,
)
from tools.generate_document_manifest import (
    build_initial_config,
    sync_document_control_config,
)


class M010DocumentLifecycleTests(unittest.TestCase):
    maxDiff = None

    def test_document_discovery_excludes_packaging_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "README.md", "# Project\n")
            self._write(root, "build/lib/README.md", "# Build copy\n")
            self._write(root, "dist/docs/README.md", "# Dist copy\n")
            self._write(root, "package.egg-info/README.md", "# Metadata copy\n")
            self._write(root, ".continuity/MASTER.md", "# Runtime bridge\n")
            self._write(root, ".continuity/STATUS.md", "# Runtime route\n")

            self.assertEqual(_managed_markdown_paths(root), {"README.md"})

    def _write(self, root: Path, relative: str, content: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _metrics(self, content: str) -> dict[str, int]:
        links = re.findall(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)", content)
        local_links = [
            link
            for link in links
            if not link.strip("<>").startswith(("http://", "https://"))
        ]
        external_links = [
            link for link in links if link.strip("<>").startswith("https://")
        ]
        return {
            "utf8_bytes": len(content.encode("utf-8")),
            "section_count": sum(
                1 for line in content.splitlines() if line.startswith("## ")
            ),
            "local_link_count": len(local_links),
            "external_link_count": len(external_links),
            "reference_depth": 0,
        }

    def _entry(
        self,
        root: Path,
        *,
        document_id: str,
        path: str,
        category: str,
        revision: int = 1,
        evidence_paths: tuple[str, ...] = (),
        valid_until: str | None = None,
    ) -> dict:
        content = (root / path).read_text(encoding="utf-8")
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        governance = category == "governance"
        change_type = {
            "governance": "decision",
            "routing": "status",
            "policy": "decision",
            "architecture": "decision",
            "research": "evidence",
            "migration": "evidence",
            "report": "projection",
            "projection": "projection",
        }[category]
        authority_prefix = {
            "governance": "governance-event://master/revision/1",
            "routing": "governance-event://master/revision/1",
            "policy": "governance-event://master/revision/1",
            "architecture": "governance-event://master/revision/1",
            "research": f"evidence-bundle://repository/{content_digest}",
            "migration": f"verification-run://repository/{content_digest}",
            "report": "state-revision://repository/1",
            "projection": "state-revision://repository/1",
        }[category]
        evidence_refs = []
        for evidence_path in evidence_paths:
            evidence = root / evidence_path
            evidence_refs.append(
                {
                    "ref_id": evidence_path.replace("/", "-"),
                    "path": evidence_path,
                    "content_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    "valid_until": valid_until,
                }
            )
        return {
            "document_id": document_id,
            "path": path,
            "category": category,
            "document_revision": revision,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "metrics": self._metrics(content),
            "authority": {
                "governance_authority": governance,
                "active_state_authority": False,
                "controlled_action_entry": False,
            },
            "change": {
                "change_type": change_type,
                "authority_ref": authority_prefix,
                "supersedes": None,
                "affected_tasks": ["M0-10"],
                "next_review": None,
            },
            "evidence_refs": evidence_refs,
        }

    def _valid_repository(self, root: Path) -> dict:
        master = (
            "# MASTER\n\n版本：revision 1\n\n## Plan\n\n| M0-10 | 🟡 | lifecycle |\n"
        )
        status = (
            "# STATUS\n\n版本：revision 1\n\n"
            "| active work | M0-10：document lifecycle（🟡） |\n"
            "| hard blocker | none |\n"
            "| next action | validate documents |\n\n"
            "## 恢复入口\n\n1. Read [MASTER](MASTER.md).\n"
        )
        target = "# Target State\n\n版本：revision 1\n\n## Documents\n\nBounded projections.\n"
        evidence = "# Acceptance\n\nverified: true\n"
        self._write(root, "MASTER.md", master)
        self._write(root, "STATUS.md", status)
        self._write(root, "docs/architecture/target-state.md", target)
        self._write(root, "docs/migrations/evidence.md", evidence)
        manifest = {
            "schema_version": "context.document-control-manifest/v1alpha1",
            "generated_at": "2026-08-12T00:00:00Z",
            "governance_revision": 1,
            "documents": [
                self._entry(
                    root,
                    document_id="master",
                    path="MASTER.md",
                    category="governance",
                    evidence_paths=("docs/migrations/evidence.md",),
                ),
                self._entry(
                    root,
                    document_id="status",
                    path="STATUS.md",
                    category="routing",
                    evidence_paths=("docs/migrations/evidence.md",),
                ),
                self._entry(
                    root,
                    document_id="target-state",
                    path="docs/architecture/target-state.md",
                    category="architecture",
                ),
                self._entry(
                    root,
                    document_id="acceptance-evidence",
                    path="docs/migrations/evidence.md",
                    category="migration",
                ),
            ],
        }
        manifest["documents"][1]["metrics"]["reference_depth"] = 1
        return manifest

    def _validate(self, root: Path, manifest: dict) -> dict:
        return validate_document_control_manifest(
            root,
            manifest,
            as_of=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )

    def _config_from_manifest(self, manifest: dict) -> dict:
        config = {
            "schema_version": "context.document-control-config/v1alpha1",
            "generated_at": manifest["generated_at"],
            "governance_revision": manifest["governance_revision"],
            "documents": [],
        }
        for entry in manifest["documents"]:
            configured = copy.deepcopy(entry)
            configured.pop("content_sha256")
            configured.pop("metrics")
            for ref in configured["evidence_refs"]:
                ref.pop("content_sha256")
            config["documents"].append(configured)
        return config

    def test_valid_manifest_recomputes_document_and_recovery_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)

            result = self._validate(root, manifest)

        self.assertEqual(result["document_count"], 4)
        self.assertEqual(result["drifted_documents"], 0)
        self.assertEqual(result["drifted_evidence_refs"], 0)
        self.assertEqual(result["duplicate_prose_blocks"], 0)
        self.assertEqual(result["expired_evidence_refs"], 0)
        self.assertEqual(result["recovery_fields_total"], 5)
        self.assertEqual(result["recovery_fields_recovered"], 5)

    def test_status_content_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            (root / "STATUS.md").write_text("stale status\n", encoding="utf-8")

            with self.assertRaisesRegex(
                DocumentLifecycleError, "document hash drift.*STATUS"
            ):
                self._validate(root, manifest)

    def test_evidence_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            (root / "docs/migrations/evidence.md").write_text(
                "# Acceptance\n\nverified: false\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                DocumentLifecycleError, "(?:document|evidence) hash drift"
            ):
                self._validate(root, manifest)

    def test_repeated_full_prose_is_rejected(self):
        repeated = " ".join(["This paragraph carries a normative contract."] * 12)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            for entry in manifest["documents"][1:3]:
                path = root / entry["path"]
                content = path.read_text(encoding="utf-8") + f"\n\n{repeated}\n"
                path.write_text(content, encoding="utf-8")
                entry["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                reference_depth = entry["metrics"]["reference_depth"]
                entry["metrics"] = self._metrics(content)
                entry["metrics"]["reference_depth"] = reference_depth

            with self.assertRaisesRegex(DocumentLifecycleError, "duplicate full prose"):
                self._validate(root, manifest)

    def test_expired_evidence_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["evidence_refs"][0]["valid_until"] = (
                "2026-08-11T23:59:59Z"
            )

            with self.assertRaisesRegex(
                DocumentLifecycleError, "expired evidence reference"
            ):
                self._validate(root, manifest)

    def test_status_over_twelve_kib_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            oversized = (root / "STATUS.md").read_text(encoding="utf-8") + "x" * 12_288
            (root / "STATUS.md").write_text(oversized, encoding="utf-8")
            entry = manifest["documents"][1]
            entry["content_sha256"] = hashlib.sha256(oversized.encode()).hexdigest()
            entry["metrics"] = self._metrics(oversized)

            with self.assertRaisesRegex(DocumentLifecycleError, "STATUS.*12 KiB"):
                self._validate(root, manifest)

    def test_master_section_over_twenty_four_kib_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            oversized = "# MASTER\n\n版本：revision 1\n\n## Plan\n\n" + "x" * 24_577
            (root / "MASTER.md").write_text(oversized, encoding="utf-8")
            entry = manifest["documents"][0]
            entry["content_sha256"] = hashlib.sha256(oversized.encode()).hexdigest()
            entry["metrics"] = self._metrics(oversized)

            with self.assertRaisesRegex(
                DocumentLifecycleError, "MASTER section.*24 KiB"
            ):
                self._validate(root, manifest)

    def test_only_master_can_claim_governance_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["authority"]["governance_authority"] = True

            with self.assertRaisesRegex(DocumentLifecycleError, "governance authority"):
                self._validate(root, manifest)

    def test_documents_cannot_claim_active_state_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["authority"]["active_state_authority"] = True

            with self.assertRaisesRegex(
                DocumentLifecycleError, "active state authority"
            ):
                self._validate(root, manifest)

    def test_path_class_and_change_type_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["category"] = "research"

            with self.assertRaisesRegex(DocumentLifecycleError, "document category"):
                self._validate(root, manifest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["change"]["change_type"] = "decision"

            with self.assertRaisesRegex(DocumentLifecycleError, "change_type"):
                self._validate(root, manifest)

    def test_reports_and_projections_cannot_claim_controlled_action_entry(self):
        paths = {
            "report": "docs/reports/current.md",
            "projection": "projections/current.md",
        }
        for category, path in paths.items():
            with (
                self.subTest(category=category),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest = self._valid_repository(root)
                self._write(root, path, f"# {category.title()}\n\nGenerated content.\n")
                entry = self._entry(
                    root,
                    document_id=f"{category}-current",
                    path=path,
                    category=category,
                )
                entry["authority"]["controlled_action_entry"] = True
                manifest["documents"].append(entry)

                with self.assertRaisesRegex(
                    DocumentLifecycleError, "controlled action"
                ):
                    self._validate(root, manifest)

    def test_report_and_projection_require_a_read_only_source_binding(self):
        paths = {
            "report": "docs/reports/current.md",
            "projection": "projections/current.md",
        }
        for category, path in paths.items():
            with (
                self.subTest(category=category),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest = self._valid_repository(root)
                self._write(root, path, f"# {category.title()}\n\nGenerated content.\n")
                manifest["documents"].append(
                    self._entry(
                        root,
                        document_id=f"{category}-current",
                        path=path,
                        category=category,
                    )
                )

                with self.assertRaisesRegex(
                    DocumentLifecycleError, "projection binding"
                ):
                    self._validate(root, manifest)

    def test_authority_reference_must_bind_to_the_manifest_revision_and_category(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][0]["change"]["authority_ref"] = (
                "invented://no-such-event"
            )

            with self.assertRaisesRegex(DocumentLifecycleError, "authority_ref"):
                self._validate(root, manifest)

    def test_research_and_migration_authority_schemes_cannot_cross_categories(self):
        invalid_refs = {
            "evidence-bundle://migration/current",
            "governance-event://master/revision/1",
            f"verification-run://repository/{'0' * 64}",
        }
        for authority_ref in invalid_refs:
            with (
                self.subTest(authority_ref=authority_ref),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest = self._valid_repository(root)
                manifest["documents"][3]["change"]["authority_ref"] = authority_ref

                with self.assertRaisesRegex(DocumentLifecycleError, "authority_ref"):
                    self._validate(root, manifest)

    def test_projection_template_version_must_be_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            self._write(root, "docs/reports/current.md", "# Current\n")
            entry = self._entry(
                root,
                document_id="current-report",
                path="docs/reports/current.md",
                category="report",
            )
            entry["projection_binding"] = {
                "source_state_revision": 1,
                "master_digest": hashlib.sha256(
                    (root / "MASTER.md").read_bytes()
                ).hexdigest(),
                "template_version": "invented-template/v999",
                "content_hash": entry["content_sha256"],
                "state_write_authority": False,
            }
            manifest["documents"].append(entry)

            with self.assertRaisesRegex(
                DocumentLifecycleError, "projection template version"
            ):
                self._validate(root, manifest)

    def test_projection_binding_must_match_current_master_and_governance_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            self._write(root, "docs/reports/current.md", "# Current\n")
            entry = self._entry(
                root,
                document_id="current-report",
                path="docs/reports/current.md",
                category="report",
            )
            entry["change"]["authority_ref"] = "state-revision://repository/2"
            entry["projection_binding"] = {
                "source_state_revision": 2,
                "master_digest": hashlib.sha256(
                    (root / "MASTER.md").read_bytes()
                ).hexdigest(),
                "template_version": "context.document-projection/v1alpha1",
                "content_hash": entry["content_sha256"],
                "state_write_authority": False,
            }
            manifest["documents"].append(entry)

            with self.assertRaisesRegex(
                DocumentLifecycleError, "projection source revision"
            ):
                self._validate(root, manifest)

    def test_projection_binding_rejects_forged_master_and_content_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            self._write(root, "docs/reports/current.md", "# Current\n")
            entry = self._entry(
                root,
                document_id="current-report",
                path="docs/reports/current.md",
                category="report",
            )
            entry["change"]["authority_ref"] = "state-revision://repository/1"
            entry["projection_binding"] = {
                "source_state_revision": 1,
                "master_digest": "0" * 64,
                "template_version": "context.document-projection/v1alpha1",
                "content_hash": "0" * 64,
                "state_write_authority": False,
            }
            manifest["documents"].append(entry)

            with self.assertRaisesRegex(DocumentLifecycleError, "projection binding"):
                self._validate(root, manifest)

    def test_supersedes_must_reference_an_earlier_revision_of_the_same_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][0]["change"]["supersedes"] = (
                "context.document://status/revision/1"
            )

            with self.assertRaisesRegex(DocumentLifecycleError, "supersedes"):
                self._validate(root, manifest)

    def test_revision_after_one_must_supersede_the_immediate_prior_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][2]["document_revision"] = 3

            with self.assertRaisesRegex(
                DocumentLifecycleError, "must supersede revision 2"
            ):
                self._validate(root, manifest)

    def test_supersedes_requires_a_committed_prior_manifest_and_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            entry = manifest["documents"][2]
            entry["document_revision"] = 999
            entry["change"]["supersedes"] = (
                "context.document://target-state/revision/998"
            )
            entry["change"]["supersedes_provenance"] = {
                "git_commit": "0" * 40,
                "manifest_sha256": "0" * 64,
                "content_sha256": "0" * 64,
            }

            with self.assertRaisesRegex(
                DocumentLifecycleError, "supersedes provenance"
            ):
                self._validate(root, manifest)

    def test_committed_prior_manifest_allows_the_next_document_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            profiles = root / "profiles"
            profiles.mkdir()
            prior_manifest_path = profiles / "document-control-manifest.yaml"
            prior_manifest_path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Document Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "document@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin prior manifest"],
                cwd=root,
                check=True,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            prior_manifest_digest = hashlib.sha256(
                prior_manifest_path.read_bytes()
            ).hexdigest()
            target_path = root / "docs/architecture/target-state.md"
            prior_content_digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
            target_content = target_path.read_text(encoding="utf-8") + "\nCurrent.\n"
            target_path.write_text(target_content, encoding="utf-8")
            entry = manifest["documents"][2]
            entry["document_revision"] = 2
            entry["content_sha256"] = hashlib.sha256(
                target_content.encode("utf-8")
            ).hexdigest()
            entry["metrics"] = self._metrics(target_content)
            entry["change"]["supersedes"] = "context.document://target-state/revision/1"
            entry["change"]["supersedes_provenance"] = {
                "git_commit": commit,
                "manifest_sha256": prior_manifest_digest,
                "content_sha256": prior_content_digest,
            }

            self._validate(root, manifest)

            manifest["documents"][2]["document_revision"] = 3
            with self.assertRaisesRegex(
                DocumentLifecycleError, "must supersede revision 2"
            ):
                self._validate(root, manifest)

    def test_sync_finds_last_commit_where_manifest_and_document_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._valid_repository(root)
            self._write(
                root,
                "docs/migrations/m0-10-document-lifecycle-acceptance-2026-08-12.md",
                "# Acceptance\n\nverified: true\n",
            )
            config = build_initial_config(
                root,
                generated_at="2026-08-12T00:00:00Z",
                governance_revision=1,
            )
            manifest = build_document_control_manifest(root, config)
            profiles = root / "profiles"
            profiles.mkdir()
            manifest_path = profiles / "document-control-manifest.yaml"
            config_path = profiles / "document-control-config.yaml"
            manifest_path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Document Test"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "document@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin matching manifest"],
                cwd=root,
                check=True,
            )
            matching_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            target = root / "docs/architecture/target-state.md"
            target.write_text(
                target.read_text(encoding="utf-8") + "\nUntracked revision.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", str(target)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "docs: omit manifest update"],
                cwd=root,
                check=True,
            )
            target.write_text(
                target.read_text(encoding="utf-8") + "\nCurrent correction.\n",
                encoding="utf-8",
            )

            synced = sync_document_control_config(
                root,
                config,
                generated_at="2026-08-12T01:00:00Z",
                governance_revision=1,
            )
            entry = next(
                item
                for item in synced["documents"]
                if item["path"] == "docs/architecture/target-state.md"
            )

            self.assertEqual(entry["document_revision"], 2)
            self.assertEqual(
                entry["change"]["supersedes_provenance"]["git_commit"],
                matching_commit,
            )
            current_manifest = build_document_control_manifest(root, synced)
            self._validate(root, current_manifest)

    def test_committed_document_revision_cannot_reset_to_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "document-control-manifest.yaml").write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Document Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "document@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin prior manifest"],
                cwd=root,
                check=True,
            )
            target_path = root / "docs/architecture/target-state.md"
            target_content = target_path.read_text(encoding="utf-8") + "\nReset.\n"
            target_path.write_text(target_content, encoding="utf-8")
            entry = manifest["documents"][2]
            entry["content_sha256"] = hashlib.sha256(
                target_content.encode("utf-8")
            ).hexdigest()
            entry["metrics"] = self._metrics(target_content)

            with self.assertRaisesRegex(
                DocumentLifecycleError, "committed document revision"
            ):
                self._validate(root, manifest)

            manifest["documents"].pop(2)
            with self.assertRaisesRegex(
                DocumentLifecycleError, "committed document removed"
            ):
                self._validate(root, manifest)

    def test_master_must_contain_exactly_one_active_leaf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            master = (root / "MASTER.md").read_text(encoding="utf-8")
            master += "| M0-11 | 🟡 | second active leaf |\n"
            (root / "MASTER.md").write_text(master, encoding="utf-8")
            manifest["documents"][0]["content_sha256"] = hashlib.sha256(
                master.encode("utf-8")
            ).hexdigest()
            manifest["documents"][0]["metrics"] = self._metrics(master)

            with self.assertRaisesRegex(
                DocumentLifecycleError, "exactly one active leaf"
            ):
                self._validate(root, manifest)

    def test_reference_depth_is_recomputed_from_the_managed_recovery_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            status = (root / "STATUS.md").read_text(encoding="utf-8")
            status += "\n[Evidence index](docs/reports/current.md)\n"
            report = "# Evidence Index\n\n[Acceptance](../migrations/evidence.md)\n"
            self._write(root, "STATUS.md", status)
            self._write(root, "docs/reports/current.md", report)
            manifest["documents"].append(
                self._entry(
                    root,
                    document_id="current-report",
                    path="docs/reports/current.md",
                    category="report",
                )
            )
            manifest["documents"][-1]["projection_binding"] = {
                "source_state_revision": 1,
                "master_digest": "0" * 64,
                "template_version": "context.document-projection/v1alpha1",
                "content_hash": "0" * 64,
                "state_write_authority": False,
            }
            config = self._config_from_manifest(manifest)

            generated = build_document_control_manifest(root, config)
            by_path = {entry["path"]: entry for entry in generated["documents"]}

            self.assertEqual(by_path["STATUS.md"]["metrics"]["reference_depth"], 2)
            self.assertEqual(
                by_path["docs/reports/current.md"]["metrics"]["reference_depth"],
                1,
            )
            self._validate(root, generated)

    def test_reference_cycle_between_expandable_documents_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            status = (root / "STATUS.md").read_text(encoding="utf-8")
            status += "\n[First report](docs/reports/first.md)\n"
            self._write(root, "STATUS.md", status)
            self._write(
                root,
                "docs/reports/first.md",
                "# First\n\n[Second](second.md)\n",
            )
            self._write(
                root,
                "docs/reports/second.md",
                "# Second\n\n[First](first.md)\n",
            )
            manifest["documents"].extend(
                [
                    self._entry(
                        root,
                        document_id="first-report",
                        path="docs/reports/first.md",
                        category="report",
                    ),
                    self._entry(
                        root,
                        document_id="second-report",
                        path="docs/reports/second.md",
                        category="report",
                    ),
                ]
            )

            with self.assertRaisesRegex(DocumentLifecycleError, "reference cycle"):
                build_document_control_manifest(
                    root, self._config_from_manifest(manifest)
                )

    def test_measured_metrics_cannot_be_forged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][1]["metrics"]["utf8_bytes"] = 1

            with self.assertRaisesRegex(
                DocumentLifecycleError, "document metrics drift"
            ):
                self._validate(root, manifest)

    def test_unknown_document_entry_field_is_rejected_by_runtime_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["documents"][0]["unexpected"] = True

            with self.assertRaisesRegex(DocumentLifecycleError, "document fields"):
                self._validate(root, manifest)

    def test_unknown_manifest_top_level_field_is_rejected_by_runtime_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            manifest["unexpected"] = True

            with self.assertRaisesRegex(DocumentLifecycleError, "manifest fields"):
                self._validate(root, manifest)

    def test_empty_documents_fail_before_committed_lineage_is_queried(self):
        root = Path(__file__).parents[1]
        manifest = yaml.safe_load(
            (root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )
        manifest["documents"] = []

        with (
            mock.patch(
                "context_control_plane.document_lifecycle._validate_committed_lineage"
            ) as validate_lineage,
            self.assertRaisesRegex(
                DocumentLifecycleError, "documents must be a non-empty array"
            ),
        ):
            validate_document_control_manifest(root, manifest)

        validate_lineage.assert_not_called()

    def test_malformed_change_receipt_fails_before_committed_lineage_is_queried(self):
        root = Path(__file__).parents[1]
        manifest = yaml.safe_load(
            (root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )
        manifest["documents"][0]["change"] = {"change_type": "correction"}

        with (
            mock.patch(
                "context_control_plane.document_lifecycle._validate_committed_lineage"
            ) as validate_lineage,
            self.assertRaisesRegex(DocumentLifecycleError, "change receipt fields"),
        ):
            validate_document_control_manifest(root, manifest)

        validate_lineage.assert_not_called()

    def test_schema_is_registered_hashed_and_rejects_unknown_fields(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load(
            (root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.document-control-manifest"
        )
        schema_path = root / entry["artifact_path"]
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            entry["content_sha256"],
        )

        document = yaml.safe_load(
            (root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(document)), [])
        unknown = copy.deepcopy(document)
        unknown["unexpected"] = True
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(unknown)))
        missing_binding = copy.deepcopy(document)
        report = next(
            item
            for item in missing_binding["documents"]
            if item["category"] == "report"
        )
        report.pop("projection_binding")
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(missing_binding)))
        forbidden_binding = copy.deepcopy(document)
        policy = next(
            item
            for item in forbidden_binding["documents"]
            if item["category"] == "policy"
        )
        policy["projection_binding"] = copy.deepcopy(
            next(
                item
                for item in forbidden_binding["documents"]
                if item["category"] == "report"
            )["projection_binding"]
        )
        self.assertTrue(
            list(Draft202012Validator(schema).iter_errors(forbidden_binding))
        )

    def test_builder_generates_hashes_metrics_and_evidence_digests_deterministically(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = self._valid_repository(root)
            config = self._config_from_manifest(expected)

            first = build_document_control_manifest(root, config)
            second = build_document_control_manifest(root, copy.deepcopy(config))

        self.assertEqual(first, expected)
        self.assertEqual(
            canonical_manifest_bytes(first), canonical_manifest_bytes(second)
        )

    def test_unregistered_normative_document_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            self._write(root, "docs/policies/unregistered.md", "# Missing\n")

            with self.assertRaisesRegex(
                DocumentLifecycleError, "unregistered normative document"
            ):
                self._validate(root, manifest)

    def test_root_and_unknown_docs_namespace_normative_files_require_registration(self):
        for path in (
            "SECURITY.md",
            "CONTRIBUTING.md",
            "docs/standards/security.md",
            ".gitea/PULL_REQUEST_TEMPLATE.md",
        ):
            with (
                self.subTest(path=path),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest = self._valid_repository(root)
                self._write(root, path, "# Normative Policy\n")

                with self.assertRaisesRegex(
                    DocumentLifecycleError, "unregistered normative document"
                ):
                    self._validate(root, manifest)

    def test_repeated_normative_prose_over_256_bytes_is_rejected(self):
        repeated = "This normative sentence must have one owner. " * 8
        self.assertGreater(len(repeated.encode("utf-8")), 256)
        self.assertLess(len(repeated.encode("utf-8")), 512)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._valid_repository(root)
            for entry in manifest["documents"][2:4]:
                path = root / entry["path"]
                content = path.read_text(encoding="utf-8") + f"\n{repeated}\n"
                path.write_text(content, encoding="utf-8")
                entry["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                entry["metrics"] = self._metrics(content)
                if entry["category"] == "migration":
                    entry["change"]["authority_ref"] = (
                        f"verification-run://repository/{entry['content_sha256']}"
                    )

            with self.assertRaisesRegex(DocumentLifecycleError, "duplicate full prose"):
                self._validate(root, manifest)

    def test_repository_manifest_is_current_and_status_stays_bounded(self):
        root = Path(__file__).parents[1]
        manifest = yaml.safe_load(
            (root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )

        result = validate_document_control_manifest(root, manifest)

        self.assertEqual(result["drifted_documents"], 0)
        self.assertEqual(result["drifted_evidence_refs"], 0)
        self.assertEqual(result["duplicate_prose_blocks"], 0)
        self.assertEqual(result["expired_evidence_refs"], 0)
        self.assertLessEqual((root / "STATUS.md").stat().st_size, 12_288)
        self.assertEqual(
            result["recovery_fields_recovered"], result["recovery_fields_total"]
        )


if __name__ == "__main__":
    unittest.main()
