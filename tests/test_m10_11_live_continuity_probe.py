"""M10-11 live context-window, continuity, and token-efficiency gates."""

from __future__ import annotations

import copy
import hashlib
import unittest

from context_control_plane.live_continuity_probe import (
    LiveContinuityProbeError,
    compose_live_continuity_segment,
    compose_live_continuity_study,
    evaluate_live_continuity_comparison,
    validate_live_continuity_report,
    validate_live_continuity_segment,
)


class M1011LiveContinuityProbeTests(unittest.TestCase):
    def _match(
        self,
        *,
        project: str = "project-alpha",
        provider: str = "provider-a",
        collaboration_mode: str = "solo",
    ) -> dict:
        return {
            "provider_contract_id": provider,
            "model_id": "model-frontier",
            "model_revision": "2026-08-20",
            "reasoning_effort": "high",
            "context_window_tokens": 1_000_000,
            "auto_compact_token_limit": 900_000,
            "cache_policy": "provider-default",
            "tool_policy_sha256": "5" * 64,
            "sandbox_policy_sha256": "6" * 64,
            "adapter_sha256": "e" * 64,
            "task_class": "history-heavy-implementation",
            "project_profile_sha256": hashlib.sha256(project.encode()).hexdigest(),
            "project_class": "software",
            "repository_topology": (
                "monolith" if project == "project-alpha" else "multi-repository"
            ),
            "collaboration_mode": collaboration_mode,
            "repository_revision": "revision-locked",
            "state_schema_version": "context.typed-state/v6alpha1",
            "starting_state_sha256": "8" * 64,
            "verification_profile_sha256": "9" * 64,
        }

    def _segment(
        self,
        *,
        arm: str,
        index: int,
        project: str = "project-alpha",
        provider: str = "provider-a",
        collaboration_mode: str = "solo",
        input_tokens: int,
        output_tokens: int,
        useful_tokens: int,
        accepted_work_count: int,
        compaction_count: int,
        recovery_bytes: int,
        skill_reads: int = 0,
        duplicate_response: bool = False,
        recovery_narration: bool = False,
        direct_answer: bool = True,
        unwanted_table: bool = False,
        actual_action_matches: bool = True,
    ) -> dict:
        acknowledged = [f"input-{index}"]
        responded = acknowledged if duplicate_response else []
        expected_action = hashlib.sha256(
            f"continue-{project}-{index}".encode()
        ).hexdigest()
        reads = [
            {
                "source_kind": "execution-packet",
                "source_ref": f"artifact://packet/{arm}/{project}/{index}",
                "content_sha256": "a" * 64,
                "bytes_read": recovery_bytes,
            }
        ]
        for read_index in range(skill_reads):
            reads.append(
                {
                    "source_kind": "skill",
                    "source_ref": f"artifact://skill/{arm}/{project}/{index}/{read_index}",
                    "content_sha256": "b" * 64,
                    "bytes_read": 1024,
                }
            )
        compactions = []
        for compact_index in range(compaction_count):
            compactions.append(
                {
                    "compaction_id": f"compact-{arm}-{project}-{index}-{compact_index}",
                    "occurred_at": f"2026-08-20T0{compact_index}:00:00+08:00",
                    "pre": {
                        "project_revision": 20 + compact_index,
                        "active_work_ref": f"work://{project}/active",
                        "claim_ref": f"claim://{project}/active",
                        "expected_first_action_sha256": expected_action,
                        "acknowledged_input_ids": acknowledged,
                    },
                    "post": {
                        "project_revision": 20 + compact_index,
                        "active_work_ref": f"work://{project}/active",
                        "claim_ref": f"claim://{project}/active",
                        "actual_first_action_sha256": (
                            expected_action if actual_action_matches else "f" * 64
                        ),
                        "responded_input_ids": responded,
                    },
                    "recovery_reads": copy.deepcopy(reads),
                }
            )
        accepted_work = [
            {
                "work_ref": f"work://{project}/{index}/{work_index}",
                "completion_receipt_ref": (
                    f"state-receipt://{project}/{arm}/{index}/{work_index}"
                ),
                "completion_receipt_sha256": hashlib.sha256(
                    f"{project}-{arm}-{index}-{work_index}".encode()
                ).hexdigest(),
                "accepted_at": f"2026-08-20T1{work_index}:00:00+08:00",
            }
            for work_index in range(accepted_work_count)
        ]
        responses = [
            {
                "response_id": f"response-{arm}-{project}-{index}",
                "input_id": f"question-{arm}-{project}-{index}",
                "input_kind": "question",
                "input_sha256": "c" * 64,
                "response_sha256": "d" * 64,
                "direct_answer": direct_answer,
                "table_present": unwanted_table,
                "table_requested": False,
                "recovery_narration": recovery_narration,
                "assessment_kind": "deterministic-contract",
                "assessment_ref": f"assessment://{arm}/{project}/{index}",
            }
        ]
        other_tokens = input_tokens - useful_tokens
        return compose_live_continuity_segment(
            segment_id=f"segment-{arm}-{project}-{provider}-{index}",
            arm=arm,
            match=self._match(
                project=project,
                provider=provider,
                collaboration_mode=collaboration_mode,
            ),
            trace={
                "source_kind": "provider-export",
                "source_ref": f"opaque://trace/{arm}/{project}/{index}",
                "source_sha256": hashlib.sha256(
                    f"trace-{arm}-{project}-{index}".encode()
                ).hexdigest(),
                "adapter_id": f"adapter-{provider}",
                "adapter_sha256": "e" * 64,
                "observed_at": "2026-08-20T12:00:00+08:00",
            },
            provider_usage={
                "status": "measured",
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": output_tokens // 2,
                "evidence_ref": f"provider-trace://{arm}/{project}/{index}",
            },
            token_attribution={
                "status": "measured",
                "tokenizer_id": "provider-native",
                "authoritative_current_tokens": useful_tokens // 2,
                "task_relevant_tokens": useful_tokens - useful_tokens // 2,
                "skill_tokens": 0,
                "repeated_history_tokens": other_tokens,
                "recovery_tokens": 0,
                "other_tokens": 0,
                "evidence_ref": f"composition-receipt://{arm}/{project}/{index}",
            },
            accepted_work=accepted_work,
            compactions=compactions,
            responses=responses,
            duration_ms=14_400_000,
        )

    def _qualified_segments(self) -> list[dict]:
        segments = []
        profiles = (
            ("project-alpha", "provider-a", "solo"),
            ("project-beta", "provider-b", "multi-session"),
        )
        for project, provider, collaboration in profiles:
            for index in range(3):
                segments.append(
                    self._segment(
                        arm="baseline",
                        index=index,
                        project=project,
                        provider=provider,
                        collaboration_mode=collaboration,
                        input_tokens=100_000,
                        output_tokens=10_000,
                        useful_tokens=20_000,
                        accepted_work_count=2,
                        compaction_count=2,
                        recovery_bytes=55_000,
                        skill_reads=4,
                        recovery_narration=True,
                    )
                )
                segments.append(
                    self._segment(
                        arm="candidate",
                        index=index,
                        project=project,
                        provider=provider,
                        collaboration_mode=collaboration,
                        input_tokens=45_000,
                        output_tokens=7_000,
                        useful_tokens=30_000,
                        accepted_work_count=4,
                        compaction_count=2,
                        recovery_bytes=8_000,
                    )
                )
        return segments

    def _study(self, segments: list[dict]) -> dict:
        baseline = [item for item in segments if item["arm"] == "baseline"]
        candidate = [item for item in segments if item["arm"] == "candidate"]
        ordered = []
        while len(baseline) >= 2 and len(candidate) >= 2:
            ordered.extend([baseline.pop(0), candidate.pop(0), candidate.pop(0), baseline.pop(0)])
        if baseline or candidate:
            if len(baseline) != 1 or len(candidate) != 1:
                raise AssertionError("test study requires balanced arm counts")
            ordered.extend([baseline.pop(), candidate.pop()])
        planned = []
        for order_index, segment in enumerate(ordered):
            sample_index = segment["segment_id"].rsplit("-", 1)[-1]
            project_digest = segment["match"]["project_profile_sha256"]
            project_name = (
                "project-alpha"
                if project_digest == self._match()["project_profile_sha256"]
                else "project-beta"
            )
            planned.append(
                {
                    "segment_id": segment["segment_id"],
                    "pair_id": f"pair-{project_digest[:8]}-{sample_index}",
                    "order_index": order_index,
                    "arm": segment["arm"],
                    "match": segment["match"],
                    "project_revision": "revision-locked",
                    "state_schema_version": "context.typed-state/v6alpha1",
                    "work_plan_refs": [
                        f"work://{project_name}/{sample_index}/{work_index}"
                        for work_index in range(4)
                    ],
                }
            )
        return compose_live_continuity_study(
            study_id="study-m10-11-live-test",
            created_at="2026-08-20T08:00:00+08:00",
            planned_segments=planned,
            baseline_feature_manifest_sha256="1" * 64,
            candidate_feature_manifest_sha256="2" * 64,
            schedule_kind="abba-balanced",
        )

    def test_qualified_comparison_measures_window_token_and_response_improvement(self) -> None:
        segments = self._qualified_segments()
        report = evaluate_live_continuity_comparison(
            segments,
            study=self._study(segments),
            report_id="report-m10-11-qualified",
            observed_at="2026-08-20T18:00:00+08:00",
        )

        validate_live_continuity_report(report)
        self.assertEqual(report["verdict"], "qualified")
        self.assertGreaterEqual(report["summary"]["input_tokens_per_work_reduction_percent"], 30)
        self.assertGreaterEqual(report["summary"]["output_tokens_per_work_reduction_percent"], 10)
        self.assertGreaterEqual(report["summary"]["accepted_work_per_compaction_improvement_percent"], 30)
        self.assertGreaterEqual(report["summary"]["useful_context_ratio_improvement_percent"], 30)
        self.assertEqual(report["veto_failures"], [])
        self.assertEqual(report["portability"]["project_profiles"], 2)
        self.assertEqual(report["portability"]["provider_contracts"], 2)

    def test_platform_style_post_compaction_reanswer_and_skill_reload_are_vetoes(self) -> None:
        segments = self._qualified_segments()
        broken = next(
            item
            for item in segments
            if item["arm"] == "candidate"
            and item["match"]["project_profile_sha256"]
            == self._match()["project_profile_sha256"]
        )
        replacement = self._segment(
            arm="candidate",
            index=0,
            input_tokens=55_000,
            output_tokens=7_000,
            useful_tokens=30_000,
            accepted_work_count=4,
            compaction_count=2,
            recovery_bytes=8_000,
            skill_reads=5,
            duplicate_response=True,
            recovery_narration=True,
            actual_action_matches=False,
        )
        segments[segments.index(broken)] = replacement

        report = evaluate_live_continuity_comparison(
            segments,
            study=self._study(segments),
            report_id="report-m10-11-broken",
            observed_at="2026-08-20T18:00:00+08:00",
        )

        self.assertEqual(report["verdict"], "failed")
        self.assertIn("acknowledged-input-replay", report["veto_failures"])
        self.assertIn("first-action-mismatch", report["veto_failures"])
        self.assertIn("recovery-narration", report["veto_failures"])
        self.assertIn("unbounded-recovery-read", report["veto_failures"])
        self.assertIn("skill-reread-after-compaction", report["veto_failures"])

    def test_single_project_or_unmeasured_usage_cannot_claim_general_improvement(self) -> None:
        one_project = [
            item
            for item in self._qualified_segments()
            if item["match"]["project_profile_sha256"]
            == self._match()["project_profile_sha256"]
        ]
        report = evaluate_live_continuity_comparison(
            one_project,
            study=self._study(one_project),
            report_id="report-m10-11-one-project",
            observed_at="2026-08-20T18:00:00+08:00",
        )
        self.assertEqual(report["verdict"], "insufficient-evidence")
        self.assertIn("project-profile-coverage", report["evidence_gaps"])
        self.assertIn("provider-contract-coverage", report["evidence_gaps"])

        unavailable = copy.deepcopy(one_project)
        unavailable[0]["provider_usage"] = {
            "status": "unavailable",
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "evidence_ref": None,
        }
        unavailable[0]["segment_sha256"] = "0" * 64
        with self.assertRaises(LiveContinuityProbeError):
            validate_live_continuity_segment(unavailable[0])

    def test_study_denominator_rejects_missing_extra_or_posthoc_work(self) -> None:
        segments = self._qualified_segments()
        study = self._study(segments)
        faults = []
        faults.append(segments[:-1])
        extra = copy.deepcopy(segments)
        changed = copy.deepcopy(extra[0])
        changed["segment_id"] = "segment-unplanned"
        changed["segment_sha256"] = "0" * 64
        faults.append(extra + [changed])
        posthoc = copy.deepcopy(segments)
        posthoc[0]["accepted_work"][0]["work_ref"] = "work://posthoc/unplanned"
        posthoc[0]["segment_sha256"] = "0" * 64
        faults.append(posthoc)
        for fault in faults:
            with self.subTest(), self.assertRaises(LiveContinuityProbeError):
                evaluate_live_continuity_comparison(
                    fault,
                    study=study,
                    report_id="report-m10-11-denominator-fault",
                    observed_at="2026-08-20T18:00:00+08:00",
                )

    def test_segment_rejects_raw_text_and_self_assessed_response_quality(self) -> None:
        segment = self._qualified_segments()[0]
        segment["responses"][0]["response_text"] = "raw private response"
        segment["segment_sha256"] = "0" * 64
        with self.assertRaises(LiveContinuityProbeError):
            validate_live_continuity_segment(segment)

        segment = self._qualified_segments()[0]
        segment["responses"][0]["assessment_kind"] = "provider-under-test"
        segment["segment_sha256"] = "0" * 64
        with self.assertRaises(LiveContinuityProbeError):
            validate_live_continuity_segment(segment)


if __name__ == "__main__":
    unittest.main()
