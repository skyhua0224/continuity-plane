"""M7-06 provider-neutral ReferenceWatcher and assertion supersedes gates."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.assertion_provenance import compose_assertion_provenance
from context_control_plane.reference_watcher import (
    ReferenceWatcherError,
    assertion_is_completion_eligible,
    canonical_reference_watch_decision_bytes,
    canonical_reference_watch_observation_bytes,
    decide_reference_watch,
    observe_reference,
    validate_reference_watch_decision,
    validate_reference_watch_observation,
)
from context_control_plane.retrieval_routing import (
    compose_retrieval_receipt,
    plan_retrieval,
)


class M706ReferenceWatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schemas = {
            "observation": json.loads(
                (
                    cls.root / "schemas/m7-06/reference-watch-observation.schema.json"
                ).read_text(encoding="utf-8")
            ),
            "decision": json.loads(
                (
                    cls.root / "schemas/m7-06/reference-watch-decision.schema.json"
                ).read_text(encoding="utf-8")
            ),
        }

    def setUp(self) -> None:
        self.baseline = b"official specification revision one\n"
        self.changed = b"official specification revision two\n"
        self.watch = {
            "watch_id": "watch/reference/specification",
            "source_id": "reference/specification",
            "source_ref": "https://standards.example.invalid/specification",
            "authority_kind": "industry_standard",
            "watch_revision": 7,
            "baseline_revision": "revision-1",
            "baseline_sha256": hashlib.sha256(self.baseline).hexdigest(),
            "valid_until": "2026-09-01T00:00:00Z",
            "assertion_ids": ["assertion/reference/revision-1"],
        }
        self.trusted_time = {
            "kind": "fixture-clock",
            "trusted_at": "2026-08-16T08:00:00Z",
            "evidence": b"fixture-clock:2026-08-16T08:00:00Z",
        }
        self.artifacts: dict[str, bytes] = {}
        self.evidence_payloads: dict[tuple[str, str], bytes] = {}
        self.replacements: dict[tuple[str, str], dict] = {}

    def test_live_observation_requires_registry_bound_verifier_resolver(self) -> None:
        self.assertIn(
            "trusted_time_verifier_resolver",
            inspect.signature(observe_reference).parameters,
        )

    @staticmethod
    def trusted_time_verifier(kind: str, trusted_at: str, evidence: bytes) -> bool:
        return evidence == f"{kind}:{trusted_at}".encode()

    def artifact_resolver(self, ref: str):
        return self.artifacts.get(ref)

    def evidence_resolver(self, source_ref: str, revision: str):
        return self.evidence_payloads.get((source_ref, revision))

    def replacement_resolver(self, assertion_id: str, record_sha256: str):
        return self.replacements.get((assertion_id, record_sha256))

    def observation_kwargs(self) -> dict:
        return {
            "expected_watch": self.watch,
            "artifact_resolver": self.artifact_resolver,
            "trusted_time_verifier": self.trusted_time_verifier,
        }

    def decision_kwargs(self, observation: dict) -> dict:
        return {
            "expected_observation": observation,
            **self.observation_kwargs(),
            "replacement_assertion_resolver": self.replacement_resolver,
            "evidence_resolver": self.evidence_resolver,
        }

    def observe(self, **overrides):
        values = {
            "watch": self.watch,
            "mode": "fixture",
            "availability_status": "available",
            "observed_revision": "revision-1",
            "observed_content": self.baseline,
            "unavailability_evidence": None,
            "trusted_time": self.trusted_time,
            "trusted_time_verifier": self.trusted_time_verifier,
        }
        values.update(overrides)
        observation = observe_reference(**values)
        if values["availability_status"] == "available":
            evidence = bytes(values["observed_content"])
        else:
            evidence = bytes(values["unavailability_evidence"])
        self.artifacts[observation["current_evidence_ref"]] = evidence
        self.artifacts[observation["trusted_time_evidence_ref"]] = bytes(
            values["trusted_time"]["evidence"]
        )
        return observation

    def decide(self, observation: dict, **kwargs):
        return decide_reference_watch(
            observation,
            expected_watch=self.watch,
            artifact_resolver=self.artifact_resolver,
            trusted_time_verifier=self.trusted_time_verifier,
            evidence_resolver=self.evidence_resolver,
            **kwargs,
        )

    def eligible(self, decision: dict, assertion_id: str, observation: dict) -> bool:
        return assertion_is_completion_eligible(
            decision,
            assertion_id,
            **self.decision_kwargs(observation),
        )

    def retrieval_receipt(
        self,
        *,
        assertion_id: str,
        source_ref: str,
        revision: str,
        content: bytes,
        asserted_at: str,
    ) -> str:
        plan = plan_retrieval(
            {
                "question_id": f"question/{assertion_id}",
                "kind": "official_reference",
                "query": "current official reference",
                "repositories": ["external-reference"],
                "freshness_required": True,
            },
            available_tools={"rtfm"},
            max_queries=1,
            max_scanned_bytes=max(len(content), 1),
            max_returned_bytes=max(len(content), 1),
            max_index_age_seconds=3600,
        )
        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=[
                {
                    "evidence_id": f"retrieval/{assertion_id}",
                    "source_kind": "official_reference",
                    "source_ref": source_ref,
                    "revision": revision,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "range": {"offset_bytes": 0, "length_bytes": len(content)},
                    "retrieved_at": asserted_at,
                    "valid_at": asserted_at,
                }
            ],
            step_results=[
                {
                    "tool": "rtfm",
                    "queries": 1,
                    "scanned_bytes": len(content),
                    "returned_bytes": len(content),
                    "index_revision": revision,
                    "index_sha256": hashlib.sha256(content).hexdigest(),
                    "index_age_seconds": 0,
                }
            ],
            executed_at=asserted_at,
            cache_status="miss",
            prior_receipt_ref=None,
        )
        payload = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        ref = f"artifact://sha256/{hashlib.sha256(payload).hexdigest()}"
        self.artifacts[ref] = payload
        self.evidence_payloads[(source_ref, revision)] = content
        return ref

    def assertion(
        self,
        *,
        assertion_id: str,
        revision: str,
        content: bytes,
        asserted_at: str,
        source_ref: str | None = None,
    ):
        resolved_source_ref = source_ref or self.watch["source_ref"]
        record = compose_assertion_provenance(
            assertion_id=assertion_id,
            assertion_text="The current official specification supports the bearing conclusion.",
            bearing=True,
            evidence=[
                {
                    "evidence_id": f"evidence/{assertion_id}",
                    "authority_kind": "industry_standard",
                    "source_ref": resolved_source_ref,
                    "revision": revision,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "valid_at": asserted_at,
                    "retrieval_receipt_ref": self.retrieval_receipt(
                        assertion_id=assertion_id,
                        source_ref=resolved_source_ref,
                        revision=revision,
                        content=content,
                        asserted_at=asserted_at,
                    ),
                }
            ],
            asserted_at=asserted_at,
            valid_until="2026-10-01T00:00:00Z",
        )
        self.replacements[(record["assertion_id"], record["record_sha256"])] = record
        return record

    @staticmethod
    def resign(record: dict, digest_field: str) -> None:
        unsigned = {key: value for key, value in record.items() if key != digest_field}
        payload = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        record[digest_field] = hashlib.sha256(payload).hexdigest()

    def test_observation_cannot_be_resealed_against_a_forged_watch_baseline(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        forged = copy.deepcopy(observation)
        forged["baseline_revision"] = forged["observed_revision"]
        forged["baseline_sha256"] = forged["observed_sha256"]
        forged["watch_outcome"] = "unchanged"
        self.resign(forged, "observation_sha256")

        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_observation(forged, **self.observation_kwargs())

    def test_decision_cannot_be_resealed_without_its_expected_observation(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        forged = self.decide(observation)
        forged.update(
            {
                "watch_outcome": "unchanged",
                "assertion_status": "current",
                "decision": "allow-current",
                "reason_code": "source-unchanged",
                "stale_assertion_ids": [],
                "quarantined_assertion_ids": [],
                "superseded_assertion_ids": [],
                "completion_eligible_assertion_ids": list(
                    forged["affected_assertion_ids"]
                ),
            }
        )
        self.resign(forged, "decision_sha256")

        with self.assertRaises(ReferenceWatcherError):
            assertion_is_completion_eligible(
                forged,
                "assertion/reference/revision-1",
                **self.decision_kwargs(observation),
            )

    def test_unsafe_decision_cannot_claim_allow_current_or_supersedes(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        forged = self.decide(observation)
        forged["decision"] = "allow-current"
        forged["superseded_assertion_ids"] = list(
            forged["affected_assertion_ids"]
        )
        self.resign(forged, "decision_sha256")

        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_decision(
                forged, **self.decision_kwargs(observation)
            )

    def test_decision_schema_rejects_an_inconsistent_state_matrix(self) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        forged = self.decide(observation)
        forged["decision"] = "allow-current"
        forged["superseded_assertion_ids"] = list(
            forged["affected_assertion_ids"]
        )
        self.resign(forged, "decision_sha256")

        with self.assertRaises(ValidationError):
            Draft202012Validator(self.schemas["decision"]).validate(forged)

    def test_current_reverification_cannot_supersede_the_same_assertion_id(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        replacement = self.assertion(
            assertion_id="assertion/reference/revision-1",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
        )
        forged = {
            "schema_version": "context.reference-watch-decision/v1alpha1",
            "decision_id": f"decision/{observation['observation_id']}",
            "observation_id": observation["observation_id"],
            "observation_sha256": observation["observation_sha256"],
            "watch_id": observation["watch_id"],
            "source_id": observation["source_id"],
            "source_identity_sha256": observation["source_identity_sha256"],
            "watch_outcome": "changed",
            "assertion_status": "current",
            "decision": "allow-current",
            "reason_code": "current-reverification",
            "affected_assertion_ids": ["assertion/reference/revision-1"],
            "stale_assertion_ids": ["assertion/reference/revision-1"],
            "quarantined_assertion_ids": [],
            "superseded_assertion_ids": ["assertion/reference/revision-1"],
            "completion_eligible_assertion_ids": [
                "assertion/reference/revision-1"
            ],
            "replacement_assertion_id": replacement["assertion_id"],
            "replacement_record_sha256": replacement["record_sha256"],
            "evaluated_at": replacement["asserted_at"],
            "state_write_authority": False,
            "completion_authority": False,
            "provider_native_authority": False,
            "decision_sha256": "",
        }
        self.resign(forged, "decision_sha256")

        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_decision(
                forged,
                expected_replacement_assertion=replacement,
                **self.decision_kwargs(observation),
            )

    def test_replacement_requires_resolved_evidence_and_retrieval_receipt(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        replacement = self.assertion(
            assertion_id="assertion/reference/revision-2",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
        )

        with self.assertRaises(ReferenceWatcherError):
            decide_reference_watch(
                observation,
                replacement_assertion=replacement,
                supersedes_assertion_ids=["assertion/reference/revision-1"],
                **self.observation_kwargs(),
            )

    def test_live_trusted_time_requires_a_verified_attestation(self) -> None:
        trusted_time = {
            "kind": "provider-signed",
            "trusted_at": "2026-08-16T08:00:00Z",
            "evidence": b"not-a-signature-or-attestation",
        }

        with self.assertRaises(ReferenceWatcherError):
            self.observe(mode="live", trusted_time=trusted_time)

    def test_trusted_resolvers_can_supply_watch_and_observation_anchors(self) -> None:
        observation = self.observe()
        decision = self.decide(observation)
        watch_resolver = lambda watch_id, revision: (
            self.watch
            if (watch_id, revision)
            == (self.watch["watch_id"], self.watch["watch_revision"])
            else None
        )
        observation_resolver = lambda observation_id, digest: (
            observation
            if (observation_id, digest)
            == (observation["observation_id"], observation["observation_sha256"])
            else None
        )

        validate_reference_watch_observation(
            observation,
            watch_resolver=watch_resolver,
            artifact_resolver=self.artifact_resolver,
            trusted_time_verifier=self.trusted_time_verifier,
        )
        self.assertTrue(
            assertion_is_completion_eligible(
                decision,
                "assertion/reference/revision-1",
                observation_resolver=observation_resolver,
                watch_resolver=watch_resolver,
                artifact_resolver=self.artifact_resolver,
                trusted_time_verifier=self.trusted_time_verifier,
            )
        )
        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_observation(
                observation,
                artifact_resolver=self.artifact_resolver,
                trusted_time_verifier=self.trusted_time_verifier,
            )
        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_decision(
                decision,
                expected_watch=self.watch,
                artifact_resolver=self.artifact_resolver,
                trusted_time_verifier=self.trusted_time_verifier,
            )

    def test_unchanged_current_reference_keeps_bound_assertion_eligible(self) -> None:
        observation = self.observe()
        decision = self.decide(observation)

        self.assertEqual(observation["watch_outcome"], "unchanged")
        self.assertEqual(observation["freshness_status"], "current")
        self.assertEqual(decision["assertion_status"], "current")
        self.assertEqual(decision["decision"], "allow-current")
        self.assertTrue(
            self.eligible(decision, "assertion/reference/revision-1", observation)
        )
        self.assertFalse(decision["state_write_authority"])
        self.assertFalse(decision["completion_authority"])
        self.assertFalse(decision["provider_native_authority"])
        validate_reference_watch_observation(observation, **self.observation_kwargs())
        validate_reference_watch_decision(
            decision, **self.decision_kwargs(observation)
        )
        Draft202012Validator(self.schemas["observation"]).validate(observation)
        Draft202012Validator(self.schemas["decision"]).validate(decision)

    def test_revision_or_hash_change_marks_old_assertion_stale(self) -> None:
        cases = (
            {"observed_revision": "revision-2", "observed_content": self.baseline},
            {"observed_revision": "revision-1", "observed_content": self.changed},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                observation = self.observe(**overrides)
                decision = self.decide(observation)

                self.assertEqual(observation["watch_outcome"], "changed")
                self.assertEqual(decision["assertion_status"], "stale")
                self.assertEqual(decision["decision"], "reverify-required")
                self.assertEqual(
                    decision["stale_assertion_ids"], ["assertion/reference/revision-1"]
                )
                self.assertEqual(decision["completion_eligible_assertion_ids"], [])
                self.assertFalse(
                    self.eligible(
                        decision, "assertion/reference/revision-1", observation
                    )
                )

    def test_unavailable_or_expired_reference_quarantines_old_assertion(self) -> None:
        unavailable = self.observe(
            availability_status="unavailable",
            observed_revision=None,
            observed_content=None,
            unavailability_evidence=b"offline fixture: upstream unavailable",
        )
        expired_time = copy.deepcopy(self.trusted_time)
        expired_time["trusted_at"] = "2026-09-02T00:00:00Z"
        expired_time["evidence"] = b"fixture-clock:2026-09-02T00:00:00Z"
        expired = self.observe(trusted_time=expired_time)

        for observation, expected in (
            (unavailable, "unavailable"),
            (expired, "expired"),
        ):
            with self.subTest(expected=expected):
                decision = self.decide(observation)
                self.assertEqual(observation["watch_outcome"], expected)
                self.assertEqual(decision["assertion_status"], "quarantined")
                self.assertEqual(
                    decision["quarantined_assertion_ids"],
                    ["assertion/reference/revision-1"],
                )
                self.assertEqual(decision["completion_eligible_assertion_ids"], [])

    def test_only_current_reverification_releases_a_changed_reference(self) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        replacement = self.assertion(
            assertion_id="assertion/reference/revision-2",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
        )
        decision = self.decide(
            observation,
            replacement_assertion=replacement,
            supersedes_assertion_ids=["assertion/reference/revision-1"],
        )

        self.assertEqual(decision["reason_code"], "current-reverification")
        self.assertEqual(decision["assertion_status"], "current")
        self.assertEqual(
            decision["superseded_assertion_ids"], ["assertion/reference/revision-1"]
        )
        self.assertEqual(
            decision["replacement_assertion_id"], "assertion/reference/revision-2"
        )
        self.assertFalse(
            self.eligible(decision, "assertion/reference/revision-1", observation)
        )
        self.assertTrue(
            self.eligible(decision, "assertion/reference/revision-2", observation)
        )

    def test_reverification_must_match_current_identity_revision_hash_and_time(
        self,
    ) -> None:
        observation = self.observe(
            observed_revision="revision-2", observed_content=self.changed
        )
        invalid_records = []
        wrong_revision = self.assertion(
            assertion_id="assertion/reference/revision-3",
            revision="revision-3",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
        )
        invalid_records.append(wrong_revision)
        wrong_source = self.assertion(
            assertion_id="assertion/reference/wrong-source",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
            source_ref="https://other.example.invalid/spec",
        )
        invalid_records.append(wrong_source)
        before_watch = self.assertion(
            assertion_id="assertion/reference/revision-before-watch",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T07:59:00Z",
        )
        invalid_records.append(before_watch)

        for replacement in invalid_records:
            with (
                self.subTest(assertion_id=replacement["assertion_id"]),
                self.assertRaises(ReferenceWatcherError),
            ):
                self.decide(
                    observation,
                    replacement_assertion=replacement,
                    supersedes_assertion_ids=["assertion/reference/revision-1"],
                )

    def test_unavailable_or_expired_observation_cannot_be_released(self) -> None:
        replacement = self.assertion(
            assertion_id="assertion/reference/revision-2",
            revision="revision-2",
            content=self.changed,
            asserted_at="2026-08-16T08:01:00Z",
        )
        unavailable = self.observe(
            availability_status="unavailable",
            observed_revision=None,
            observed_content=None,
            unavailability_evidence=b"offline fixture: upstream unavailable",
        )
        with self.assertRaises(ReferenceWatcherError):
            self.decide(
                unavailable,
                replacement_assertion=replacement,
                supersedes_assertion_ids=["assertion/reference/revision-1"],
            )

    def test_source_identity_and_trusted_time_are_hash_bound(self) -> None:
        observation = self.observe()
        mutations = (
            ("source_ref", "https://other.example.invalid/spec"),
            ("trusted_at", "2026-08-17T08:00:00Z"),
            ("current_evidence_sha256", "0" * 64),
            ("state_write_authority", True),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(observation)
                changed[field] = value
                with self.assertRaises(ReferenceWatcherError):
                    validate_reference_watch_observation(
                        changed, **self.observation_kwargs()
                    )

    def test_live_mode_rejects_fixture_clock(self) -> None:
        with self.assertRaisesRegex(ReferenceWatcherError, "fixture clock"):
            self.observe(mode="live")

    def test_replay_is_byte_deterministic_and_unknown_fields_fail_closed(self) -> None:
        first = self.observe()
        second = self.observe()
        first_decision = self.decide(first)
        second_decision = self.decide(second)

        self.assertEqual(
            canonical_reference_watch_observation_bytes(
                first, **self.observation_kwargs()
            ),
            canonical_reference_watch_observation_bytes(
                second, **self.observation_kwargs()
            ),
        )
        self.assertEqual(
            canonical_reference_watch_decision_bytes(
                first_decision, **self.decision_kwargs(first)
            ),
            canonical_reference_watch_decision_bytes(
                second_decision, **self.decision_kwargs(second)
            ),
        )
        invalid = copy.deepcopy(first_decision)
        invalid["unknown"] = True
        with self.assertRaises(ReferenceWatcherError):
            validate_reference_watch_decision(
                invalid, **self.decision_kwargs(first)
            )


if __name__ == "__main__":
    unittest.main()
