import copy
import hashlib
import unittest
from pathlib import Path

from context_control_plane.assertion_provenance import (
    AssertionProvenanceError,
    compose_assertion_provenance,
    validate_assertion_provenance,
)


class M701AssertionProvenanceTests(unittest.TestCase):
    root = Path(__file__).parents[1]
    receipt_payload = (
        root / "experiments/retrieval/m6-01-retrieval-receipt.json"
    ).read_bytes()

    @property
    def receipt_ref(self) -> str:
        return "artifact://sha256/" + hashlib.sha256(
            self.receipt_payload
        ).hexdigest()

    def artifact_resolver(self, ref: str) -> bytes | None:
        return self.receipt_payload if ref == self.receipt_ref else None

    def resolution(self) -> dict:
        return {"root": self.root, "artifact_resolver": self.artifact_resolver}

    def evidence(self) -> list[dict]:
        payload = (
            self.root / "context_control_plane/assertion_provenance.py"
        ).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return [
            {
                "evidence_id": "evidence/retrieval-router",
                "authority_kind": "current_code",
                "source_ref": (
                    "repo://context-control-plane/"
                    "context_control_plane/assertion_provenance.py#L1"
                ),
                "revision": f"worktree:sha256:{digest}",
                "sha256": digest,
                "valid_at": "2026-08-15T07:00:00Z",
                "retrieval_receipt_ref": self.receipt_ref,
            }
        ]

    def test_bearing_assertion_requires_current_provenance(self) -> None:
        record = compose_assertion_provenance(
            assertion_id="assertion/m6-07/bounded-retrieval",
            assertion_text="Bounded retrieval reduced duplicate fixture reads by 50 percent.",
            bearing=True,
            evidence=self.evidence(),
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-09-15T07:00:01Z",
            **self.resolution(),
        )
        validate_assertion_provenance(
            record,
            current_time="2026-08-15T07:01:00Z",
            **self.resolution(),
        )
        self.assertEqual(record["provenance_coverage"], 1.0)
        self.assertFalse(record["state_write_authority"])

    def test_memory_candidate_cannot_support_bearing_assertion(self) -> None:
        evidence = self.evidence()
        evidence[0]["authority_kind"] = "memory_candidate"
        with self.assertRaisesRegex(AssertionProvenanceError, "candidate"):
            compose_assertion_provenance(
                assertion_id="assertion/m6-07/invalid",
                assertion_text="A historical candidate is current fact.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_expired_assertion_fails_current_validation(self) -> None:
        record = compose_assertion_provenance(
            assertion_id="assertion/m6-07/expired",
            assertion_text="This assertion has expired.",
            bearing=True,
            evidence=self.evidence(),
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-08-15T07:00:02Z",
            **self.resolution(),
        )
        with self.assertRaisesRegex(AssertionProvenanceError, "expired"):
            validate_assertion_provenance(
                record,
                current_time="2026-08-15T07:01:00Z",
                **self.resolution(),
            )

    def test_record_is_strict_and_digest_bound(self) -> None:
        record = compose_assertion_provenance(
            assertion_id="assertion/m6-07/strict",
            assertion_text="Every bearing assertion has current provenance.",
            bearing=True,
            evidence=self.evidence(),
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-09-15T07:00:01Z",
            **self.resolution(),
        )
        invalid = copy.deepcopy(record)
        invalid["accepted"] = True
        with self.assertRaises(AssertionProvenanceError):
            validate_assertion_provenance(invalid, **self.resolution())
        invalid = copy.deepcopy(record)
        invalid["evidence"][0]["sha256"] = "2" * 64
        with self.assertRaisesRegex(AssertionProvenanceError, "digest"):
            validate_assertion_provenance(invalid, **self.resolution())

    def test_bearing_evidence_cannot_postdate_the_assertion(self) -> None:
        evidence = self.evidence()
        evidence[0]["valid_at"] = "2026-08-15T07:00:02Z"
        with self.assertRaisesRegex(AssertionProvenanceError, "postdates"):
            compose_assertion_provenance(
                assertion_id="assertion/m6-07/future-evidence",
                assertion_text="Future evidence cannot support a current assertion.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_bearing_evidence_requires_content_addressed_retrieval_receipt(self) -> None:
        evidence = self.evidence()
        evidence[0]["retrieval_receipt_ref"] = "receipt://missing"
        with self.assertRaisesRegex(AssertionProvenanceError, "retrieval_receipt_ref"):
            compose_assertion_provenance(
                assertion_id="assertion/m6-07/dangling-receipt",
                assertion_text="A dangling receipt cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_current_code_bearing_without_resolver_fails_closed(self) -> None:
        with self.assertRaisesRegex(AssertionProvenanceError, "resolver"):
            compose_assertion_provenance(
                assertion_id="assertion/m6-07/unresolved-code",
                assertion_text="Unresolved code cannot support a bearing assertion.",
                bearing=True,
                evidence=self.evidence(),
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
            )

    def test_current_code_bearing_rejects_nonexistent_repo_path(self) -> None:
        evidence = self.evidence()
        evidence[0]["source_ref"] = (
            "repo://context-control-plane/context_control_plane/does-not-exist.py#L1"
        )
        with self.assertRaisesRegex(AssertionProvenanceError, "source_ref"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/missing-code",
                assertion_text="Missing code cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_current_code_bearing_rejects_unbound_revision(self) -> None:
        evidence = self.evidence()
        evidence[0]["revision"] = "worktree:sha256:" + "0" * 64
        with self.assertRaisesRegex(AssertionProvenanceError, "revision"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/false-revision",
                assertion_text="An unrelated revision cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_current_code_bearing_rejects_content_digest_mismatch(self) -> None:
        evidence = self.evidence()
        evidence[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(AssertionProvenanceError, "evidence digest"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/false-content",
                assertion_text="Unmatched content cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_current_code_bearing_rejects_unresolved_retrieval_receipt(self) -> None:
        evidence = self.evidence()
        evidence[0]["retrieval_receipt_ref"] = "artifact://sha256/" + "0" * 64
        with self.assertRaisesRegex(AssertionProvenanceError, "retrieval receipt"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/missing-receipt",
                assertion_text="A missing receipt cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                **self.resolution(),
            )

    def test_current_code_bearing_rejects_invalid_retrieval_receipt(self) -> None:
        invalid_receipt = b"{}"
        evidence = self.evidence()
        evidence[0]["retrieval_receipt_ref"] = (
            "artifact://sha256/" + hashlib.sha256(invalid_receipt).hexdigest()
        )
        with self.assertRaisesRegex(AssertionProvenanceError, "retrieval receipt"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/invalid-receipt",
                assertion_text="An invalid receipt cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                root=self.root,
                artifact_resolver=lambda ref: (
                    invalid_receipt
                    if ref == evidence[0]["retrieval_receipt_ref"]
                    else None
                ),
            )

    def test_current_state_bearing_requires_revision_bound_resolver(self) -> None:
        state_payload = b'{"project_id":"context-control-plane","revision":56}'
        evidence = self.evidence()
        evidence[0].update(
            {
                "authority_kind": "current_state",
                "source_ref": "state://context-control-plane/revision/56",
                "revision": "state:56",
                "sha256": hashlib.sha256(state_payload).hexdigest(),
            }
        )

        def state_resolver(source_ref: str, revision: str) -> bytes | None:
            if (
                source_ref == "state://context-control-plane/revision/56"
                and revision == "state:56"
            ):
                return state_payload
            return None

        record = compose_assertion_provenance(
            assertion_id="assertion/m7-01/current-state",
            assertion_text="The current State revision is resolver-backed.",
            bearing=True,
            evidence=evidence,
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-09-15T07:00:01Z",
            evidence_resolver=state_resolver,
            artifact_resolver=self.artifact_resolver,
        )
        validate_assertion_provenance(
            record,
            evidence_resolver=state_resolver,
            artifact_resolver=self.artifact_resolver,
        )
        evidence[0]["revision"] = "state:55"
        with self.assertRaisesRegex(AssertionProvenanceError, "resolve"):
            compose_assertion_provenance(
                assertion_id="assertion/m7-01/stale-state",
                assertion_text="A stale State revision cannot establish provenance.",
                bearing=True,
                evidence=evidence,
                asserted_at="2026-08-15T07:00:01Z",
                valid_until="2026-09-15T07:00:01Z",
                evidence_resolver=state_resolver,
                artifact_resolver=self.artifact_resolver,
            )


if __name__ == "__main__":
    unittest.main()
