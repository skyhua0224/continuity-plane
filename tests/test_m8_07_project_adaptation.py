"""M8-07 ProjectAdaptation lifecycle behavior tests."""

from __future__ import annotations

import copy
import unittest

from context_control_plane.project_adaptation import (
    ProjectAdaptationError,
    ProjectAdaptationLoop,
)


NOW = "2026-08-16T12:00:00Z"


def _changes() -> dict:
    return {
        "retrieval_order": ["rg", "lsp"],
        "common_path_refs": ["path://src"],
        "common_command_refs": ["command://test"],
        "verification_hint_refs": ["verification://focused"],
        "skill_applicability": ["skill://tdd"],
        "presentation_preferences": {"response_density": "compact"},
    }


def _metrics(*, repeated_read_bytes: int = 100) -> dict:
    return {
        "bytes_read": 1000,
        "bytes_emitted": 500,
        "repeated_read_bytes": repeated_read_bytes,
        "verification_failures": 0,
    }


def _vetoes(value: bool = True) -> dict[str, bool]:
    return {key: value for key in ("E1", "E2", "E4", "E6", "E8", "E9")}


class M807ProjectAdaptationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = ProjectAdaptationLoop(
            project_id="project-m8-07",
            profile_id="profile-m8-07",
            clock=lambda: NOW,
        )
        self.observation = self.loop.observe(
            run_ref="run://verified-1",
            state_revision=7,
            verified=True,
            metrics=_metrics(),
            correction_refs=["correction://compact-output"],
            failure_fixture_refs=["fixture://repeat-read"],
        )

    def _candidate(self, *, version: str = "1.0.0") -> dict:
        return self.loop.propose(
            observation_refs=[self.observation["observation_id"]],
            version=version,
            scope="project",
            applicability=[{"kind": "project", "ref": "project://project-m8-07"}],
            changes=_changes(),
            metrics_before=_metrics(),
            metrics_after=_metrics(repeated_read_bytes=50),
        )

    def test_unapproved_proposal_cannot_activate(self) -> None:
        proposal = self._candidate()

        with self.assertRaisesRegex(ProjectAdaptationError, "approval"):
            self.loop.activate(proposal["adaptation_id"])
        self.assertIsNone(self.loop.active_version)
        self.assertEqual(self.loop.authority_snapshot()["active_task_id"], "task-m8-07")

    def test_shadow_replay_requires_three_identical_runs(self) -> None:
        proposal = self._candidate()

        receipt = self.loop.shadow(
            proposal["adaptation_id"],
            fixture_ref="fixture://repeat-read",
            provider_id="codex",
            budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
            attempts=3,
        )

        self.assertEqual(receipt["attempts"], 3)
        self.assertTrue(receipt["deterministic"])
        self.assertEqual(self.loop.proposals[proposal["adaptation_id"]]["status"], "shadow")
        self.assertEqual(receipt["provider_invocations"], 0)

    def test_nondeterministic_shadow_is_quarantined(self) -> None:
        proposal = self._candidate()
        counter = {"value": 0}

        def unstable(variant, *_args):
            counter["value"] += 1
            return {"projection": {"variant": variant, "value": counter["value"]}}

        receipt = self.loop.shadow(
            proposal["adaptation_id"],
            fixture_ref="fixture://repeat-read",
            provider_id="codex",
            budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
            attempts=3,
            evaluator=unstable,
        )

        self.assertFalse(receipt["deterministic"])
        self.assertEqual(self.loop.proposals[proposal["adaptation_id"]]["status"], "quarantined")

    def test_approval_and_activation_bind_digest_revision_and_vetoes(self) -> None:
        proposal = self._candidate()
        self.loop.shadow(
            proposal["adaptation_id"],
            fixture_ref="fixture://repeat-read",
            provider_id="codex",
            budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
            attempts=3,
        )
        approved = self.loop.approve(
            proposal["adaptation_id"],
            approval_ref="approval://m8-07/1",
            approval_round=1,
        )
        activated = self.loop.activate(proposal["adaptation_id"])

        self.assertEqual(approved["content_sha256"], proposal["content_sha256"])
        self.assertEqual(activated["proposal_sha256"], proposal["content_sha256"])
        self.assertGreater(activated["activation_revision"], proposal["proposal_revision"])
        self.assertEqual(self.loop.active_version, "1.0.0")
        self.assertTrue(all(activated["safety_veto_results"].values()))
        self.assertEqual(activated["state_write_authority"], False)

    def test_rollback_restores_prior_veto_snapshot_and_preserves_authority(self) -> None:
        first = self._candidate()
        self.loop.shadow(first["adaptation_id"], fixture_ref="fixture://a", provider_id="codex", budget={"input_tokens": 1, "output_tokens": 1, "tool_calls": 0}, attempts=3)
        self.loop.approve(first["adaptation_id"], approval_ref="approval://m8-07/1", approval_round=1)
        self.loop.activate(first["adaptation_id"])

        second = self._candidate(version="1.1.0")
        self.loop.shadow(second["adaptation_id"], fixture_ref="fixture://b", provider_id="codex", budget={"input_tokens": 1, "output_tokens": 1, "tool_calls": 0}, attempts=3)
        self.loop.approve(second["adaptation_id"], approval_ref="approval://m8-07/2", approval_round=1)
        self.loop.activate(second["adaptation_id"])
        before = copy.deepcopy(self.loop.authority_snapshot())

        receipt = self.loop.rollback(second["adaptation_id"], target_version="1.0.0")

        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(self.loop.active_version, "1.0.0")
        self.assertTrue(all(receipt["safety_veto_results"].values()))
        self.assertEqual(self.loop.authority_snapshot(), before)

    def test_opt_out_and_reset_remove_active_adaptation_without_erasing_history(self) -> None:
        proposal = self._candidate()
        self.loop.shadow(proposal["adaptation_id"], fixture_ref="fixture://a", provider_id="codex", budget={"input_tokens": 1, "output_tokens": 1, "tool_calls": 0}, attempts=3)
        self.loop.approve(proposal["adaptation_id"], approval_ref="approval://m8-07/1", approval_round=1)
        self.loop.activate(proposal["adaptation_id"])

        receipt = self.loop.reset()

        self.assertEqual(receipt["status"], "reset")
        self.assertIsNone(self.loop.active_version)
        self.assertIn(proposal["adaptation_id"], self.loop.proposals)
        self.assertEqual(receipt["provider_invocations"], 0)


if __name__ == "__main__":
    unittest.main()
