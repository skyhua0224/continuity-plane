import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.assertion_provenance import compose_assertion_provenance
from context_control_plane.claim_evidence_benchmark import benchmark_claim_evidence
from context_control_plane.claim_evidence_gate import (
    ClaimEvidenceError,
    evaluate_claim_evidence_gate,
    validate_claim_evidence_verdict,
)


class M702ClaimEvidenceGateTests(unittest.TestCase):
    root = Path(__file__).parents[1]
    receipt_payload = (root / "experiments/retrieval/m6-01-retrieval-receipt.json").read_bytes()

    @property
    def receipt_ref(self) -> str:
        return "artifact://sha256/" + hashlib.sha256(self.receipt_payload).hexdigest()

    def artifact_resolver(self, ref: str) -> bytes | None:
        return self.receipt_payload if ref == self.receipt_ref else None

    def resolution(self) -> dict:
        return {"root": self.root, "artifact_resolver": self.artifact_resolver}

    def _assertion(
        self,
        *,
        authority_kind: str = "current_code",
        bearing: bool = True,
        asserted_at: str = "2026-08-16T07:00:01Z",
    ) -> dict:
        payload = (self.root / "context_control_plane/claim_evidence_gate.py").read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        evidence = {
            "evidence_id": "evidence/m7-02/current-gate",
            "authority_kind": authority_kind,
            "source_ref": (
                "repo://context-control-plane/context_control_plane/claim_evidence_gate.py#L1"
                if authority_kind == "current_code"
                else "state://project/demo/revision/7"
            ),
            "revision": f"worktree:sha256:{digest}" if authority_kind == "current_code" else "state:revision:7",
            "sha256": digest,
            "valid_at": "2026-08-16T07:00:00Z",
            "retrieval_receipt_ref": self.receipt_ref,
        }
        return compose_assertion_provenance(
            assertion_id="assertion/m7-02/current-gate",
            assertion_text="The claim evidence gate is present in the current repository.",
            bearing=bearing,
            evidence=[evidence],
            asserted_at=asserted_at,
            valid_until="2026-09-16T07:00:01Z",
            **self.resolution(),
        )

    def claim(self, *, claim_kind: str = "completion") -> dict:
        return {
            "claim_id": "claim/m7-02/acceptance",
            "work_id": "M7-02",
            "claim_kind": claim_kind,
            "statement": "M7-02 completion is supported by current evidence.",
            "evidence_assertion_ids": ["assertion/m7-02/current-gate"],
            "scope_refs": [
                {"scope_kind": "file", "scope_ref": "repo://context-control-plane/context_control_plane/claim_evidence_gate.py"}
            ],
        }

    def resolution_kwargs(self) -> dict:
        return {
            **self.resolution(),
            "current_time": "2026-08-16T07:01:00Z",
        }

    def test_completion_without_evidence_is_denied(self):
        verdict = evaluate_claim_evidence_gate(
            self.claim(), evidence_records=[], **self.resolution_kwargs()
        )
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "missing_evidence")

    def test_path_claim_requires_current_code_authority(self):
        record = self._assertion(authority_kind="industry_standard")
        verdict = evaluate_claim_evidence_gate(
            self.claim(claim_kind="path"),
            evidence_records=[record],
            **self.resolution_kwargs(),
        )
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "required_authority_missing")

    def test_unresolved_official_authority_cannot_standalone(self):
        record = self._assertion(authority_kind="software_official")
        for claim_kind in ("decision", "constraint"):
            verdict = evaluate_claim_evidence_gate(
                self.claim(claim_kind=claim_kind),
                evidence_records=[record],
                **self.resolution_kwargs(),
            )
            with self.subTest(claim_kind=claim_kind):
                self.assertEqual(verdict["decision"], "deny")
                self.assertEqual(verdict["reason"], "required_authority_missing")

    def test_path_claim_requires_a_path_scope(self):
        claim = self.claim(claim_kind="path")
        claim["scope_refs"] = []
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                claim,
                evidence_records=[self._assertion()],
                **self.resolution_kwargs(),
            )

    def test_candidate_or_non_bearing_record_cannot_complete_claim(self):
        for authority_kind in ("memory_candidate", "historical_report"):
            record = self._assertion(authority_kind=authority_kind, bearing=False)
            verdict = evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[record],
                **self.resolution_kwargs(),
            )
            with self.subTest(authority_kind=authority_kind):
                self.assertEqual(verdict["decision"], "deny")
                self.assertEqual(verdict["reason"], "non_bearing_evidence")

    def test_claim_rejects_an_undeclared_assertion(self):
        claim = self.claim()
        claim["evidence_assertion_ids"] = ["assertion/m7-02/different"]
        verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=[self._assertion()],
            **self.resolution_kwargs(),
        )
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "evidence_binding_mismatch")

    def test_assertion_created_after_gate_time_is_rejected(self):
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[
                    self._assertion(asserted_at="2026-08-16T08:00:00Z")
                ],
                **self.resolution_kwargs(),
            )

    def test_unresolved_repo_path_is_denied(self):
        record = self._assertion()
        record["evidence"][0]["source_ref"] = (
            "repo://context-control-plane/context_control_plane/missing.py#L1"
        )
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[record],
                **self.resolution_kwargs(),
            )

    def test_unresolved_claim_scope_is_denied_even_with_real_code_evidence(self):
        claim = self.claim(claim_kind="path")
        claim["scope_refs"][0]["scope_ref"] = (
            "repo://context-control-plane/context_control_plane/missing.py"
        )
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                claim,
                evidence_records=[self._assertion()],
                **self.resolution_kwargs(),
            )

    def test_path_claim_requires_evidence_for_the_claimed_path(self):
        claim = self.claim(claim_kind="path")
        claim["scope_refs"][0]["scope_ref"] = (
            "repo://context-control-plane/context_control_plane/assertion_provenance.py"
        )
        verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=[self._assertion()],
            **self.resolution_kwargs(),
        )
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "path_evidence_mismatch")

    def test_wrong_code_revision_or_missing_artifact_is_denied(self):
        wrong_revision = self._assertion()
        wrong_revision["evidence"][0]["revision"] = "worktree:sha256:" + "0" * 64
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[wrong_revision],
                **self.resolution_kwargs(),
            )
        with self.assertRaises(ClaimEvidenceError):
            evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[self._assertion()],
                root=self.root,
                artifact_resolver=lambda _ref: None,
                current_time="2026-08-16T07:01:00Z",
            )

    def test_valid_completion_is_allowed_without_state_authority(self):
        record = self._assertion()
        verdict = evaluate_claim_evidence_gate(
            self.claim(),
            evidence_records=[record],
            **self.resolution_kwargs(),
        )
        self.assertEqual(verdict["decision"], "allow")
        self.assertEqual(verdict["reason"], "evidence_satisfied")
        self.assertFalse(verdict["state_write_authority"])
        self.assertFalse(verdict["completion_authority"])

    def test_gate_verdict_is_strict_and_digest_bound(self):
        record = self._assertion()
        verdict = evaluate_claim_evidence_gate(
            self.claim(), evidence_records=[record], **self.resolution_kwargs()
        )
        invalid = copy.deepcopy(verdict)
        invalid["extra"] = True
        with self.assertRaises(ClaimEvidenceError):
            validate_claim_evidence_verdict(invalid)

    def test_resealed_allow_without_evidence_is_rejected(self):
        verdict = evaluate_claim_evidence_gate(
            self.claim(), evidence_records=[self._assertion()], **self.resolution_kwargs()
        )
        verdict["matched_authority_kinds"] = []
        verdict["evidence_ids"] = []
        unsigned = copy.deepcopy(verdict)
        unsigned.pop("verdict_sha256")
        verdict["verdict_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.assertRaises(ClaimEvidenceError):
            validate_claim_evidence_verdict(verdict)

    def test_gate_and_benchmark_schemas_are_registered_and_hashed(self):
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        expected = {
            "context.claim-evidence-gate": "schemas/m7-02/claim-evidence-gate.schema.json",
            "context.claim-evidence-benchmark": "schemas/m7-02/claim-evidence-benchmark.schema.json",
        }
        for schema_id, relative_path in expected.items():
            entry = next(item for item in registry["schemas"] if item["schema_id"] == schema_id)
            path = self.root / relative_path
            self.assertEqual(entry["artifact_path"], relative_path)
            self.assertEqual(entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self.assertFalse(schema["additionalProperties"])
        gate_schema = json.loads(
            (self.root / expected["context.claim-evidence-gate"]).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(gate_schema).validate(
            evaluate_claim_evidence_gate(
                self.claim(),
                evidence_records=[self._assertion()],
                **self.resolution_kwargs(),
            )
        )

    def test_fixed_benchmark_has_zero_false_allows_and_replays(self):
        first = benchmark_claim_evidence(samples=1000, root=self.root)
        second = benchmark_claim_evidence(samples=1000, root=self.root)
        self.assertEqual(first["samples"], 1000)
        self.assertEqual(first["successful_samples"], 1000)
        self.assertEqual(first["false_allow_count"], 0)
        self.assertEqual(first["false_deny_count"], 0)
        self.assertEqual(first["outcomes_sha256"], second["outcomes_sha256"])
        self.assertFalse(first["state_write_authority"])
        self.assertFalse(first["completion_authority"])

    def test_committed_benchmark_receipt_replays_exactly(self):
        path = self.root / "experiments/evidence/m7-02-claim-evidence-results.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        replayed = benchmark_claim_evidence(samples=1000, root=self.root)
        self.assertEqual(committed, replayed)
        schema = json.loads(
            (self.root / "schemas/m7-02/claim-evidence-benchmark.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(committed)


if __name__ == "__main__":
    unittest.main()
