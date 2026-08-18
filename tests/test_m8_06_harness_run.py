"""M8-06 provider-neutral Harness Run contract and failure gates."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from context_control_plane.harness_run import (
    HARNESS_RUN_SCHEMA_VERSION,
    HarnessCoordinator,
    HarnessRunError,
    create_harness_run,
    replay_harness_events,
    validate_harness_run,
)

NOW = "2026-08-16T20:00:00+08:00"


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run(
    *,
    run_id: str = "run-parent",
    task_id: str = "task-m8-06",
    task_revision: int = 7,
    provider: str = "codex",
    provider_contract_version: str = "provider-contract/v1",
    status: str = "running",
    parent_run_id: str | None = None,
    claim_id: str = "claim-parent",
    scope_refs: list[dict[str, str]] | None = None,
) -> dict:
    return create_harness_run(
        run_id=run_id,
        project_id="project-m8-06",
        task_id=task_id,
        task_revision=task_revision,
        claim_id=claim_id,
        claim_lease_epoch=2,
        claim_fence=2,
        provider=provider,
        provider_contract_version=provider_contract_version,
        execution_packet_sha256="a" * 64,
        skill_set_digest="b" * 64,
        tool_grants=["repo.read", "state.commit"],
        checkpoint_id="checkpoint-m8-06",
        effect_high_watermark=11,
        verification_profile_id="profile-default",
        reference_validity_watermark=3,
        trace_id="trace-m8-06",
        status=status,
        parent_run_id=parent_run_id,
        scope_refs=scope_refs
        or [{"scope_kind": "file", "scope_ref": "context_control_plane/harness_run.py"}],
        created_at=NOW,
        updated_at=NOW,
    )


class M806HarnessRunTests(unittest.TestCase):
    def test_provider_drift_is_quarantined_before_dispatch(self) -> None:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
        )
        coordinator.register_run(_run())
        drifted = _run(
            run_id="run-drifted",
            parent_run_id="run-parent",
            claim_id="claim-drifted",
            provider_contract_version="provider-contract/v0",
        )

        with self.assertRaisesRegex(HarnessRunError, "provider contract drift"):
            coordinator.dispatch_worker("run-parent", drifted)
        self.assertNotIn("run-drifted", coordinator.runs)
        self.assertEqual(coordinator.authority_snapshot()["task_revision"], 7)

    def test_worker_loss_does_not_change_state_mcp_authority(self) -> None:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
        )
        coordinator.register_run(_run())
        worker = _run(
            run_id="run-worker",
            parent_run_id="run-parent",
            claim_id="claim-worker",
        )
        coordinator.dispatch_worker("run-parent", worker)
        before = copy.deepcopy(coordinator.authority_snapshot())

        receipt = coordinator.mark_worker_lost("run-worker")

        self.assertEqual(receipt["status"], "lost")
        self.assertEqual(coordinator.authority_snapshot(), before)
        self.assertEqual(coordinator.runs["run-worker"]["status"], "failed")
        self.assertEqual(
            coordinator.events[-1]["payload"]["run_sha256"],
            coordinator.runs["run-worker"]["run_sha256"],
        )

    def test_effect_without_active_claim_is_rejected_without_record(self) -> None:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
            effect_dispatcher=lambda intent: {"accepted": True, "intent": intent},
        )
        with self.assertRaisesRegex(HarnessRunError, "active claim"):
            _run(run_id="run-unclaimed", claim_id="")

        coordinator.register_run(_run())
        with self.assertRaisesRegex(HarnessRunError, "scope"):
            coordinator.commit_effect(
                "run-parent",
                effect_id="effect-outside-scope",
                effect_key="effect:outside-scope",
                operation="write-file",
                scope_ref={"scope_kind": "file", "scope_ref": "other.py"},
                expected_project_revision=7,
            )
        self.assertEqual(coordinator.effects, {})

    def test_effect_is_idempotent_when_claim_and_revision_are_valid(self) -> None:
        calls: list[dict] = []

        def dispatch(intent: dict) -> dict:
            calls.append(intent)
            return {"accepted": True, "effect_id": intent["effect_id"]}

        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
            effect_dispatcher=dispatch,
        )
        coordinator.register_run(_run())
        kwargs = {
            "effect_id": "effect-write",
            "effect_key": "effect:write",
            "operation": "write-file",
            "scope_ref": {
                "scope_kind": "file",
                "scope_ref": "context_control_plane/harness_run.py",
            },
            "expected_project_revision": 7,
        }
        first = coordinator.commit_effect("run-parent", **kwargs)
        second = coordinator.commit_effect("run-parent", **kwargs)

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["status"], "started")

    def test_stale_handoff_is_rejected_and_first_action_is_bound(self) -> None:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
        )
        coordinator.register_run(_run())
        target = _run(
            run_id="run-target",
            parent_run_id="run-parent",
            claim_id="claim-target",
            status="proposed",
        )
        coordinator.register_run(target)
        coordinator.runs["run-parent"]["status"] = "waiting"
        with self.assertRaisesRegex(HarnessRunError, "stale handoff"):
            coordinator.create_handoff(
                "run-parent",
                "run-target",
                checkpoint_id="checkpoint-m8-06",
                next_action="run verifier",
                expected_task_revision=6,
            )

        handoff = coordinator.create_handoff(
            "run-parent",
            "run-target",
            checkpoint_id="checkpoint-m8-06",
            next_action="run verifier",
            expected_task_revision=7,
        )
        with self.assertRaisesRegex(HarnessRunError, "first action"):
            coordinator.acknowledge_handoff(handoff["handoff_id"], "edit code")
        accepted = coordinator.acknowledge_handoff(
            handoff["handoff_id"], "run verifier"
        )
        self.assertEqual(accepted["status"], "accepted")

    def test_fan_in_requires_terminal_workers_and_replay_is_provider_neutral(self) -> None:
        coordinator = HarnessCoordinator(
            project_id="project-m8-06",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: NOW,
        )
        coordinator.register_run(_run())
        worker_ids = []
        for index in range(2):
            run_id = f"run-worker-{index}"
            worker_ids.append(run_id)
            coordinator.dispatch_worker(
                "run-parent",
                _run(
                    run_id=run_id,
                    parent_run_id="run-parent",
                    claim_id=f"claim-worker-{index}",
                ),
            )
        with self.assertRaisesRegex(HarnessRunError, "terminal"):
            coordinator.fan_in("run-parent", worker_ids)
        for run_id in worker_ids:
            coordinator.complete_worker(run_id, evidence_sha256="c" * 64)
        receipt = coordinator.fan_in("run-parent", worker_ids)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(
            coordinator.events[-1]["payload"]["run_sha256"],
            coordinator.runs["run-parent"]["run_sha256"],
        )
        events = coordinator.events
        codex_replay = replay_harness_events(events, provider="codex")
        claude_replay = replay_harness_events(events, provider="claude")
        self.assertEqual(codex_replay, claude_replay)
        self.assertEqual(codex_replay["fan_in_count"], 1)

    def test_run_hash_and_schema_are_strict(self) -> None:
        run = _run()
        self.assertEqual(run["schema_version"], HARNESS_RUN_SCHEMA_VERSION)
        self.assertEqual(run["run_sha256"], _sha({k: v for k, v in run.items() if k != "run_sha256"}))
        with self.assertRaisesRegex(HarnessRunError, "fields"):
            broken = {**run, "unexpected": True}
            validate_harness_run(broken)


if __name__ == "__main__":
    unittest.main()
