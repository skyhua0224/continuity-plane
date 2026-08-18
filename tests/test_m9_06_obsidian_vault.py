"""M9-06 generated Obsidian vault contract."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.context_health_projection import (
    build_context_health_projection,
)
from context_control_plane.external_state_provider import (
    HMACExternalStateProjectionSigner,
)
from context_control_plane.obsidian_vault import (
    ObsidianVaultError,
    build_obsidian_vault,
    validate_obsidian_vault,
    write_obsidian_vault,
)
from context_control_plane.project_graph_projection import (
    build_project_graph_projection,
)
from tests.test_m9_04_context_health_projection import NOW, M904Fixture


def _canonical_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _signed_projection(
    signer: HMACExternalStateProjectionSigner,
    *,
    schema_version: str,
    authority: dict,
    extra: dict,
) -> dict:
    projection = {
        "schema_version": schema_version,
        "project_id": "project-m9-06",
        "state_revision": 9,
        "state_sha256": "a" * 64,
        "source_projection_sha256": "b" * 64,
        "authority": authority,
        **extra,
    }
    projection["projection_sha256"] = _canonical_digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def _resign_projection(
    projection: dict,
    signer: HMACExternalStateProjectionSigner,
) -> None:
    unsigned = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = _canonical_digest(unsigned)
    projection["signature"] = signer.sign(
        {**unsigned, "projection_sha256": projection["projection_sha256"]}
    )


class M906ObsidianVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-06-test",
            secret=b"m9-06-obsidian-vault-test-key",
        )
        self.graph = _signed_projection(
            self.signer,
            schema_version="context.project-graph-projection/v1alpha1",
            authority={
                "state_write_authority": False,
                "controlled_action_authority": False,
                "provider_authority": 0,
                "external_effect_authority": 0,
            },
            extra={
                "governance_ref": "governance://master/74",
                "active_work_set": [{"work_id": "M9-06", "status": "active"}],
                "work_ledger": {"work_count": 1, "open_blocker_count": 0},
                "health": {"cycle_work_ids": [], "orphan_work_ids": []},
            },
        )
        self.decisions = _signed_projection(
            self.signer,
            schema_version="context.decision-evidence-projection/v1alpha1",
            authority={
                "state_write_authority": False,
                "completion_authority": False,
                "approval_authority": False,
                "provider_authority": 0,
                "external_effect_authority": 0,
            },
            extra={
                "governance_ref": "governance://master/74",
                "decision_timeline": [
                    {
                        "decision_id": "decision-current",
                        "statement": "Keep source-bound vaults",
                    }
                ],
                "constraint_matrix": [],
                "evidence_matrix": [],
                "health": {"current_decision_ids": ["decision-current"]},
            },
        )
        self.health = _signed_projection(
            self.signer,
            schema_version="context.context-health-projection/v1alpha1",
            authority={
                "state_write_authority": False,
                "completion_authority": False,
                "approval_authority": False,
                "provider_authority": 0,
                "external_effect_authority": 0,
            },
            extra={
                "source_projection_sha256": self.graph["source_projection_sha256"],
                "decision_evidence_projection_sha256": self.decisions[
                    "projection_sha256"
                ],
                "provider_id": "provider-m9-06",
                "slo": [{"name": "restore", "status": "pass"}],
                "drilldowns": [],
                "health": {"status": "healthy"},
            },
        )

    def build(self) -> dict:
        return build_obsidian_vault(
            project_graph=self.graph,
            decision_evidence=self.decisions,
            context_health=self.health,
            signer=self.signer,
            generated_at="2026-08-17T23:59:00Z",
        )

    def test_vault_binds_signed_same_revision_sources_and_read_only_files(self) -> None:
        vault = self.build()

        self.assertEqual(vault["project_id"], "project-m9-06")
        self.assertEqual(vault["state_revision"], 9)
        self.assertEqual(vault["template_version"], "context.obsidian-vault/v1alpha1")
        self.assertEqual(vault["authority"]["state_write_authority"], False)
        self.assertEqual(
            [entry["path"] for entry in vault["files"]],
            [
                "00 Context Control Plane.md",
                "10 Project Graph.md",
                "20 Decisions and Evidence.md",
                "30 Context Health.md",
            ],
        )
        self.assertEqual(
            vault["sources"]["context_health_projection_sha256"],
            self.health["projection_sha256"],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            write_obsidian_vault(root, vault, signer=self.signer)
            self.assertEqual(
                validate_obsidian_vault(root, vault, signer=self.signer), vault
            )

    def test_mismatched_or_unsigned_projection_is_rejected_before_render(self) -> None:
        wrong_revision = copy.deepcopy(self.decisions)
        wrong_revision["state_revision"] = 10
        _resign_projection(wrong_revision, self.signer)
        with self.assertRaisesRegex(ObsidianVaultError, "revision"):
            build_obsidian_vault(
                project_graph=self.graph,
                decision_evidence=wrong_revision,
                context_health=self.health,
                signer=self.signer,
                generated_at="2026-08-17T23:59:00Z",
            )

        forged = copy.deepcopy(self.graph)
        forged["active_work_set"][0]["status"] = "completed"
        unsigned = {
            key: value
            for key, value in forged.items()
            if key not in {"projection_sha256", "signature"}
        }
        forged["projection_sha256"] = _canonical_digest(unsigned)
        with self.assertRaisesRegex(ObsidianVaultError, "signature"):
            build_obsidian_vault(
                project_graph=forged,
                decision_evidence=self.decisions,
                context_health=self.health,
                signer=self.signer,
                generated_at="2026-08-17T23:59:00Z",
            )

    def test_generated_file_tampering_or_manual_markdown_fails_closed(self) -> None:
        vault = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            write_obsidian_vault(root, vault, signer=self.signer)
            generated = root / "10 Project Graph.md"
            generated.write_text(
                generated.read_text(encoding="utf-8") + "edited\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ObsidianVaultError, "digest"):
                validate_obsidian_vault(root, vault, signer=self.signer)

            second_root = Path(directory) / "vault-with-note"
            write_obsidian_vault(second_root, vault, signer=self.signer)
            (second_root / "human-note.md").write_text(
                "change active work\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ObsidianVaultError, "file set"):
                validate_obsidian_vault(second_root, vault, signer=self.signer)

    def test_manifest_tampering_is_rejected_after_public_hashes_are_recomputed(
        self,
    ) -> None:
        vault = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            write_obsidian_vault(root, vault, signer=self.signer)
            manifest_path = root / ".context-control-plane.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = manifest["files"][0]
            entry["content"] += "forged\n"
            content = entry["content"].encode("utf-8")
            entry["content_sha256"] = hashlib.sha256(content).hexdigest()
            entry["utf8_bytes"] = len(content)
            unsigned = {
                key: value
                for key, value in manifest.items()
                if key not in {"vault_sha256", "signature"}
            }
            manifest["vault_sha256"] = _canonical_digest(unsigned)
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ObsidianVaultError, "signature"):
                validate_obsidian_vault(root, signer=self.signer)

    def test_existing_unvalidated_vault_is_never_overwritten(self) -> None:
        vault = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            root.mkdir()
            (root / "human-note.md").write_text("preserve me\n", encoding="utf-8")
            with self.assertRaisesRegex(ObsidianVaultError, "non-empty"):
                write_obsidian_vault(root, vault, signer=self.signer)

    def test_oversized_generated_file_is_rejected_before_manifest_creation(
        self,
    ) -> None:
        oversized_graph = copy.deepcopy(self.graph)
        oversized_graph["work_ledger"]["overflow"] = "x" * (4 * 1024 * 1024)
        _resign_projection(oversized_graph, self.signer)

        with self.assertRaisesRegex(ObsidianVaultError, "file size"):
            build_obsidian_vault(
                project_graph=oversized_graph,
                decision_evidence=self.decisions,
                context_health=self.health,
                signer=self.signer,
                generated_at="2026-08-17T23:59:00Z",
            )

    def test_real_m9_projections_render_health_reference_harness_and_replay(
        self,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            artifact_store = LocalArtifactStore(Path(directory) / "artifacts")
            artifact_store.initialize()
            fixture = M904Fixture(repository_root, artifact_store)
            graph = build_project_graph_projection(
                fixture.source_projection,
                signer=fixture.signer,
                observed_at=NOW,
            )
            health = build_context_health_projection(
                source_projection=fixture.source_projection,
                decision_evidence_projection=fixture.decision_projection,
                provenance_bundle=fixture.provenance_bundle,
                trace_events=fixture.trace_events,
                compaction_records=fixture.compaction_records,
                accounting_records=fixture.accounting_records,
                reference_records=fixture.reference_records,
                harness_runs=fixture.harness_runs,
                harness_events=fixture.harness_events,
                recovery_records=fixture.recovery_records,
                artifact_store=fixture.artifact_store,
                provider_id="provider-m9-06-health",
                observed_at=NOW,
                signer=fixture.signer,
                evidence_resolver=fixture.evidence_resolver,
                artifact_resolver=fixture.artifact_resolver,
                trusted_time_verifier=fixture.trusted_time_verifier,
            )
            vault = build_obsidian_vault(
                project_graph=graph,
                decision_evidence=fixture.decision_projection,
                context_health=health,
                signer=fixture.signer,
                generated_at=NOW,
            )

        documents = {item["path"]: item["content"] for item in vault["files"]}
        self.assertIn('"graph"', documents["10 Project Graph.md"])
        self.assertIn('"context_health"', documents["30 Context Health.md"])
        self.assertIn('"reference_health"', documents["30 Context Health.md"])
        self.assertIn('"harness_health"', documents["30 Context Health.md"])
        self.assertIn('"replay_health"', documents["30 Context Health.md"])


if __name__ == "__main__":
    unittest.main()
