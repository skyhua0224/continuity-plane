import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from context_control_plane.assertion_provenance import validate_assertion_provenance
from context_control_plane.codegraph_verification import validate_codegraph_receipt
from context_control_plane.m6_acceptance_fixtures import (
    build_m6_acceptance_fixtures,
    canonical_m6_semantic_bundle_bytes,
)
from context_control_plane.mcp_admission import (
    validate_mcp_admission_decision,
    validate_mcp_registry_snapshot,
)
from context_control_plane.memory_ablation import validate_memory_ablation
from context_control_plane.recall_provider import validate_recall_receipt
from context_control_plane.retrieval_benchmark import validate_retrieval_benchmark
from context_control_plane.retrieval_routing import (
    validate_retrieval_plan,
    validate_retrieval_receipt,
)
from context_control_plane.reviewer_adapter import validate_review_receipt


class M6AcceptanceFixtureTests(unittest.TestCase):
    root = Path(__file__).parents[1]
    expected: ClassVar[set[str]] = {
        "m6-01-retrieval-results.json",
        "m6-01-retrieval-plan.json",
        "m6-01-retrieval-receipt.json",
        "m6-02-codegraph-verification.json",
        "m6-03-recall-completed.json",
        "m6-03-recall-503.json",
        "m6-04-memory-ablation.json",
        "m6-05-review-local.json",
        "m6-05-review-timeout.json",
        "m6-06-registry-snapshot.json",
        "m6-06-read-admission.json",
        "m6-06-unauthorized-write-admission.json",
        "m6-07-assertion-provenance.json",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = build_m6_acceptance_fixtures(samples=100)

    @classmethod
    def _assertion_resolution(
        cls, fixtures: dict[str, dict], *, use_root: bool = False
    ) -> dict:
        results = fixtures["m6-01-retrieval-results.json"]
        receipt = fixtures["m6-01-retrieval-receipt.json"]
        results_bytes = (
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        receipt_bytes = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        source_ref = (
            "repo://context-control-plane/"
            "experiments/retrieval/m6-01-retrieval-results.json"
        )
        revision = "worktree:sha256:" + hashlib.sha256(results_bytes).hexdigest()
        receipt_ref = "artifact://sha256/" + hashlib.sha256(receipt_bytes).hexdigest()

        def artifact_resolver(ref: str) -> bytes | None:
            return receipt_bytes if ref == receipt_ref else None

        if use_root:
            return {"root": cls.root, "artifact_resolver": artifact_resolver}

        def evidence_resolver(ref: str, candidate_revision: str) -> bytes | None:
            if ref == source_ref and candidate_revision == revision:
                return results_bytes
            return None

        return {
            "evidence_resolver": evidence_resolver,
            "artifact_resolver": artifact_resolver,
        }

    def test_bundle_contains_independently_valid_receipts(self) -> None:
        self.assertEqual(set(self.fixtures), self.expected)
        validate_retrieval_benchmark(self.fixtures["m6-01-retrieval-results.json"])
        plan = self.fixtures["m6-01-retrieval-plan.json"]
        validate_retrieval_plan(plan)
        validate_retrieval_receipt(
            self.fixtures["m6-01-retrieval-receipt.json"],
            trusted_plan=plan,
            root=self.root,
        )
        validate_codegraph_receipt(
            self.fixtures["m6-02-codegraph-verification.json"], root=self.root
        )
        validate_recall_receipt(self.fixtures["m6-03-recall-completed.json"])
        validate_recall_receipt(self.fixtures["m6-03-recall-503.json"])
        validate_memory_ablation(self.fixtures["m6-04-memory-ablation.json"])
        validate_review_receipt(self.fixtures["m6-05-review-local.json"])
        validate_review_receipt(self.fixtures["m6-05-review-timeout.json"])
        validate_mcp_registry_snapshot(self.fixtures["m6-06-registry-snapshot.json"])
        validate_mcp_admission_decision(self.fixtures["m6-06-read-admission.json"])
        validate_mcp_admission_decision(
            self.fixtures["m6-06-unauthorized-write-admission.json"]
        )
        validate_assertion_provenance(
            self.fixtures["m6-07-assertion-provenance.json"],
            current_time="2026-08-15T07:30:00Z",
            **self._assertion_resolution(self.fixtures),
        )

    def test_unauthorized_write_fixture_activates_no_write_tools(self) -> None:
        decision = self.fixtures["m6-06-unauthorized-write-admission.json"]
        active_scopes = {
            tool["scope"]
            for server in decision["servers"]
            for tool in server["active_tools"]
        }
        self.assertNotIn("state_write", active_scopes)
        self.assertNotIn("external_effect", active_scopes)

    def test_committed_receipts_match_current_implementations(self) -> None:
        directory = self.root / "experiments/retrieval"
        committed = {
            path.name: json.loads(path.read_text()) for path in directory.glob("m6-*.json")
        }
        self.assertEqual(set(committed), self.expected)
        validate_retrieval_benchmark(
            committed["m6-01-retrieval-results.json"], root=self.root
        )
        plan = committed["m6-01-retrieval-plan.json"]
        validate_retrieval_plan(plan)
        validate_retrieval_receipt(
            committed["m6-01-retrieval-receipt.json"],
            trusted_plan=plan,
            root=self.root,
        )
        validate_memory_ablation(
            committed["m6-04-memory-ablation.json"], root=self.root
        )
        self.assertEqual(
            committed["m6-01-retrieval-results.json"]["implementation_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/retrieval_routing.py").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            committed["m6-04-memory-ablation.json"]["subject_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/recall_provider.py").read_bytes()
            ).hexdigest(),
        )
        validate_codegraph_receipt(
            committed["m6-02-codegraph-verification.json"], root=self.root
        )
        validate_recall_receipt(committed["m6-03-recall-completed.json"])
        validate_recall_receipt(committed["m6-03-recall-503.json"])
        validate_review_receipt(committed["m6-05-review-local.json"])
        validate_review_receipt(committed["m6-05-review-timeout.json"])
        validate_mcp_registry_snapshot(committed["m6-06-registry-snapshot.json"])
        validate_mcp_admission_decision(committed["m6-06-read-admission.json"])
        validate_mcp_admission_decision(
            committed["m6-06-unauthorized-write-admission.json"]
        )
        assertion = committed["m6-07-assertion-provenance.json"]
        validate_assertion_provenance(
            assertion,
            current_time="2026-08-15T07:30:00Z",
            **self._assertion_resolution(committed, use_root=True),
        )
        retrieval_receipt_bytes = json.dumps(
            committed["m6-01-retrieval-receipt.json"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            assertion["evidence"][0]["retrieval_receipt_ref"],
            "artifact://sha256/" + hashlib.sha256(retrieval_receipt_bytes).hexdigest(),
        )
        retrieval_bytes = (
            json.dumps(
                committed["m6-01-retrieval-results.json"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            assertion["evidence"][0]["sha256"],
            hashlib.sha256(retrieval_bytes).hexdigest(),
        )

    def test_cli_publishes_all_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    str(self.root / "tools/generate_m6_acceptance_fixtures.py"),
                    "--samples",
                    "100",
                    "--output-dir",
                    temporary,
                ],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            paths = {path.name for path in Path(temporary).glob("*.json")}
            self.assertEqual(paths, self.expected)
            for path in Path(temporary).glob("*.json"):
                self.assertIsInstance(json.loads(path.read_text()), dict)

    def test_regeneration_has_stable_semantic_payload(self) -> None:
        replay = build_m6_acceptance_fixtures(samples=100)
        replay["m6-01-retrieval-results.json"]["p50_ms"] += 1
        replay["m6-01-retrieval-results.json"]["p95_ms"] += 1
        replay["m6-01-retrieval-results.json"]["max_ms"] += 1
        self.assertEqual(
            canonical_m6_semantic_bundle_bytes(self.fixtures),
            canonical_m6_semantic_bundle_bytes(replay),
        )


if __name__ == "__main__":
    unittest.main()
