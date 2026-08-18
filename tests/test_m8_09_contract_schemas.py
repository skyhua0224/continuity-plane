"""M8-09 unattended dispatcher strict contract-schema tests."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from context_control_plane.unattended_cursor_store import build_campaign_cursor
from context_control_plane.unattended_dispatcher import UnattendedDispatcher
from context_control_plane.unattended_dispatcher_benchmark import (
    benchmark_unattended_dispatcher,
)
from tests.test_m8_09_unattended_dispatcher import (
    _condition_decision,
    _CrashAfterClaimRuntime,
    _fixture,
    _Runtime,
    _VerificationFailureRuntime,
)

SCHEMAS = {
    "context.condition-decision": "condition-decision.schema.json",
    "context.blocking-decision-v2": "blocking-decision-v2.schema.json",
    "context.unattended-campaign-cursor": "unattended-campaign-cursor.schema.json",
    "context.unattended-dispatch-step": "unattended-dispatch-step.schema.json",
    "context.unattended-campaign-receipt": "unattended-campaign-receipt.schema.json",
    "context.unattended-dispatcher-benchmark": "unattended-dispatcher-benchmark.schema.json",
}


class M809ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-09"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        cls.registry = Registry().with_resources(
            (
                schema["$id"],
                Resource.from_contents(schema),
            )
            for schema in cls.schemas.values()
        )
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

    @classmethod
    def samples(cls) -> dict[str, list[dict]]:
        condition_ref = "condition://m8-09/schema"
        state, profile = _fixture(
            obligations=[
                ("work-conditional", "conditional", "autonomous", condition_ref)
            ]
        )
        conditional_runtime = _Runtime(state, profile)
        decision = _condition_decision(
            conditional_runtime,
            condition_ref=condition_ref,
            outcome="met",
            evidence_ids=["evidence-work-conditional"],
        )
        conditional_runtime.condition_decisions = [decision]
        conditional_receipt = UnattendedDispatcher(
            conditional_runtime, actor_ref="actor-executor"
        ).run(max_steps=2)
        closed_cursor = conditional_runtime.cursor_store.read("campaign-run-m8-09")

        initial_cursor = build_campaign_cursor(
            campaign_run_id="campaign-run-schema-initial",
            project_id=state["project"]["project_id"],
            profile_id=profile["profile"]["profile_id"],
            governance_revision=profile["profile"]["revision"],
            start_project_revision=state["project"]["revision"],
        )

        crash_state, crash_profile = _fixture(
            obligations=[("work-crash", "required", "autonomous", None)]
        )
        crash_runtime = _CrashAfterClaimRuntime(crash_state, crash_profile)
        try:
            UnattendedDispatcher(
                crash_runtime, actor_ref="actor-executor"
            ).step()
        except RuntimeError:
            pass
        else:
            raise AssertionError("crash fixture did not stop after claim")
        claimed_cursor = crash_runtime.cursor_store.read("campaign-run-m8-09")

        failed_state, failed_profile = _fixture(
            obligations=[("work-failed", "required", "autonomous", None)]
        )
        failed_runtime = _VerificationFailureRuntime(failed_state, failed_profile)
        blocked_receipt = UnattendedDispatcher(
            failed_runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        not_met_state, not_met_profile = _fixture(
            obligations=[
                ("work-not-met", "conditional", "autonomous", condition_ref)
            ]
        )
        not_met_runtime = _Runtime(not_met_state, not_met_profile)
        not_met_decision = _condition_decision(
            not_met_runtime,
            condition_ref=condition_ref,
            outcome="not-met",
            evidence_ids=["evidence-work-not-met"],
        )
        not_met_runtime.condition_decisions = [not_met_decision]
        not_met_receipt = UnattendedDispatcher(
            not_met_runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        blocking_decision = {
            "schema_version": "context.blocking-decision/v2alpha1",
            "blocking_decision_id": "decision-condition-evidence",
            "project_id": state["project"]["project_id"],
            "project_revision": state["project"]["revision"],
            "blocker_id": "blocker-condition-evidence",
            "blocker_kind": "condition-evidence-unavailable",
            "reason": "Current condition evidence is unavailable.",
            "affected_work_ids": ["work-conditional"],
            "evidence_ids": ["evidence-work-conditional"],
            "affected_scope_refs": [
                {"scope_kind": "capability", "scope_ref": "campaign/work-conditional"}
            ],
            "decision_options": [],
            "default_option_id": None,
            "resume_condition": {
                "kind": "evidence",
                "refs": [condition_ref],
            },
            "resolution_actor": "external_system",
            "safe_reversible_default_available": False,
            "state_write_authority": False,
        }

        assert closed_cursor is not None
        assert claimed_cursor is not None
        return {
            "context.condition-decision": [decision],
            "context.blocking-decision-v2": [blocking_decision],
            "context.unattended-campaign-cursor": [
                initial_cursor,
                claimed_cursor,
                closed_cursor,
            ],
            "context.unattended-dispatch-step": conditional_receipt["steps"],
            "context.unattended-campaign-receipt": [
                conditional_receipt,
                not_met_receipt,
                blocked_receipt,
            ],
            "context.unattended-dispatcher-benchmark": [
                benchmark_unattended_dispatcher(
                    root=cls.root,
                    samples=1,
                    generated_at="2026-08-17T12:00:00+00:00",
                )
            ],
        }

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, samples in self.samples().items():
            validator = Draft202012Validator(
                self.schemas[schema_id],
                format_checker=FormatChecker(),
                registry=self.registry,
            )
            for index, sample in enumerate(samples):
                with self.subTest(schema_id=schema_id, sample=index):
                    validator.validate(sample)

    def test_top_level_fields_are_required_and_unknown_fields_rejected(self) -> None:
        for schema_id, samples in self.samples().items():
            schema = self.schemas[schema_id]
            validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
                registry=self.registry,
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for index, sample in enumerate(samples):
                with self.subTest(
                    schema_id=schema_id, sample=index, mutation="extra"
                ), self.assertRaises(ValidationError):
                    validator.validate({**sample, "unexpected": True})
                for field in sample:
                    missing = copy.deepcopy(sample)
                    del missing[field]
                    with self.subTest(
                        schema_id=schema_id,
                        sample=index,
                        missing=field,
                    ), self.assertRaises(ValidationError):
                        validator.validate(missing)

    def test_condition_evidence_blocker_schema_rejects_other_resume_kinds(self) -> None:
        sample = copy.deepcopy(self.samples()["context.blocking-decision-v2"][0])
        sample["resume_condition"] = {
            "kind": "new_ready_work",
            "refs": ["work-unrelated"],
        }
        validator = Draft202012Validator(
            self.schemas["context.blocking-decision-v2"],
            format_checker=FormatChecker(),
            registry=self.registry,
        )

        with self.assertRaises(ValidationError):
            validator.validate(sample)


if __name__ == "__main__":
    unittest.main()
