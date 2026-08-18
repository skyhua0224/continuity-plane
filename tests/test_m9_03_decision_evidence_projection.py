"""M9-03 Decision Timeline and Evidence Matrix projection tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from context_control_plane.assertion_provenance import compose_assertion_provenance
from context_control_plane.claim_evidence_gate import evaluate_claim_evidence_gate
from context_control_plane.decision_evidence_projection import (
    DecisionEvidenceProjectionError,
    build_decision_evidence_projection,
    validate_decision_evidence_projection,
)
from context_control_plane.experiment_lifecycle import experiment_contract_sha256
from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_review import (
    add_idea_relationship,
    apply_idea_review,
    migrate_typed_state_v3_to_v4,
    open_correction_protection,
    upsert_idea_observation,
)
from context_control_plane.state_mcp import RequestContext
from context_control_plane.typed_state import validate_typed_state


class _Source:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(snapshot)

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {"adapter_id": "context.m9-03-test"},
            },
            "error": None,
        }


def _snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [
        {
            "evidence_id": "evidence-old",
            "kind": "test",
            "artifact_ref": "artifact://sha256/" + "1" * 64,
            "content_sha256": "1" * 64,
            "validity": "verified",
            "observed_at": "2026-08-14T06:00:00+08:00",
            "verified_at": "2026-08-14T06:05:00+08:00",
        },
        {
            "evidence_id": "evidence-current",
            "kind": "source-code",
            "artifact_ref": "artifact://sha256/" + "2" * 64,
            "content_sha256": "2" * 64,
            "validity": "verified",
            "observed_at": "2026-08-14T07:00:00+08:00",
            "verified_at": "2026-08-14T07:05:00+08:00",
        },
    ]
    snapshot["decisions"] = [
        {
            "decision_id": "decision-old",
            "work_id": "work-active",
            "status": "superseded",
            "statement": "Use the old path",
            "decided_at": "2026-08-14T06:10:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": ["evidence-old"],
        },
        {
            "decision_id": "decision-current",
            "work_id": "work-active",
            "status": "accepted",
            "statement": "Use the current path",
            "decided_at": "2026-08-14T07:10:00+08:00",
            "supersedes_decision_id": "decision-old",
            "evidence_ids": ["evidence-current"],
        },
    ]
    snapshot["project"]["current_decision_ids"] = ["decision-current"]
    validate_typed_state(snapshot)
    return snapshot


def _external_projection(
    snapshot: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
    *,
    request_id: str,
) -> dict[str, Any]:
    response = ExternalStateProjectionProvider(
        _Source(snapshot),
        provider_id="provider-m9-03-test",
        signer=signer,
    ).call_tool(
        EXTERNAL_READ_TOOL,
        {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
        },
        context=RequestContext("actor-reader", "authorization-reader"),
    )
    if not response["ok"]:
        raise AssertionError(response["error"])
    return response["result"]


def _bundle_digest(bundle: dict[str, Any]) -> str:
    body = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    return hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _resign_projection(
    projection: dict[str, Any], signer: HMACExternalStateProjectionSigner
) -> None:
    body = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    projection["signature"] = signer.sign(
        {**body, "projection_sha256": projection["projection_sha256"]}
    )


class M903DecisionEvidenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-03-test",
            secret=b"m9-03-decision-evidence-test-key",
        )

    def test_projection_preserves_decision_history_and_evidence_links(self) -> None:
        snapshot = _snapshot()

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-test"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )

        self.assertEqual(projection["state_revision"], snapshot["project"]["revision"])
        self.assertEqual(
            [item["decision_id"] for item in projection["decision_timeline"]],
            ["decision-old", "decision-current"],
        )
        self.assertEqual(
            projection["decision_timeline"][1]["supersedes_decision_id"],
            "decision-old",
        )
        self.assertEqual(
            projection["decision_timeline"][0]["superseded_by_decision_ids"],
            ["decision-current"],
        )
        self.assertTrue(projection["decision_timeline"][1]["is_current"])
        matrix = {
            item["evidence_id"]: item for item in projection["evidence_matrix"]
        }
        self.assertEqual(matrix["evidence-current"]["display_status"], "current")
        self.assertEqual(
            matrix["evidence-current"]["referencing_objects"],
            [
                {
                    "object_kind": "decision",
                    "object_id": "decision-current",
                    "reference_path": "evidence_ids",
                    "object_status": "accepted",
                    "reference_state": "current",
                }
            ],
        )
        self.assertEqual(matrix["evidence-old"]["display_status"], "superseded")
        self.assertEqual(
            projection["authority"],
            {
                "state_write_authority": False,
                "completion_authority": False,
                "approval_authority": False,
                "provider_authority": 0,
                "external_effect_authority": 0,
            },
        )

    def test_state_only_projection_is_explicitly_metadata_only(self) -> None:
        snapshot = _snapshot()
        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-metadata"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )

        self.assertEqual(
            projection["capabilities"],
            {
                "typed_evidence": True,
                "assertion_provenance": False,
                "claim_evidence": False,
                "provenance_mode": "metadata-only",
            },
        )
        current = next(
            item
            for item in projection["evidence_matrix"]
            if item["evidence_id"] == "evidence-current"
        )
        self.assertEqual(current["provenance"], [])

    def test_validated_m7_bundle_binds_assertion_claim_and_retrieval_receipt(self) -> None:
        snapshot = _snapshot()
        state_payload = b"M9-03 current State evidence"
        state_digest = hashlib.sha256(state_payload).hexdigest()
        current = next(
            item
            for item in snapshot["evidence"]
            if item["evidence_id"] == "evidence-current"
        )
        current["content_sha256"] = state_digest
        current["artifact_ref"] = f"artifact://sha256/{state_digest}"
        receipt_payload = (
            Path(__file__).parents[1]
            / "experiments/retrieval/m6-01-retrieval-receipt.json"
        ).read_bytes()
        receipt_ref = "artifact://sha256/" + hashlib.sha256(
            receipt_payload
        ).hexdigest()

        def evidence_resolver(source_ref: str, revision: str) -> bytes | None:
            if (
                source_ref == "state://project/project-idea-benchmark/revision/9"
                and revision == "state:revision:9"
            ):
                return state_payload
            return None

        def artifact_resolver(ref: str) -> bytes | None:
            return receipt_payload if ref == receipt_ref else None

        assertion = compose_assertion_provenance(
            assertion_id="assertion/m9-03/decision-current",
            assertion_text="The current Decision is supported by current State.",
            bearing=True,
            evidence=[
                {
                    "evidence_id": "evidence-current",
                    "authority_kind": "current_state",
                    "source_ref": (
                        "state://project/project-idea-benchmark/revision/9"
                    ),
                    "revision": "state:revision:9",
                    "sha256": state_digest,
                    "valid_at": "2026-08-14T07:05:00+08:00",
                    "retrieval_receipt_ref": receipt_ref,
                }
            ],
            asserted_at="2026-08-14T07:06:00+08:00",
            valid_until="2026-09-17T20:00:00+08:00",
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        claim = {
            "claim_id": "claim/m9-03/decision-current",
            "work_id": "work-active",
            "claim_kind": "decision",
            "statement": "Use the current path",
            "evidence_assertion_ids": [assertion["assertion_id"]],
            "scope_refs": [],
        }
        verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=[assertion],
            current_time="2026-08-14T07:07:00+08:00",
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        source = _external_projection(
            snapshot, self.signer, request_id="request-m9-03-provenance"
        )
        bundle = {
            "schema_version": "context.decision-evidence-provenance-bundle/v1alpha1",
            "project_id": snapshot["project"]["project_id"],
            "state_revision": snapshot["project"]["revision"],
            "state_sha256": source["state_sha256"],
            "source_projection_sha256": source["projection_sha256"],
            "assertion_records": [assertion],
            "claims": [claim],
            "claim_verdicts": [verdict],
            "bindings": [
                {
                    "object_kind": "decision",
                    "object_id": "decision-current",
                    "typed_evidence_id": "evidence-current",
                    "assertion_id": assertion["assertion_id"],
                    "assertion_evidence_id": "evidence-current",
                    "assertion_record_sha256": assertion["record_sha256"],
                    "claim_id": claim["claim_id"],
                    "claim_sha256": verdict["claim_sha256"],
                    "gate_id": verdict["gate_id"],
                    "verdict_sha256": verdict["verdict_sha256"],
                }
            ],
            "bundle_sha256": "",
        }
        bundle["bundle_sha256"] = _bundle_digest(bundle)

        projection = build_decision_evidence_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
            provenance_bundle=bundle,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )

        self.assertEqual(projection["capabilities"]["provenance_mode"], "validated")
        matrix = {
            item["evidence_id"]: item for item in projection["evidence_matrix"]
        }
        self.assertEqual(
            matrix["evidence-current"]["provenance"],
            [
                {
                    "assertion_id": assertion["assertion_id"],
                    "assertion_record_sha256": assertion["record_sha256"],
                    "claim_id": claim["claim_id"],
                    "claim_sha256": verdict["claim_sha256"],
                    "claim_decision": "allow",
                    "verdict_sha256": verdict["verdict_sha256"],
                    "retrieval_receipt_ref": receipt_ref,
                }
            ],
        )

        forged = copy.deepcopy(bundle)
        forged["bindings"][0]["typed_evidence_id"] = "evidence-old"
        forged["bundle_sha256"] = _bundle_digest(forged)
        with self.assertRaises(DecisionEvidenceProjectionError):
            build_decision_evidence_projection(
                source,
                signer=self.signer,
                observed_at="2026-08-17T20:00:00+08:00",
                provenance_bundle=forged,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )

        future_assertion = compose_assertion_provenance(
            assertion_id="assertion/m9-03/decision-current",
            assertion_text="The current Decision is supported by current State.",
            bearing=True,
            evidence=assertion["evidence"],
            asserted_at="2026-08-18T07:06:00+08:00",
            valid_until="2026-09-17T20:00:00+08:00",
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        future_verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=[future_assertion],
            current_time="2026-08-18T07:07:00+08:00",
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
        future = copy.deepcopy(bundle)
        future["assertion_records"] = [future_assertion]
        future["claim_verdicts"] = [future_verdict]
        future["bindings"][0].update(
            {
                "assertion_record_sha256": future_assertion["record_sha256"],
                "verdict_sha256": future_verdict["verdict_sha256"],
            }
        )
        future["bundle_sha256"] = _bundle_digest(future)
        with self.assertRaises(DecisionEvidenceProjectionError):
            build_decision_evidence_projection(
                source,
                signer=self.signer,
                observed_at="2026-08-17T20:00:00+08:00",
                provenance_bundle=future,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )

    def test_proposed_decision_is_not_current_and_evidence_states_are_distinct(self) -> None:
        snapshot = _snapshot()
        for evidence_id, validity, digest in (
            ("evidence-candidate", "candidate", "3"),
            ("evidence-stale", "stale", "4"),
            ("evidence-rejected", "rejected", "5"),
            ("evidence-unreferenced", "verified", "6"),
        ):
            snapshot["evidence"].append(
                {
                    "evidence_id": evidence_id,
                    "kind": "artifact",
                    "artifact_ref": f"artifact://sha256/{digest * 64}",
                    "content_sha256": digest * 64,
                    "validity": validity,
                    "observed_at": "2026-08-14T07:20:00+08:00",
                    "verified_at": (
                        "2026-08-14T07:21:00+08:00"
                        if validity == "verified"
                        else None
                    ),
                }
            )
        snapshot["decisions"].append(
            {
                "decision_id": "decision-proposed",
                "work_id": "work-active",
                "status": "proposed",
                "statement": "Candidate path",
                "decided_at": "2026-08-14T07:30:00+08:00",
                "supersedes_decision_id": None,
                "evidence_ids": [
                    "evidence-candidate",
                    "evidence-stale",
                    "evidence-rejected",
                ],
            }
        )
        validate_typed_state(snapshot)

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-states"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )
        decisions = {
            item["decision_id"]: item for item in projection["decision_timeline"]
        }
        evidence = {
            item["evidence_id"]: item for item in projection["evidence_matrix"]
        }
        self.assertFalse(decisions["decision-proposed"]["is_current"])
        self.assertEqual(evidence["evidence-candidate"]["display_status"], "candidate")
        self.assertEqual(evidence["evidence-stale"]["display_status"], "stale")
        self.assertEqual(evidence["evidence-rejected"]["display_status"], "rejected")
        self.assertEqual(
            evidence["evidence-unreferenced"]["display_status"], "unreferenced"
        )

    def test_all_common_state_evidence_references_are_reversed(self) -> None:
        snapshot = _snapshot()
        snapshot["works"][2]["evidence_ids"] = ["evidence-current"]
        snapshot["ideas"] = [
            {
                "idea_id": "idea-evidence",
                "parent_work_id": "work-active",
                "source_ref": "opaque://idea/evidence",
                "summary": "Retain an evidence link",
                "status": "candidate",
                "return_work_id": "work-active",
                "expiry": None,
                "attempt_budget": None,
                "promotion_target": None,
                "evidence_ids": ["evidence-current"],
            }
        ]
        snapshot["constraints"] = [
            {
                "constraint_id": "constraint-evidence",
                "status": "active",
                "statement": "Keep evidence traceable",
                "scope_work_ids": ["work-active"],
                "expires_at": None,
                "supersedes_constraint_id": None,
                "evidence_ids": ["evidence-current"],
            }
        ]
        snapshot["project"]["active_constraint_ids"] = ["constraint-evidence"]
        snapshot["blockers"] = [
            {
                "blocker_id": "blocker-evidence",
                "status": "open",
                "reason": "Evidence review pending",
                "blocked_work_ids": ["work-active"],
                "evidence_ids": ["evidence-current"],
                "opened_at": "2026-08-14T07:11:00+08:00",
                "resolved_at": None,
                "supersedes_blocker_id": None,
            }
        ]
        snapshot["project"]["open_blocker_ids"] = ["blocker-evidence"]
        snapshot["works"][2]["blocker_ids"] = ["blocker-evidence"]
        snapshot["effects"] = [
            {
                "effect_id": "effect-evidence",
                "effect_key": "effect-key-evidence",
                "work_id": "work-active",
                "claim_id": "claim-active",
                "status": "succeeded",
                "operation": "record-evidence",
                "scope_ref": {
                    "scope_kind": "capability",
                    "scope_ref": "idea/active",
                },
                "expected_project_revision": 9,
                "sequence_no": 1,
                "evidence_ids": ["evidence-current"],
                "result_ref": "artifact://sha256/" + "7" * 64,
                "requested_at": "2026-08-14T07:12:00+08:00",
                "completed_at": "2026-08-14T07:13:00+08:00",
                "attempt_id": None,
            }
        ]
        snapshot["project"]["effect_high_watermark"] = 1
        validate_typed_state(snapshot)

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-references"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )
        current = next(
            item
            for item in projection["evidence_matrix"]
            if item["evidence_id"] == "evidence-current"
        )
        self.assertEqual(
            {item["object_kind"] for item in current["referencing_objects"]},
            {"work", "idea", "decision", "constraint", "blocker", "effect"},
        )

    def test_v4_review_correction_and_promotion_evidence_references_are_reversed(
        self,
    ) -> None:
        snapshot = _snapshot()
        experiment = next(
            item for item in snapshot["works"] if item["work_id"] == "work-target"
        )
        experiment.update(
            {
                "kind": "experiment",
                "return_point_work_id": "goal",
                "exit_criteria": ["criterion-evidence"],
                "attempt_budget": 1,
                "expires_at": "2026-08-14T09:00:00+08:00",
                "promotion_target_work_id": "goal",
                "mainline_authority": False,
            }
        )
        snapshot["claims"].append(
            {
                "claim_id": "claim-experiment",
                "work_id": "work-target",
                "actor_ref": "actor-owner",
                "status": "released",
                "expected_project_revision": 9,
                "claimed_at": "2026-08-14T07:00:00+08:00",
                "lease_expires_at": "2026-08-14T07:30:00+08:00",
                "released_at": "2026-08-14T07:20:00+08:00",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "work-target"}
                ],
            }
        )
        contract_sha256 = experiment_contract_sha256(experiment)
        snapshot["experiment_attempts"] = [
            {
                "attempt_id": "attempt-evidence",
                "work_id": "work-target",
                "claim_id": "claim-experiment",
                "actor_ref": "actor-owner",
                "attempt_no": 1,
                "experiment_contract_sha256": contract_sha256,
                "started_at": "2026-08-14T07:10:00+08:00",
            }
        ]
        snapshot["experiment_promotions"] = [
            {
                "promotion_id": "promotion-evidence",
                "kind": "proposed",
                "proposal_id": "promotion-evidence",
                "work_id": "work-target",
                "target_work_id": "goal",
                "actor_ref": "actor-owner",
                "source_work_revision": 1,
                "target_work_revision": 1,
                "attempt_id": "attempt-evidence",
                "experiment_contract_sha256": contract_sha256,
                "criterion_evidence": {
                    "criterion-evidence": ["evidence-current"]
                },
                "created_at": "2026-08-14T07:20:00+08:00",
            }
        ]
        snapshot = migrate_typed_state_v3_to_v4(
            snapshot, migrated_at="2026-08-14T07:30:00+08:00"
        )
        snapshot = upsert_idea_observation(
            snapshot,
            idea_id="idea-source",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_abcdefghijklmnopqrstuvwxyz",
            summary="Trace the source Idea.",
            scope_ref="work://work-active/source",
            urgency="later",
            review_at=None,
            occurrence_id="occurrence-source",
            observed_at="2026-08-14T07:31:00+08:00",
        )
        snapshot = upsert_idea_observation(
            snapshot,
            idea_id="idea-target",
            parent_work_id="work-active",
            return_work_id="work-active",
            source_ref="rng_bcdefghijklmnopqrstuvwxyza",
            summary="Trace the target Idea.",
            scope_ref="work://work-active/target",
            urgency="later",
            review_at=None,
            occurrence_id="occurrence-target",
            observed_at="2026-08-14T07:32:00+08:00",
        )
        snapshot = add_idea_relationship(
            snapshot,
            relationship_id="relationship-evidence",
            source_idea_id="idea-source",
            target_idea_id="idea-target",
            relationship_kind="depends-on",
            evidence_ids=["evidence-current"],
            created_at="2026-08-14T07:33:00+08:00",
        )
        snapshot = apply_idea_review(
            snapshot,
            idea_id="idea-source",
            review_id="review-evidence",
            reviewer_ref="actor-owner",
            decision="keep",
            urgency="next",
            impact="high",
            review_at=None,
            evidence_ids=["evidence-current"],
            reviewed_at="2026-08-14T07:34:00+08:00",
        )
        snapshot = open_correction_protection(
            snapshot,
            protection_id="protection-evidence",
            idea_id="idea-source",
            affected_work_ids=["work-active"],
            affected_scope_refs=["capability:idea/active"],
            reason="Retain the correction evidence.",
            evidence_ids=["evidence-current"],
            opened_at="2026-08-14T07:35:00+08:00",
        )

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-v4-references"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )
        current = next(
            item
            for item in projection["evidence_matrix"]
            if item["evidence_id"] == "evidence-current"
        )
        references = {
            (item["object_kind"], item["object_id"]): item
            for item in current["referencing_objects"]
        }
        self.assertEqual(
            {
                key
                for key in references
                if key[0]
                in {
                    "idea-relationship",
                    "idea-review",
                    "correction-protection",
                    "experiment-promotion",
                }
            },
            {
                ("idea-relationship", "relationship-evidence"),
                ("idea-review", "review-evidence"),
                ("correction-protection", "protection-evidence"),
                ("experiment-promotion", "promotion-evidence"),
            },
        )
        self.assertEqual(
            references[("experiment-promotion", "promotion-evidence")][
                "reference_path"
            ],
            "criterion_evidence.criterion-evidence",
        )
        self.assertEqual(
            references[("correction-protection", "protection-evidence")][
                "reference_state"
            ],
            "current",
        )

    def test_impossible_chronology_and_cross_work_supersedes_are_rejected(self) -> None:
        mutations = (
            lambda snapshot: next(
                item
                for item in snapshot["evidence"]
                if item["evidence_id"] == "evidence-current"
            ).__setitem__("verified_at", "2026-08-14T06:59:00+08:00"),
            lambda snapshot: next(
                item
                for item in snapshot["decisions"]
                if item["decision_id"] == "decision-current"
            ).__setitem__("decided_at", "2026-08-14T06:00:00+08:00"),
            lambda snapshot: next(
                item
                for item in snapshot["decisions"]
                if item["decision_id"] == "decision-current"
            ).__setitem__("work_id", "work-target"),
        )
        for index, mutate in enumerate(mutations):
            snapshot = _snapshot()
            mutate(snapshot)
            validate_typed_state(snapshot)
            source = _external_projection(
                snapshot,
                self.signer,
                request_id=f"request-m9-03-chronology-{index}",
            )
            with self.subTest(index=index), self.assertRaises(
                DecisionEvidenceProjectionError
            ):
                build_decision_evidence_projection(
                    source,
                    signer=self.signer,
                    observed_at="2026-08-17T20:00:00+08:00",
                )

    def test_decision_timeline_orders_rfc3339_offsets_by_absolute_time(self) -> None:
        snapshot = _snapshot()
        snapshot["project"]["updated_at"] = "2026-08-14T11:00:00+08:00"
        snapshot["claims"][0]["lease_expires_at"] = "2026-08-14T12:00:00+08:00"
        snapshot["decisions"][0]["decided_at"] = "2026-08-14T09:00:00+08:00"
        snapshot["decisions"][1]["decided_at"] = "2026-08-14T02:00:00+00:00"
        validate_typed_state(snapshot)

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-offset-order"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )

        self.assertEqual(
            [item["decision_id"] for item in projection["decision_timeline"]],
            ["decision-old", "decision-current"],
        )

    def test_projection_rejects_evidence_count_above_capacity(self) -> None:
        snapshot = build_idea_snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": f"evidence-{index:05d}",
                "kind": "artifact",
                "artifact_ref": "artifact://sha256/" + f"{index:064x}",
                "content_sha256": f"{index:064x}",
                "validity": "candidate",
                "observed_at": "2026-08-14T07:00:00+08:00",
                "verified_at": None,
            }
            for index in range(10_001)
        ]
        validate_typed_state(snapshot)
        with self.assertRaises(DecisionEvidenceProjectionError):
            build_decision_evidence_projection(
                _external_projection(
                    snapshot, self.signer, request_id="request-m9-03-capacity"
                ),
                signer=self.signer,
                observed_at="2026-08-17T20:00:00+08:00",
            )

    def test_provenance_binding_capacity_is_rejected_before_bundle_digest(self) -> None:
        snapshot = _snapshot()
        source = _external_projection(
            snapshot, self.signer, request_id="request-m9-03-binding-capacity"
        )
        bundle = {
            "schema_version": "context.decision-evidence-provenance-bundle/v1alpha1",
            "project_id": source["project_id"],
            "state_revision": source["state_revision"],
            "state_sha256": source["state_sha256"],
            "source_projection_sha256": source["projection_sha256"],
            "assertion_records": [],
            "claims": [],
            "claim_verdicts": [],
            "bindings": [{}] * 50_001,
            "bundle_sha256": "0" * 64,
        }

        with self.assertRaisesRegex(
            DecisionEvidenceProjectionError,
            "provenance bindings exceed the contract",
        ):
            build_decision_evidence_projection(
                source,
                signer=self.signer,
                observed_at="2026-08-17T20:00:00+08:00",
                provenance_bundle=bundle,
            )

    def test_constraint_supersedes_and_expired_active_health_are_visible(self) -> None:
        snapshot = _snapshot()
        snapshot["constraints"] = [
            {
                "constraint_id": "constraint-old",
                "status": "superseded",
                "statement": "Use the old evidence rule",
                "scope_work_ids": ["work-active"],
                "expires_at": None,
                "supersedes_constraint_id": None,
                "evidence_ids": ["evidence-old"],
            },
            {
                "constraint_id": "constraint-current",
                "status": "active",
                "statement": "Use the current evidence rule",
                "scope_work_ids": ["work-active"],
                "expires_at": "2026-08-16T00:00:00+08:00",
                "supersedes_constraint_id": "constraint-old",
                "evidence_ids": ["evidence-current"],
            },
        ]
        snapshot["project"]["active_constraint_ids"] = ["constraint-current"]
        validate_typed_state(snapshot)

        projection = build_decision_evidence_projection(
            _external_projection(
                snapshot, self.signer, request_id="request-m9-03-constraints"
            ),
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )
        constraints = {
            item["constraint_id"]: item for item in projection["constraint_matrix"]
        }
        self.assertEqual(
            constraints["constraint-old"]["superseded_by_constraint_ids"],
            ["constraint-current"],
        )
        self.assertTrue(constraints["constraint-current"]["is_current"])
        self.assertTrue(
            constraints["constraint-current"]["expired_at_observation"]
        )
        self.assertEqual(
            projection["health"]["expired_active_constraint_ids"],
            ["constraint-current"],
        )

    def test_rebuild_rejects_coordinated_tampering_and_authority_escalation(self) -> None:
        snapshot = _snapshot()
        source = _external_projection(
            snapshot, self.signer, request_id="request-m9-03-tamper"
        )
        projection = build_decision_evidence_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-17T20:00:00+08:00",
        )
        validate_decision_evidence_projection(
            projection,
            source_projection=source,
            signer=self.signer,
        )

        mutations = (
            lambda item: item["decision_timeline"][1].__setitem__(
                "is_current", False
            ),
            lambda item: item["evidence_matrix"][1].__setitem__(
                "display_status", "stale"
            ),
            lambda item: item["health"]["current_decision_ids"].clear(),
        )
        for mutate in mutations:
            forged = copy.deepcopy(projection)
            mutate(forged)
            _resign_projection(forged, self.signer)
            with self.assertRaises(DecisionEvidenceProjectionError):
                validate_decision_evidence_projection(
                    forged,
                    source_projection=source,
                    signer=self.signer,
                )

        forged = copy.deepcopy(projection)
        forged["authority"]["provider_authority"] = False
        _resign_projection(forged, self.signer)
        with self.assertRaises(DecisionEvidenceProjectionError):
            validate_decision_evidence_projection(
                forged,
                source_projection=source,
                signer=self.signer,
            )


if __name__ == "__main__":
    unittest.main()
