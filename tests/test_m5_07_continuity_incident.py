import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.context_trace import LocalContextTraceEmitter
from context_control_plane.continuity_incident import (
    ContinuityIncidentError,
    admit_continuity_incident,
    build_continuation_obligation,
    build_continuity_record,
    detect_continuity_incident,
    validate_continuation_obligation,
    validate_continuity_incident,
)
from context_control_plane.input_progression import canonical_progression_decision_bytes


class ContinuityIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.termination = {
            "run_ref": "run/dogfood/2026-08-16/m7-02-stop",
            "project_id": "context-control-plane",
            "campaign_id": "M7",
            "completed_work_id": "M7-02",
            "active_work_id": "M7-03",
            "project_revision": 57,
            "termination_kind": "assistant-final",
            "ready_work_ids": ["M7-03"],
            "typed_blocker_id": None,
            "terminated_at": "2026-08-16T16:00:00+08:00",
            "termination_time_status": "measured",
        }
        self.decision = {
            "schema_version": "context.continuation-dispatch-decision/v1alpha1",
            "request_id": "dispatch/m7-03/revision-57",
            "request_sha256": "2" * 64,
            "project_id": "context-control-plane",
            "project_revision": 57,
            "governance_revision": 57,
            "observed_at": "2026-08-16T15:59:59+08:00",
            "intent_kind": "dispatch_tick",
            "input_ref": None,
            "input_sha256": None,
            "input_kind": None,
            "classifier_provenance_ref": None,
            "action": "continue-active",
            "reason_code": "active-leaf-sticky",
            "active_work_id_before": "M7-03",
            "active_work_id_after": "M7-03",
            "active_work_revision_before": 1,
            "active_work_revision_after": 1,
            "selected_work_id": None,
            "selected_work_revision": None,
            "blocker_id": None,
            "blocker_kind": None,
            "reason": None,
            "affected_work_ids": [],
            "evidence_ids": [],
            "governance_evidence_refs": [],
            "decision_options": [],
            "default_behavior": None,
            "affected_scope_refs": [],
            "resume_condition": None,
            "pending_route_decision_sha256": None,
            "state_write_authority": False,
        }

    def emitter(self):
        return LocalContextTraceEmitter(
            binding={
                "project_id": "context-control-plane",
                "state_revision": 57,
                "active_work_id": "M7-03",
                "trace_id": "7" * 32,
                "span_id": "3" * 16,
                "run_id": "run/dogfood/m7-02-stop",
                "operation_id": "operation/continuity/detect",
                "correlation_id": "campaign/M7",
            },
            source={
                "kind": "user-correction",
                "provider": "codex",
                "adapter": "continuity-detector/v1",
                "source_ref": "observation://dogfood/m7-02-stop",
            },
        )

    def detect(self):
        return detect_continuity_incident(
            termination=self.termination,
            continuation_basis=self.decision,
            trace_emitter=self.emitter(),
            detected_at="2026-08-16T16:08:00+08:00",
            detection_source="user-correction",
            evidence_refs=[
                "STATUS.md@revision-57",
                "git:ac962ca",
            ],
        )

    def test_ready_work_final_is_a_premature_stop(self) -> None:
        incident = self.detect()

        self.assertEqual(incident["schema_version"], "context.continuity-incident/v1alpha1")
        self.assertEqual(incident["incident_kind"], "premature-stop")
        self.assertEqual(incident["expected_action"], "continue")
        self.assertEqual(incident["actual_action"], "stop")
        self.assertEqual(incident["active_work_id"], "M7-03")
        self.assertEqual(
            incident["continuation_basis_sha256"],
            hashlib.sha256(canonical_progression_decision_bytes(self.decision)).hexdigest(),
        )
        self.assertEqual(incident["continuation_basis_kind"], "m3-decision")
        self.assertEqual(incident["remaining_ready_work_ids"], ["M7-03"])
        self.assertEqual(incident["veto_failures"], ["premature-stop"])
        self.assertEqual(incident["measurement_scope"], "live-observation")
        self.assertEqual(incident["status"], "regressed")
        self.assertEqual(incident["trace_event_name"], "context.dogfood.continuity-incident")
        self.assertFalse(incident["state_write_authority"])
        self.assertFalse(incident["completion_authority"])
        self.assertFalse(incident["provider_native_authority"])
        self.assertFalse(incident["trace_authority"])
        validate_continuity_incident(incident)

    def test_typed_blocker_does_not_create_a_premature_stop(self) -> None:
        self.termination["typed_blocker_id"] = "blocker/external-evidence"

        self.assertIsNone(self.detect())

    def test_no_ready_work_does_not_create_a_premature_stop(self) -> None:
        self.termination["active_work_id"] = None
        self.termination["ready_work_ids"] = []
        self.decision.update(
            action="stop-complete",
            reason_code="campaign-complete",
            active_work_id_before=None,
            active_work_id_after=None,
            active_work_revision_before=None,
            active_work_revision_after=None,
            reason="all required work is complete",
            governance_evidence_refs=["state/project/revision-57"],
            resume_condition={"kind": "new_ready_work", "refs": ["project/context-control-plane"]},
        )

        self.assertIsNone(self.detect())

    def test_revision_or_selected_work_drift_fails_closed(self) -> None:
        for field, value in (
            ("project_revision", 58),
            ("active_work_id_after", "M7-04"),
        ):
            with self.subTest(field=field):
                decision = copy.deepcopy(self.decision)
                decision[field] = value
                with self.assertRaises(ContinuityIncidentError):
                    detect_continuity_incident(
                        termination=self.termination,
                        continuation_basis=decision,
                        trace_emitter=self.emitter(),
                        detected_at="2026-08-16T16:08:00+08:00",
                        detection_source="user-correction",
                        evidence_refs=["STATUS.md@revision-57"],
                    )

    def test_forged_continuation_decision_fails_closed(self) -> None:
        self.decision["state_write_authority"] = True

        with self.assertRaises(ContinuityIncidentError):
            self.detect()

    def test_governance_obligation_is_a_distinct_reconstructed_basis(self) -> None:
        obligation = build_continuation_obligation(
            obligation_id="obligation/M7-03/revision-57",
            project_id="context-control-plane",
            campaign_id="M7",
            active_work_id="M7-03",
            project_revision=57,
            governance_revision=57,
            expected_action="continue",
            ready_work_ids=["M7-03"],
            typed_blocker_id=None,
            observed_at="2026-08-16T16:08:00+08:00",
            evidence_refs=["STATUS.md@revision-57", "MASTER.md@revision-57"],
        )
        validate_continuation_obligation(obligation)

        incident = detect_continuity_incident(
            termination=self.termination,
            continuation_basis=obligation,
            trace_emitter=self.emitter(),
            detected_at="2026-08-16T16:08:00+08:00",
            detection_source="user-correction",
            evidence_refs=["STATUS.md@revision-57", "MASTER.md@revision-57"],
        )

        self.assertEqual(incident["continuation_basis_kind"], "governance-obligation")
        self.assertEqual(incident["continuation_basis_ref"], obligation["obligation_id"])
        self.assertEqual(incident["continuation_basis_sha256"], obligation["obligation_sha256"])

    def test_obligation_campaign_time_and_blocker_type_are_bound(self) -> None:
        obligation = build_continuation_obligation(
            obligation_id="obligation/M7-03/revision-57",
            project_id="context-control-plane",
            campaign_id="M8",
            active_work_id="M7-03",
            project_revision=57,
            governance_revision=57,
            expected_action="continue",
            ready_work_ids=["M7-03"],
            typed_blocker_id=None,
            observed_at="2030-08-16T16:08:00+08:00",
            evidence_refs=["STATUS.md@revision-57"],
        )
        with self.assertRaises(ContinuityIncidentError):
            detect_continuity_incident(
                termination=self.termination,
                continuation_basis=obligation,
                trace_emitter=self.emitter(),
                detected_at="2026-08-16T16:08:00+08:00",
                detection_source="user-correction",
                evidence_refs=["STATUS.md@revision-57"],
            )

        blocked = copy.deepcopy(self.termination)
        blocked["typed_blocker_id"] = True
        with self.assertRaises(ContinuityIncidentError):
            detect_continuity_incident(
                termination=blocked,
                continuation_basis=self.decision,
                trace_emitter=self.emitter(),
                detected_at="2026-08-16T16:08:00+08:00",
                detection_source="user-correction",
                evidence_refs=["STATUS.md@revision-57"],
            )

    def test_continuity_ids_and_refs_match_wire_length_limits(self) -> None:
        with self.assertRaises(ContinuityIncidentError):
            build_continuation_obligation(
                obligation_id="obligation/M7-03/revision-57",
                project_id="context-control-plane",
                campaign_id="M7",
                active_work_id="M7-03",
                project_revision=57,
                governance_revision=57,
                expected_action="continue",
                ready_work_ids=["x" * 513],
                typed_blocker_id=None,
                observed_at="2026-08-16T16:08:00+08:00",
                evidence_refs=["STATUS.md@revision-57"],
            )

    def test_digest_and_extra_fields_fail_closed(self) -> None:
        incident = self.detect()
        mutations = (
            lambda value: value.update(incident_sha256="0" * 64),
            lambda value: value.update(unregistered=True),
            lambda value: value.update(state_write_authority=True),
            lambda value: value.update(provider_native_authority=True),
            lambda value: value.update(veto_failures=[]),
        )

        for mutate in mutations:
            invalid = copy.deepcopy(incident)
            mutate(invalid)
            with self.assertRaises(ContinuityIncidentError):
                validate_continuity_incident(invalid)

    def test_replay_is_deterministic_and_duplicate_admission_is_idempotent(self) -> None:
        first = self.detect()
        replay = self.detect()

        self.assertEqual(first, replay)
        self.assertEqual(admit_continuity_incident([first], replay), first)

        conflict = copy.deepcopy(first)
        conflict["incident_sha256"] = "0" * 64
        with self.assertRaises(ContinuityIncidentError):
            admit_continuity_incident([first], conflict)

    def test_existing_m5_event_schema_does_not_accept_incident_as_event_kind(self) -> None:
        schema = json.loads(
            (self.root / "schemas/m5-07/dogfood-event.schema.json").read_text(
                encoding="utf-8"
            )
        )
        invalid = {
            "schema_version": "context.dogfood-event/v1alpha1",
            "event_kind": "continuity-incident",
        }

        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

    def test_schema_registry_and_live_receipt_match_runtime_contract(self) -> None:
        schema_path = self.root / "schemas/m5-07/continuity-incident.schema.json"
        obligation_schema_path = (
            self.root / "schemas/m5-07/continuation-obligation.schema.json"
        )
        input_path = (
            self.root / "experiments/dogfood/live-premature-stop-2026-08-16-input.json"
        )
        receipt_path = self.root / "experiments/dogfood/live-premature-stop-2026-08-16.json"
        trace_path = (
            self.root / "experiments/dogfood/live-premature-stop-2026-08-16-trace.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        obligation_schema = json.loads(obligation_schema_path.read_text(encoding="utf-8"))
        input_document = json.loads(input_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        rebuilt_receipt, rebuilt_trace = build_continuity_record(input_document)
        self.assertEqual(rebuilt_receipt, receipt)
        self.assertEqual(rebuilt_trace, trace)
        Draft202012Validator(schema).validate(receipt)
        validate_continuity_incident(receipt)
        Draft202012Validator(obligation_schema).validate(
            input_document["continuation_obligation"]
        )
        validate_continuation_obligation(input_document["continuation_obligation"])

        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.continuity-incident"
        )
        self.assertEqual(
            entry["artifact_path"],
            "schemas/m5-07/continuity-incident.schema.json",
        )
        self.assertEqual(entry["current_wire_version"], receipt["schema_version"])
        obligation_entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.continuation-obligation"
        )
        self.assertEqual(
            obligation_entry["current_wire_version"],
            input_document["continuation_obligation"]["schema_version"],
        )


if __name__ == "__main__":
    unittest.main()
