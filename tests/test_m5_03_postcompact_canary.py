"""M5-03 deterministic PostCompact write-gate behavior."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.checkpoint import publish_checkpoint
from context_control_plane.compaction_checkpoint import (
    DeepSeekCompactionHookAdapter,
    PiCompactionHookAdapter,
    build_material_event_delta,
)
from context_control_plane.execution_packet_benchmark import benchmark_fixture
from context_control_plane.postcompact_canary import (
    PostCompactCanaryError,
    canonical_postcompact_canary_bytes,
    evaluate_postcompact_canary,
    validate_postcompact_canary_receipt,
)


class M503PostCompactCanaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture = benchmark_fixture(cls.root)
        cls.snapshot = fixture["snapshot"]
        cls.packet = fixture["packet"]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = LocalArtifactStore(Path(directory.name) / "artifacts")
        self.store.initialize()
        self.event_head = {"sequence_no": 1, "event_sha256": self._event()["event_sha256"]}
        read_result = {
            "snapshot": copy.deepcopy(self.snapshot),
            "revision": 7,
            "event_head": copy.deepcopy(self.event_head),
            "registry_digest": "d" * 64,
            "capabilities": {
                "schema_version": "context.state-store-capabilities/v1alpha1",
                "adapter_id": "context.sqlite",
                "adapter_version": "1.0.0-alpha.1",
                "authority_mode": "local",
                "operations": ["create_project", "read_project", "read_events", "commit_event"],
                "shared_authority": False,
                "offline_write": True,
                "unique_claim": False,
                "multi_writer": True,
                "lease_clock": "process",
                "artifact_scope": "local",
                "expected_revision": True,
                "migration_source": True,
                "migration_target": True,
            },
        }
        self.checkpoint_ref = publish_checkpoint(
            read_result, self.store, canonical_plan_sha256=self.packet["canonical_plan_sha256"]
        )
        self.packet_ref = self.store.put_bytes(
            json.dumps(
                self.packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        self.binding = {
            "schema_version": "context.postcompact-authority-binding/v1alpha1",
            "project_id": self.packet["project_id"],
            "project_revision": self.packet["project_revision"],
            "event_head": copy.deepcopy(self.event_head),
            "governance_ref": self.packet["governance_ref"],
            "canonical_plan_sha256": self.packet["canonical_plan_sha256"],
            "registry_digest": "d" * 64,
            "state_sha256": self.packet["state_sha256"],
            "checkpoint_ref": self.checkpoint_ref.to_document(),
            "expected_packet_ref": self.packet_ref.to_document(),
            "active_work_id": self.packet["active_leaf"]["work_id"],
            "task_revision": self.packet["active_leaf"]["revision"],
            "effect_high_watermark": self.snapshot["project"]["effect_high_watermark"],
        }

    def _event(self):
        event = {
            "schema_version": "context.state-event/v4alpha1",
            "event_id": "event-m5-03",
            "event_type": "state-transition",
            "project_id": self.snapshot["project"]["project_id"],
            "sequence_no": 1,
            "revision_before": 7,
            "revision_after": 7,
            "occurred_at": "2026-08-15T00:10:00+08:00",
            "actor_ref": "actor://executor",
            "causation_ref": "run:m5-03",
            "correlation_ref": "task:m5-03",
            "previous_event_sha256": None,
            "supersedes_event_id": None,
            "changes": [],
            "project_after": copy.deepcopy(self.snapshot["project"]),
            "task_transition": None,
            "experiment_transition": None,
        }
        event["event_sha256"] = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return event

    def _delta(self):
        return build_material_event_delta(
            snapshot=copy.deepcopy(self.snapshot),
            base_checkpoint_ref=self.checkpoint_ref.to_document(),
            base_revision=7,
            base_event_head=None,
            events=[self._event()],
            execution_packet=copy.deepcopy(self.packet),
            observed_at="2026-08-15T00:10:00+08:00",
        )

    def _case(self, provider_id="pi"):
        delta = self._delta()
        if provider_id == "pi":
            metadata = {"cut_point": 128, "split_turn": True, "usage_tokens": 2048}
            hook = PiCompactionHookAdapter().capture(
                delta, metadata=metadata, observed_at="2026-08-15T00:10:00+08:00"
            )
        else:
            metadata = {"checkpoint_kind": "semantic", "checkpoint_sequence": 4}
            hook = DeepSeekCompactionHookAdapter().capture(
                delta, metadata=metadata, observed_at="2026-08-15T00:10:00+08:00"
            )
        return delta, hook, metadata

    def _evaluate(self, provider_id="pi", **overrides):
        delta, hook, metadata = self._case(provider_id)
        arguments = {
            "artifact_store": self.store,
            "trusted_binding": copy.deepcopy(self.binding),
            "restored_packet": copy.deepcopy(self.packet),
            "delta": delta,
            "hook_receipt": hook,
            "observed_host_metadata": metadata,
            "observed_at": "2026-08-15T00:11:00+08:00",
        }
        arguments.update(overrides)
        return evaluate_postcompact_canary(**arguments)

    def test_exact_pi_and_deepseek_restore_opens_only_the_execution_gate(self):
        for provider_id in ("pi", "deepseek"):
            with self.subTest(provider_id=provider_id):
                receipt = self._evaluate(provider_id)
                validate_postcompact_canary_receipt(receipt)
                self.assertEqual(receipt["status"], "pass")
                self.assertEqual(receipt["execution_gate"], "allow")
                self.assertFalse(receipt["state_write_authority"])
                self.assertFalse(receipt["provider_native_authority"])
                self.assertEqual(receipt["active_work_id"], self.packet["active_leaf"]["work_id"])
                self.assertEqual(
                    receipt["decision_ids"],
                    sorted(item["decision_id"] for item in self.packet["decisions"]),
                )
                self.assertEqual(
                    receipt["constraint_ids"],
                    sorted(item["constraint_id"] for item in self.packet["constraints"]),
                )

    def test_canary_is_deterministic_for_identical_restore_inputs(self):
        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(
            canonical_postcompact_canary_bytes(first),
            canonical_postcompact_canary_bytes(second),
        )

    def test_decision_constraint_and_active_work_loss_fail_closed(self):
        mutations = (
            lambda packet: packet["decisions"].clear(),
            lambda packet: packet["constraints"].clear(),
            lambda packet: packet["active_leaf"].__setitem__("work_id", "work-other"),
        )
        for mutation in mutations:
            restored = copy.deepcopy(self.packet)
            mutation(restored)
            with self.subTest(mutation=mutation), self.assertRaises(PostCompactCanaryError):
                self._evaluate(restored_packet=restored)

    def test_packet_delta_and_watermark_mismatch_fail_closed(self):
        delta, hook, metadata = self._case()
        for label, mutate in (
            ("packet", lambda local_delta, _hook: local_delta.__setitem__("execution_packet_sha256", "a" * 64)),
            ("watermark", lambda local_delta, _hook: local_delta.__setitem__("effect_high_watermark", 99)),
            ("hook", lambda _delta, local_hook: local_hook.__setitem__("delta_sha256", "b" * 64)),
        ):
            local_delta = copy.deepcopy(delta)
            local_hook = copy.deepcopy(hook)
            mutate(local_delta, local_hook)
            with self.subTest(label=label), self.assertRaises(PostCompactCanaryError):
                evaluate_postcompact_canary(
                    artifact_store=self.store,
                    trusted_binding=copy.deepcopy(self.binding),
                    restored_packet=copy.deepcopy(self.packet),
                    delta=local_delta,
                    hook_receipt=local_hook,
                    observed_host_metadata=metadata,
                    observed_at="2026-08-15T00:11:00+08:00",
                )

    def test_pi_cut_point_and_split_turn_mismatch_fail_closed(self):
        delta, hook, metadata = self._case("pi")
        for observed in (
            {**metadata, "cut_point": 127},
            {**metadata, "split_turn": False},
        ):
            with self.subTest(observed=observed), self.assertRaises(PostCompactCanaryError):
                evaluate_postcompact_canary(
                    artifact_store=self.store,
                    trusted_binding=copy.deepcopy(self.binding),
                    restored_packet=copy.deepcopy(self.packet),
                    delta=delta,
                    hook_receipt=hook,
                    observed_host_metadata=observed,
                    observed_at="2026-08-15T00:11:00+08:00",
                )

    def test_deepseek_checkpoint_mismatch_fails_closed(self):
        delta, hook, metadata = self._case("deepseek")
        observed = {**metadata, "checkpoint_sequence": 5}
        with self.assertRaises(PostCompactCanaryError):
            evaluate_postcompact_canary(
                artifact_store=self.store,
                trusted_binding=copy.deepcopy(self.binding),
                restored_packet=copy.deepcopy(self.packet),
                delta=delta,
                hook_receipt=hook,
                observed_host_metadata=observed,
                observed_at="2026-08-15T00:11:00+08:00",
            )

    def test_self_consistent_forged_packet_set_cannot_replace_checkpoint_truth(self):
        forged = copy.deepcopy(self.packet)
        forged["active_leaf"]["title"] = "forged but internally consistent title"
        body = copy.deepcopy(forged)
        body.pop("packet_sha256")
        forged["packet_sha256"] = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        forged_ref = self.store.put_bytes(
            json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        binding = copy.deepcopy(self.binding)
        binding["expected_packet_ref"] = forged_ref.to_document()
        delta = build_material_event_delta(
            snapshot=copy.deepcopy(self.snapshot),
            base_checkpoint_ref=self.checkpoint_ref.to_document(),
            base_revision=7,
            base_event_head=None,
            events=[self._event()],
            execution_packet=forged,
            observed_at="2026-08-15T00:10:00+08:00",
        )
        metadata = {"cut_point": 128, "split_turn": True, "usage_tokens": 2048}
        hook = PiCompactionHookAdapter().capture(
            delta, metadata=metadata, observed_at="2026-08-15T00:10:00+08:00"
        )
        with self.assertRaises(PostCompactCanaryError):
            evaluate_postcompact_canary(
                artifact_store=self.store,
                trusted_binding=binding,
                restored_packet=forged,
                delta=delta,
                hook_receipt=hook,
                observed_host_metadata=metadata,
                observed_at="2026-08-15T00:11:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
