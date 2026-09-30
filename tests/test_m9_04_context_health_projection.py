"""M9-04 Context, Reference, Harness Health and Replay projection tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.assertion_provenance import compose_assertion_provenance
from context_control_plane.checkpoint import publish_checkpoint
from context_control_plane.claim_evidence_gate import evaluate_claim_evidence_gate
from context_control_plane.compaction_checkpoint import (
    PiCompactionHookAdapter,
    build_material_event_delta,
)
from context_control_plane.context_accounting import (
    compose_context_accounting,
    measured_metric,
)
from context_control_plane.context_accounting_benchmark import (
    benchmark_context_accounting_fixture,
)
from context_control_plane.context_health_projection import (
    ContextHealthProjectionError,
    build_context_health_projection,
    validate_context_health_projection,
)
from context_control_plane.context_trace import append_context_trace_event
from context_control_plane.context_trace_benchmark import REQUIRED_EVENT_NAMES
from context_control_plane.decision_evidence_projection import (
    build_decision_evidence_projection,
)
from context_control_plane.durable_continuation import (
    compose_durable_continuation,
)
from context_control_plane.durable_state_migration import (
    migrate_typed_state_v4_to_v5,
)
from context_control_plane.execution_packet import compose_execution_packet
from context_control_plane.execution_packet_benchmark import benchmark_fixture
from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from context_control_plane.harness_run import HarnessCoordinator, create_harness_run
from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.idea_review import migrate_typed_state_v3_to_v4
from context_control_plane.metric_evidence import (
    MetricEvidenceError,
    compose_metric_evidence,
)
from context_control_plane.postcompact_canary import evaluate_postcompact_canary
from context_control_plane.reference_watcher import (
    decide_reference_watch,
    observe_reference,
)
from context_control_plane.shared_state_migration import migrate_typed_state_v5_to_v6
from context_control_plane.skill_resolver import (
    canonical_skill_resolution_request_bytes,
)
from context_control_plane.state_mcp import RequestContext
from context_control_plane.typed_state import (
    canonical_state_bytes,
    validate_typed_state,
)

NOW = "2026-08-17T22:00:00+08:00"
PLAN_SHA256 = "f" * 64
REGISTRY_SHA256 = "a" * 64
REFERENCE_WATERMARK = 7
SOURCE_REF = "https://example.invalid/<script>alert(1)</script>"
STATE_SOURCE_REF = "state://project/project-idea-benchmark/revision/9"
BASELINE = b"official specification revision one\n"
CHANGED = b"official specification revision two\n"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _attach_metric_evidence(
    fixture: Any,
    receipt: dict[str, Any],
    route: dict[str, Any],
    metric_name: str,
    evidence_ref: str,
) -> None:
    metric = route["metrics"][metric_name]
    fixture.artifacts[evidence_ref] = compose_metric_evidence(
        accounting_id=receipt["accounting_id"],
        accounting_sha256=receipt["accounting_sha256"],
        corpus_id=receipt["corpus_id"],
        corpus_sha256=receipt["corpus_sha256"],
        route_id=route["route_id"],
        provider_id=route["provider_id"],
        trace_event_id="event-m9-04-8",
        run_id="run-m9-04",
        observed_at=receipt["observed_at"],
        metrics={
            metric_name: {
                "value": metric["value"],
                "unit": metric["unit"],
                "source_kind": metric["source_kind"],
            }
        },
    )


def _attach_all_metric_evidence(fixture: Any, receipt: dict[str, Any]) -> None:
    for route in receipt["routes"]:
        by_ref: dict[str, dict[str, dict[str, Any]]] = {}
        for name, metric in route["metrics"].items():
            if metric["status"] == "measured":
                by_ref.setdefault(metric["evidence_ref"], {})[name] = {
                    "value": metric["value"],
                    "unit": metric["unit"],
                    "source_kind": metric["source_kind"],
                }
        for evidence_ref, metrics in by_ref.items():
            fixture.artifacts[evidence_ref] = compose_metric_evidence(
                accounting_id=receipt["accounting_id"],
                accounting_sha256=receipt["accounting_sha256"],
                corpus_id=receipt["corpus_id"],
                corpus_sha256=receipt["corpus_sha256"],
                route_id=route["route_id"],
                provider_id=route["provider_id"],
                trace_event_id="event-m9-04-8",
                run_id="run-m9-04",
                observed_at=receipt["observed_at"],
                metrics=metrics,
            )


def _rehash_harness_run(run: dict[str, Any]) -> None:
    run["run_sha256"] = _digest(
        {key: value for key, value in run.items() if key != "run_sha256"}
    )


def _append_harness_event(
    events: list[dict[str, Any]], *, event_type: str, payload: dict[str, Any]
) -> None:
    event = {
        "schema_version": "context.harness-event/v1alpha1",
        "event_id": f"event-m9-04-extra-{len(events) + 1}",
        "event_type": event_type,
        "project_id": events[-1]["project_id"],
        "task_revision": events[-1]["task_revision"],
        "sequence_no": len(events) + 1,
        "previous_event_sha256": events[-1]["event_sha256"],
        "payload": payload,
        "observed_at": "2026-08-17T21:06:00+08:00",
    }
    event["event_sha256"] = _digest(event)
    events.append(event)


class _Source:
    def __init__(self, snapshot: dict[str, Any], event_head: dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(snapshot)
        self.event_head = copy.deepcopy(event_head)

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        del tool, context
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": copy.deepcopy(self.event_head),
                "registry_digest": REGISTRY_SHA256,
                "capabilities": {"adapter_id": "context.m9-04-test"},
            },
            "error": None,
        }


def _capabilities() -> dict[str, Any]:
    return {
        "schema_version": "context.state-store-capabilities/v1alpha1",
        "adapter_id": "context.sqlite",
        "adapter_version": "1.0.0-alpha.1",
        "authority_mode": "local",
        "operations": [
            "create_project",
            "read_project",
            "read_events",
            "commit_event",
        ],
        "shared_authority": False,
        "offline_write": True,
        "unique_claim": False,
        "multi_writer": True,
        "lease_clock": "process",
        "artifact_scope": "local",
        "expected_revision": True,
        "migration_source": True,
        "migration_target": True,
    }


class M904Fixture:
    def __init__(self, root: Path, artifact_store: LocalArtifactStore) -> None:
        self.root = root
        self.artifact_store = artifact_store
        self.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-04-test",
            secret=b"m9-04-context-health-projection-test-key",
        )
        self.artifacts: dict[str, bytes] = {}
        self.evidence_payloads: dict[tuple[str, str], bytes] = {}
        self.snapshot = self._snapshot()
        self.state_event = self._state_event()
        self.event_head = {
            "sequence_no": self.state_event["sequence_no"],
            "event_sha256": self.state_event["event_sha256"],
        }
        self.source_projection = self._external_projection()
        self.provenance_bundle = self._provenance_bundle()
        self.decision_projection = build_decision_evidence_projection(
            self.source_projection,
            signer=self.signer,
            observed_at=NOW,
            provenance_bundle=self.provenance_bundle,
            evidence_resolver=self.evidence_resolver,
            artifact_resolver=self.artifact_resolver,
        )
        self.packet = self._execution_packet()
        self.checkpoint_ref = self._checkpoint()
        self.compaction_records = self._compaction_records()
        self.accounting_records = self._accounting_records()
        self.trace_events = self._trace_events()
        self.reference_records = self._reference_records()
        self.harness_runs, self.harness_events = self._harness_sources()
        self.recovery_records = self._recovery_records()

    def artifact_resolver(self, ref: str) -> bytes | None:
        return self.artifacts.get(ref)

    def evidence_resolver(self, source_ref: str, revision: str) -> bytes | None:
        return self.evidence_payloads.get((source_ref, revision))

    @staticmethod
    def trusted_time_verifier(kind: str, trusted_at: str, evidence: bytes) -> bool:
        return evidence == f"{kind}:{trusted_at}".encode()

    def _snapshot(self) -> dict[str, Any]:
        snapshot = build_idea_snapshot()
        snapshot["project"]["updated_at"] = "2026-08-17T20:04:00+08:00"
        claim = snapshot["claims"][0]
        claim["claimed_at"] = "2026-08-17T20:00:00+08:00"
        claim["lease_expires_at"] = "2026-08-18T20:00:00+08:00"
        evidence_sha256 = hashlib.sha256(BASELINE).hexdigest()
        snapshot["evidence"] = [
            {
                "evidence_id": "evidence-m9-04-reference",
                "kind": "standard",
                "artifact_ref": f"artifact://sha256/{evidence_sha256}",
                "content_sha256": evidence_sha256,
                "validity": "verified",
                "observed_at": "2026-08-17T20:00:00+08:00",
                "verified_at": "2026-08-17T20:01:00+08:00",
            }
        ]
        snapshot["decisions"] = [
            {
                "decision_id": "decision-m9-04-current",
                "work_id": "work-active",
                "status": "accepted",
                "statement": "Use the verified current reference.",
                "decided_at": "2026-08-17T20:03:00+08:00",
                "supersedes_decision_id": None,
                "evidence_ids": ["evidence-m9-04-reference"],
            }
        ]
        snapshot["project"]["current_decision_ids"] = [
            "decision-m9-04-current"
        ]
        validate_typed_state(snapshot)
        migrated = migrate_typed_state_v3_to_v4(
            snapshot,
            migrated_at="2026-08-17T20:04:00+08:00",
        )
        migrated = migrate_typed_state_v4_to_v5(migrated)
        migrated = migrate_typed_state_v5_to_v6(migrated)
        validate_typed_state(migrated)
        return migrated

    def _state_event(self) -> dict[str, Any]:
        event = {
            "schema_version": "context.state-event/v4alpha1",
            "event_id": "event-m9-04-state",
            "event_type": "state-transition",
            "project_id": self.snapshot["project"]["project_id"],
            "sequence_no": 1,
            "revision_before": self.snapshot["project"]["revision"],
            "revision_after": self.snapshot["project"]["revision"],
            "occurred_at": "2026-08-17T20:05:00+08:00",
            "actor_ref": "actor://executor",
            "causation_ref": "run:m9-04",
            "correlation_ref": "task:m9-04",
            "previous_event_sha256": None,
            "supersedes_event_id": None,
            "changes": [],
            "project_after": copy.deepcopy(self.snapshot["project"]),
            "task_transition": None,
            "experiment_transition": None,
        }
        event["event_sha256"] = hashlib.sha256(_canonical(event)).hexdigest()
        return event

    def _external_projection(self) -> dict[str, Any]:
        response = ExternalStateProjectionProvider(
            _Source(self.snapshot, self.event_head),
            provider_id="provider-m9-04-test",
            signer=self.signer,
        ).call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-04",
                "project_id": self.snapshot["project"]["project_id"],
                "expected_revision": self.snapshot["project"]["revision"],
            },
            context=RequestContext("actor-reader", "authorization-reader"),
        )
        if not response["ok"]:
            raise AssertionError(response["error"])
        return response["result"]

    def _retrieval_receipt(self) -> str:
        payload = (
            self.root / "experiments/retrieval/m6-01-retrieval-receipt.json"
        ).read_bytes()
        ref = "artifact://sha256/" + hashlib.sha256(payload).hexdigest()
        self.artifacts[ref] = payload
        return ref

    def _provenance_bundle(self) -> dict[str, Any]:
        evidence_sha256 = hashlib.sha256(BASELINE).hexdigest()
        self.evidence_payloads[(STATE_SOURCE_REF, "state:revision:9")] = BASELINE
        self.evidence_payloads[(SOURCE_REF, "revision-1")] = BASELINE
        assertion = compose_assertion_provenance(
            assertion_id="assertion-m9-04-reference",
            assertion_text="The current reference supports the active Decision.",
            bearing=True,
            evidence=[
                {
                    "evidence_id": "evidence-m9-04-reference",
                    "authority_kind": "current_state",
                    "source_ref": STATE_SOURCE_REF,
                    "revision": "state:revision:9",
                    "sha256": evidence_sha256,
                    "valid_at": "2026-08-17T20:01:00+08:00",
                    "retrieval_receipt_ref": self._retrieval_receipt(),
                },
                {
                    "evidence_id": "evidence-m9-04-reference-watch",
                    "authority_kind": "industry_standard",
                    "source_ref": SOURCE_REF,
                    "revision": "revision-1",
                    "sha256": evidence_sha256,
                    "valid_at": "2026-08-17T20:01:00+08:00",
                    "retrieval_receipt_ref": self._retrieval_receipt(),
                }
            ],
            asserted_at="2026-08-17T20:02:00+08:00",
            valid_until="2026-09-17T20:00:00+08:00",
            evidence_resolver=self.evidence_resolver,
            artifact_resolver=self.artifact_resolver,
        )
        claim = {
            "claim_id": "claim-m9-04-decision",
            "work_id": "work-active",
            "claim_kind": "decision",
            "statement": "Use the verified current reference.",
            "evidence_assertion_ids": [assertion["assertion_id"]],
            "scope_refs": [],
        }
        verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=[assertion],
            current_time="2026-08-17T20:02:30+08:00",
            evidence_resolver=self.evidence_resolver,
            artifact_resolver=self.artifact_resolver,
        )
        bundle = {
            "schema_version": (
                "context.decision-evidence-provenance-bundle/v1alpha1"
            ),
            "project_id": self.snapshot["project"]["project_id"],
            "state_revision": self.snapshot["project"]["revision"],
            "state_sha256": self.source_projection["state_sha256"],
            "source_projection_sha256": self.source_projection[
                "projection_sha256"
            ],
            "assertion_records": [assertion],
            "claims": [claim],
            "claim_verdicts": [verdict],
            "bindings": [
                {
                    "object_kind": "decision",
                    "object_id": "decision-m9-04-current",
                    "typed_evidence_id": "evidence-m9-04-reference",
                    "assertion_id": assertion["assertion_id"],
                    "assertion_evidence_id": "evidence-m9-04-reference",
                    "assertion_record_sha256": assertion["record_sha256"],
                    "claim_id": claim["claim_id"],
                    "claim_sha256": verdict["claim_sha256"],
                    "gate_id": verdict["gate_id"],
                    "verdict_sha256": verdict["verdict_sha256"],
                }
            ],
            "bundle_sha256": "",
        }
        bundle["bundle_sha256"] = _digest(
            {key: value for key, value in bundle.items() if key != "bundle_sha256"}
        )
        return bundle

    def _execution_packet(self) -> dict[str, Any]:
        fixture = benchmark_fixture(self.root)
        request = copy.deepcopy(fixture["skill_request"])
        decision = copy.deepcopy(fixture["skill_decision"])
        request["request_id"] = "resolve-m9-04"
        request["project_ref"] = f"project://{self.snapshot['project']['project_id']}"
        request["task_ref"] = f"task://{self.snapshot['project']['primary_work_id']}"
        decision["request_id"] = request["request_id"]
        decision["request_sha256"] = hashlib.sha256(
            canonical_skill_resolution_request_bytes(request)
        ).hexdigest()
        return compose_execution_packet(
            snapshot=self.snapshot,
            skill_request=request,
            skill_decision=decision,
            compiled_skill_packet=fixture["compiled_skill_packet"],
            next_action="continue M9-04",
            continuation_cursor={
                "last_durable_action": "m9-04-start",
                "in_flight_phase": "ready-to-execute",
                "confirmed_input_refs": ["opaque://input/m9-04"],
                "reserved_effect_ids": [],
                "replay_policy": "verify-before-effect",
            },
            canonical_plan_sha256=PLAN_SHA256,
            observed_at="2026-08-17T20:06:00+08:00",
        )

    def _checkpoint(self):
        return publish_checkpoint(
            {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": copy.deepcopy(self.event_head),
                "registry_digest": REGISTRY_SHA256,
                "capabilities": _capabilities(),
            },
            self.artifact_store,
            canonical_plan_sha256=PLAN_SHA256,
        )

    def _compaction_records(self) -> list[dict[str, Any]]:
        packet_ref = self.artifact_store.put_bytes(_canonical(self.packet))
        delta = build_material_event_delta(
            snapshot=self.snapshot,
            base_checkpoint_ref=self.checkpoint_ref.to_document(),
            base_revision=self.snapshot["project"]["revision"],
            base_event_head=None,
            events=[self.state_event],
            execution_packet=self.packet,
            observed_at="2026-08-17T20:10:00+08:00",
        )
        metadata = {"cut_point": 128, "split_turn": True, "usage_tokens": 2048}
        hook = PiCompactionHookAdapter().capture(
            delta,
            metadata=metadata,
            observed_at="2026-08-17T20:10:00+08:00",
        )
        binding = {
            "schema_version": "context.postcompact-authority-binding/v1alpha1",
            "project_id": self.snapshot["project"]["project_id"],
            "project_revision": self.snapshot["project"]["revision"],
            "event_head": copy.deepcopy(self.event_head),
            "governance_ref": self.snapshot["project"]["governance_ref"],
            "canonical_plan_sha256": PLAN_SHA256,
            "registry_digest": REGISTRY_SHA256,
            "state_sha256": hashlib.sha256(
                canonical_state_bytes(self.snapshot)
            ).hexdigest(),
            "checkpoint_ref": self.checkpoint_ref.to_document(),
            "expected_packet_ref": packet_ref.to_document(),
            "active_work_id": self.snapshot["project"]["primary_work_id"],
            "task_revision": next(
                work["revision"]
                for work in self.snapshot["works"]
                if work["work_id"] == self.snapshot["project"]["primary_work_id"]
            ),
            "effect_high_watermark": self.snapshot["project"][
                "effect_high_watermark"
            ],
        }
        canary = evaluate_postcompact_canary(
            artifact_store=self.artifact_store,
            trusted_binding=binding,
            restored_packet=self.packet,
            delta=delta,
            hook_receipt=hook,
            observed_host_metadata=metadata,
            observed_at="2026-08-17T20:11:00+08:00",
        )
        return [
            {
                "precompact_event_id": "event-m9-04-1",
                "postcompact_event_id": "event-m9-04-2",
                "trusted_binding": binding,
                "restored_packet": self.packet,
                "delta": delta,
                "hook_receipt": hook,
                "observed_host_metadata": metadata,
                "canary_receipt": canary,
            }
        ]

    def _accounting_records(self) -> list[dict[str, Any]]:
        accounting = benchmark_context_accounting_fixture()["accounting"]
        for route in accounting["routes"]:
            by_ref: dict[str, dict[str, dict[str, Any]]] = {}
            for name, metric in route["metrics"].items():
                if metric["status"] == "measured":
                    by_ref.setdefault(metric["evidence_ref"], {})[name] = {
                        "value": metric["value"],
                        "unit": metric["unit"],
                        "source_kind": metric["source_kind"],
                    }
            for evidence_ref, metrics in by_ref.items():
                self.artifacts[evidence_ref] = compose_metric_evidence(
                    accounting_id=accounting["accounting_id"],
                    accounting_sha256=accounting["accounting_sha256"],
                    corpus_id=accounting["corpus_id"],
                    corpus_sha256=accounting["corpus_sha256"],
                    route_id=route["route_id"],
                    provider_id=route["provider_id"],
                    trace_event_id="event-m9-04-8",
                    run_id="run-m9-04",
                    observed_at=accounting["observed_at"],
                    metrics=metrics,
                )
        return [
            {
                "trace_event_id": "event-m9-04-8",
                "receipt": accounting,
                "receipt_ref": "artifact://sha256/"
                + hashlib.sha256(_canonical(accounting)).hexdigest(),
            }
        ]

    def _compaction_artifact_refs(self) -> tuple[list[str], list[str]]:
        record = self.compaction_records[0]
        pre = [
            "artifact://sha256/" + hashlib.sha256(_canonical(record["delta"])).hexdigest(),
            "artifact://sha256/"
            + hashlib.sha256(_canonical(record["hook_receipt"])).hexdigest(),
        ]
        post = [
            "artifact://sha256/"
            + hashlib.sha256(_canonical(record["canary_receipt"])).hexdigest()
        ]
        return pre, post

    def _trace_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        work_id = self.snapshot["project"]["primary_work_id"]
        binding = {
            "project_id": self.snapshot["project"]["project_id"],
            "state_revision": self.snapshot["project"]["revision"],
            "active_work_id": work_id,
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "run_id": "run-m9-04",
            "operation_id": "operation-m9-04",
            "correlation_id": "correlation-m9-04",
        }
        pre_refs, post_refs = self._compaction_artifact_refs()
        for index, name in enumerate(REQUIRED_EVENT_NAMES, start=1):
            refs = [
                "artifact://sha256/" + hashlib.sha256(name.encode()).hexdigest()
            ]
            if name == "context.compaction.precompact":
                refs = pre_refs
            elif name == "context.compaction.postcompact":
                refs = post_refs
            elif name == "context.delivery.accepted":
                refs = [self.accounting_records[0]["receipt_ref"]]
            append_context_trace_event(
                events,
                event_name=name,
                binding=binding,
                source={
                    "kind": "local_hook",
                    "provider": "provider-neutral",
                    "adapter": "context-trace/v1",
                    "source_ref": "hook://m9-04/live-probe",
                },
                evidence_refs=refs,
                observed_at=f"2026-08-17T21:00:{index:02d}+08:00",
                attributes={"result": "observed"},
                event_id=f"event-m9-04-{index}",
            )
        return events

    def _reference_records(self) -> list[dict[str, Any]]:
        watch = {
            "watch_id": "watch-m9-04-specification",
            "source_id": "reference-m9-04-specification",
            "source_ref": SOURCE_REF,
            "authority_kind": "industry_standard",
            "watch_revision": REFERENCE_WATERMARK,
            "baseline_revision": "revision-1",
            "baseline_sha256": hashlib.sha256(BASELINE).hexdigest(),
            "valid_until": "2026-09-01T00:00:00Z",
            "assertion_ids": ["assertion-m9-04-reference"],
        }
        trusted_time = {
            "kind": "fixture-clock",
            "trusted_at": "2026-08-17T12:00:00Z",
            "evidence": b"fixture-clock:2026-08-17T12:00:00Z",
        }
        observation = observe_reference(
            watch=watch,
            mode="fixture",
            availability_status="available",
            observed_revision="revision-2",
            observed_content=CHANGED,
            unavailability_evidence=None,
            trusted_time=trusted_time,
            trusted_time_verifier=self.trusted_time_verifier,
        )
        self.artifacts[observation["current_evidence_ref"]] = CHANGED
        self.artifacts[observation["trusted_time_evidence_ref"]] = trusted_time[
            "evidence"
        ]
        decision = decide_reference_watch(
            observation,
            expected_watch=watch,
            artifact_resolver=self.artifact_resolver,
            trusted_time_verifier=self.trusted_time_verifier,
        )
        return [
            {
                "reference_validity_watermark": REFERENCE_WATERMARK,
                "watch": watch,
                "observation": observation,
                "decision": decision,
                "replacement_assertion": None,
            }
        ]

    def _harness_sources(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        work = next(
            item for item in self.snapshot["works"] if item["work_id"] == "work-active"
        )
        claim = next(
            item
            for item in self.snapshot["claims"]
            if item["work_id"] == work["work_id"] and item["status"] == "active"
        )
        coordinator = HarnessCoordinator(
            project_id=self.snapshot["project"]["project_id"],
            task_revision=work["revision"],
            provider_contract_version="provider-contract/v1",
            clock=lambda: "2026-08-17T21:05:00+08:00",
        )
        run = create_harness_run(
            run_id="run-m9-04",
            project_id=self.snapshot["project"]["project_id"],
            task_id=work["work_id"],
            task_revision=work["revision"],
            claim_id=claim["claim_id"],
            claim_lease_epoch=claim["lease_epoch"],
            claim_fence=claim["lease_epoch"],
            provider="codex",
            provider_contract_version="provider-contract/v1",
            execution_packet_sha256=self.packet["packet_sha256"],
            skill_set_digest="d" * 64,
            tool_grants=["repo.read"],
            checkpoint_id=self.checkpoint_ref.digest,
            effect_high_watermark=self.snapshot["project"][
                "effect_high_watermark"
            ],
            verification_profile_id="profile-m9-04",
            reference_validity_watermark=REFERENCE_WATERMARK,
            trace_id="trace-m9-04",
            status="running",
            parent_run_id=None,
            scope_refs=copy.deepcopy(work["scope_refs"]),
            created_at="2026-08-17T21:00:00+08:00",
            updated_at="2026-08-17T21:00:00+08:00",
        )
        coordinator.register_run(run)
        coordinator.mark_worker_lost(run["run_id"])
        return list(coordinator.runs.values()), coordinator.events

    def _recovery_records(self) -> list[dict[str, Any]]:
        work = next(
            item for item in self.snapshot["works"] if item["work_id"] == "work-active"
        )
        durable = compose_durable_continuation(
            operation_id="operation-m9-04",
            project_id=self.snapshot["project"]["project_id"],
            project_revision=self.snapshot["project"]["revision"],
            task_id=work["work_id"],
            task_revision=work["revision"],
            event_head=self.event_head,
            phase="effect-in-flight",
            last_durable_action="effect-intent:effect-m9-04",
            next_action="verify-effect:effect-m9-04",
            acknowledged_input_ids=["input-m9-04-answered"],
            reserved_effects=[
                {
                    "effect_id": "effect-m9-04",
                    "replay_policy": "never",
                    "status": "started",
                }
            ],
            response_mode="continue-silently",
        )
        common = {
            "checkpoint_ref": self.checkpoint_ref.to_document(),
            "canonical_plan_sha256": PLAN_SHA256,
            "effect_high_watermark": self.snapshot["project"][
                "effect_high_watermark"
            ],
            "durable_state": durable,
            "restored_state": durable,
            "response_input_id": None,
            "requested_effect_id": "effect-m9-04",
            "replay_requested": False,
            "recovery_reads": [
                {
                    "source_ref": self.checkpoint_ref.uri,
                    "content_sha256": self.checkpoint_ref.digest,
                    "bytes_read": self.checkpoint_ref.size_bytes,
                }
            ],
            "recovery_budget_bytes": 64 * 1024,
        }
        return [
            {
                **copy.deepcopy(common),
                "recovery_id": "recovery-m9-04-pass",
                "proposed_first_action": durable["next_action"],
                "observed_at": "2026-08-17T21:10:00+08:00",
            },
            {
                **copy.deepcopy(common),
                "recovery_id": "recovery-m9-04-fail",
                "proposed_first_action": "restart-from-plan",
                "observed_at": "2026-08-17T21:11:00+08:00",
            },
        ]


class M904ContextHealthProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = LocalArtifactStore(Path(temporary.name) / "artifacts")
        store.initialize()
        self.fixture = M904Fixture(Path(__file__).resolve().parents[1], store)

    def build(self) -> dict[str, Any]:
        f = self.fixture
        return build_context_health_projection(
            source_projection=f.source_projection,
            decision_evidence_projection=f.decision_projection,
            provenance_bundle=f.provenance_bundle,
            trace_events=f.trace_events,
            compaction_records=f.compaction_records,
            accounting_records=f.accounting_records,
            reference_records=f.reference_records,
            harness_runs=f.harness_runs,
            harness_events=f.harness_events,
            recovery_records=f.recovery_records,
            artifact_store=f.artifact_store,
            provider_id="provider-docmost-health",
            observed_at=NOW,
            signer=f.signer,
            evidence_resolver=f.evidence_resolver,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )

    def validate(self, projection: dict[str, Any]) -> None:
        f = self.fixture
        validate_context_health_projection(
            projection,
            source_projection=f.source_projection,
            decision_evidence_projection=f.decision_projection,
            provenance_bundle=f.provenance_bundle,
            trace_events=f.trace_events,
            compaction_records=f.compaction_records,
            accounting_records=f.accounting_records,
            reference_records=f.reference_records,
            harness_runs=f.harness_runs,
            harness_events=f.harness_events,
            recovery_records=f.recovery_records,
            artifact_store=f.artifact_store,
            signer=f.signer,
            evidence_resolver=f.evidence_resolver,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )

    def test_projection_exposes_bound_health_and_failure_drilldowns(self) -> None:
        projection = self.build()
        self.validate(projection)

        self.assertEqual(projection["state_revision"], 9)
        self.assertEqual(
            projection["governance_ref"],
            self.fixture.snapshot["project"]["governance_ref"],
        )
        self.assertEqual(
            projection["source_bindings"]["reference_validity_watermark"],
            REFERENCE_WATERMARK,
        )
        self.assertEqual(
            projection["source_bindings"]["checkpoint_sha256s"],
            [self.fixture.checkpoint_ref.digest],
        )
        self.assertEqual(
            projection["source_bindings"]["harness_event_head_sha256"],
            self.fixture.harness_events[-1]["event_sha256"],
        )
        self.assertEqual(projection["context_health"]["compaction_pair_count"], 1)
        self.assertEqual(
            projection["context_health"]["context_window_status"], "unavailable"
        )
        self.assertEqual(
            projection["context_health"]["provider_token_status"], "unavailable"
        )
        self.assertEqual(
            projection["context_health"]["retrieval_status"], "measured"
        )
        self.assertEqual(
            projection["reference_health"]["stale_assertion_ids"],
            ["assertion-m9-04-reference"],
        )
        self.assertEqual(projection["harness_health"]["failed_run_count"], 1)
        self.assertEqual(projection["replay_health"]["failed_recovery_count"], 1)
        self.assertEqual(projection["overall_status"], "failed")
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
        encoded = json.dumps(projection, sort_keys=True)
        self.assertNotIn("<script>", encoded)
        self.assertNotIn("restart-from-plan", encoded)
        self.assertTrue(
            all(
                ref.startswith("artifact://sha256/")
                for item in projection["drilldowns"]
                for ref in item["artifact_refs"]
            )
        )

    def test_missing_accounting_is_explicitly_unavailable(self) -> None:
        f = self.fixture
        projection = build_context_health_projection(
            source_projection=f.source_projection,
            decision_evidence_projection=f.decision_projection,
            provenance_bundle=f.provenance_bundle,
            trace_events=f.trace_events,
            compaction_records=f.compaction_records,
            accounting_records=[],
            reference_records=f.reference_records,
            harness_runs=f.harness_runs,
            harness_events=f.harness_events,
            recovery_records=f.recovery_records,
            artifact_store=f.artifact_store,
            provider_id="provider-docmost-health",
            observed_at=NOW,
            signer=f.signer,
            evidence_resolver=f.evidence_resolver,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )

        self.assertEqual(
            projection["context_health"]["retrieval_status"], "unavailable"
        )
        self.assertIsNone(projection["context_health"]["retrieval_read_bytes"])
        self.assertIsNone(projection["context_health"]["retrieval_output_bytes"])

    def test_partial_provider_measurement_does_not_publish_partial_totals(
        self,
    ) -> None:
        f = self.fixture
        source = copy.deepcopy(f.accounting_records[0]["receipt"])
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            source["routes"][0]["metrics"][name] = measured_metric(
                10,
                "tokens",
                source_kind="provider_trace",
                evidence_ref=f"trace://provider/{name}",
            )
        receipt = compose_context_accounting(
            accounting_id=source["accounting_id"],
            corpus_id=source["corpus_id"],
            corpus_sha256=source["corpus_sha256"],
            budget_tokens=source["budget_tokens"],
            routes=source["routes"],
            observed_at=source["observed_at"],
        )
        _attach_all_metric_evidence(f, receipt)
        receipt_ref = "artifact://sha256/" + hashlib.sha256(
            _canonical(receipt)
        ).hexdigest()
        records = [
            {
                "trace_event_id": "event-m9-04-8",
                "receipt": receipt,
                "receipt_ref": receipt_ref,
            }
        ]
        events = copy.deepcopy(f.trace_events)
        events[-1]["evidence_refs"] = [receipt_ref]
        events[-1]["event_sha256"] = _digest(
            {
                key: value
                for key, value in events[-1].items()
                if key != "event_sha256"
            }
        )

        projection = self._build_with(
            accounting_records=records, trace_events=events
        )

        self.assertEqual(
            projection["context_health"]["provider_token_status"], "unavailable"
        )
        self.assertIsNone(projection["context_health"]["input_tokens"])
        self.assertIsNone(projection["context_health"]["output_tokens"])

    def test_measured_provider_observability_is_a_passed_slo(self) -> None:
        f = self.fixture
        source = copy.deepcopy(f.accounting_records[0]["receipt"])
        for route in source["routes"]:
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                route["metrics"][name] = measured_metric(
                    10,
                    "tokens",
                    source_kind="provider_trace",
                    evidence_ref=f"trace://provider/{route['route_id']}/{name}",
                )
        receipt = compose_context_accounting(
            accounting_id=source["accounting_id"],
            corpus_id=source["corpus_id"],
            corpus_sha256=source["corpus_sha256"],
            budget_tokens=source["budget_tokens"],
            routes=source["routes"],
            observed_at=source["observed_at"],
        )
        _attach_all_metric_evidence(f, receipt)
        receipt_ref = "artifact://sha256/" + hashlib.sha256(
            _canonical(receipt)
        ).hexdigest()
        records = [
            {
                "trace_event_id": "event-m9-04-8",
                "receipt": receipt,
                "receipt_ref": receipt_ref,
            }
        ]
        events = copy.deepcopy(f.trace_events)
        events[-1]["evidence_refs"] = [receipt_ref]
        events[-1]["event_sha256"] = _digest(
            {
                key: value
                for key, value in events[-1].items()
                if key != "event_sha256"
            }
        )

        projection = self._build_with(
            accounting_records=records, trace_events=events
        )
        slo = next(
            item
            for item in projection["context_health"]["slo_results"]
            if item["slo_id"] == "provider-token-observability"
        )

        self.assertEqual(projection["context_health"]["provider_token_status"], "measured")
        self.assertEqual(slo["status"], "passed")

    def test_measured_provider_metric_requires_resolvable_evidence(self) -> None:
        f = self.fixture
        source = copy.deepcopy(f.accounting_records[0]["receipt"])
        for route in source["routes"]:
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                route["metrics"][name] = measured_metric(
                    10,
                    "tokens",
                    source_kind="provider_trace",
                    evidence_ref=f"trace://missing/{route['route_id']}/{name}",
                )
        receipt = compose_context_accounting(
            accounting_id=source["accounting_id"],
            corpus_id=source["corpus_id"],
            corpus_sha256=source["corpus_sha256"],
            budget_tokens=source["budget_tokens"],
            routes=source["routes"],
            observed_at=source["observed_at"],
        )
        receipt_ref = "artifact://sha256/" + hashlib.sha256(
            _canonical(receipt)
        ).hexdigest()
        records = [
            {
                "trace_event_id": "event-m9-04-8",
                "receipt": receipt,
                "receipt_ref": receipt_ref,
            }
        ]
        events = copy.deepcopy(f.trace_events)
        events[-1]["evidence_refs"] = [receipt_ref]
        events[-1]["event_sha256"] = _digest(
            {
                key: value
                for key, value in events[-1].items()
                if key != "event_sha256"
            }
        )

        with self.assertRaisesRegex(
            ContextHealthProjectionError, "metric evidence"
        ):
            self._build_with(accounting_records=records, trace_events=events)

    def test_measured_provider_metric_rejects_unbound_trace_bytes(self) -> None:
        f = self.fixture
        source = copy.deepcopy(f.accounting_records[0]["receipt"])
        for route in source["routes"]:
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                evidence_ref = f"trace://provider/{route['route_id']}/{name}"
                route["metrics"][name] = measured_metric(
                    999_999_999,
                    "tokens",
                    source_kind="provider_trace",
                    evidence_ref=evidence_ref,
                )
                f.artifacts[evidence_ref] = b"provider-trace"
        receipt = compose_context_accounting(
            accounting_id=source["accounting_id"],
            corpus_id=source["corpus_id"],
            corpus_sha256=source["corpus_sha256"],
            budget_tokens=source["budget_tokens"],
            routes=source["routes"],
            observed_at=source["observed_at"],
        )
        receipt_ref = "artifact://sha256/" + hashlib.sha256(
            _canonical(receipt)
        ).hexdigest()
        records = [
            {
                "trace_event_id": "event-m9-04-8",
                "receipt": receipt,
                "receipt_ref": receipt_ref,
            }
        ]
        events = copy.deepcopy(f.trace_events)
        events[-1]["evidence_refs"] = [receipt_ref]
        events[-1]["event_sha256"] = _digest(
            {
                key: value
                for key, value in events[-1].items()
                if key != "event_sha256"
            }
        )

        with self.assertRaisesRegex(
            ContextHealthProjectionError, "metric evidence binding"
        ):
            self._build_with(accounting_records=records, trace_events=events)

    def test_metric_evidence_binds_accounting_and_corpus_digests(self) -> None:
        receipt = self.fixture.accounting_records[0]["receipt"]
        metric = receipt["routes"][0]["metrics"]["retrieval_read_bytes"]
        evidence = json.loads(self.fixture.artifacts[metric["evidence_ref"]])

        self.assertEqual(evidence["accounting_sha256"], receipt["accounting_sha256"])
        self.assertEqual(evidence["corpus_sha256"], receipt["corpus_sha256"])

    def test_metric_evidence_runtime_rejects_schema_unknown_values(self) -> None:
        base = {
            "accounting_id": "accounting-test",
            "accounting_sha256": "a" * 64,
            "corpus_id": "corpus-test",
            "corpus_sha256": "b" * 64,
            "route_id": "route-test",
            "provider_id": "provider-test",
            "trace_event_id": "event-test",
            "run_id": "run-test",
            "observed_at": NOW,
        }
        cases = (
            {"not_in_schema": {"value": 1, "unit": "count", "source_kind": "retrieval_receipt"}},
            {"retrieval_queries": {"value": 1, "unit": "nonsense", "source_kind": "retrieval_receipt"}},
            {"retrieval_queries": {"value": 1, "unit": "count", "source_kind": "forged"}},
        )
        for metrics in cases:
            with self.subTest(metrics=metrics), self.assertRaises(MetricEvidenceError):
                compose_metric_evidence(**base, metrics=metrics)

    def test_rebuild_rejects_coordinated_reseal(self) -> None:
        projection = self.build()
        projection["harness_health"]["failed_run_count"] = 0
        projection["harness_health"]["status"] = "passed"
        body = {
            key: value
            for key, value in projection.items()
            if key not in {"projection_sha256", "signature"}
        }
        projection["projection_sha256"] = _digest(body)
        projection["signature"] = self.fixture.signer.sign(projection)

        with self.assertRaises(ContextHealthProjectionError):
            self.validate(projection)

    def test_harness_run_rejects_stale_claim_epoch(self) -> None:
        f = self.fixture
        runs = copy.deepcopy(f.harness_runs)
        runs[0]["claim_lease_epoch"] += 1
        runs[0]["claim_fence"] += 1
        _rehash_harness_run(runs[0])

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(harness_runs=runs)

    def test_harness_run_rejects_wrong_work_revision(self) -> None:
        f = self.fixture
        runs = copy.deepcopy(f.harness_runs)
        runs[0]["task_revision"] += 1
        _rehash_harness_run(runs[0])

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(harness_runs=runs)

    def test_harness_terminal_event_binds_final_run_digest(self) -> None:
        runs = copy.deepcopy(self.fixture.harness_runs)
        runs[0]["provider"] = "claude"
        _rehash_harness_run(runs[0])

        with self.assertRaisesRegex(ContextHealthProjectionError, "terminal digest"):
            self._build_with(harness_runs=runs)

    def test_harness_run_rejects_claim_expired_at_projection_time(self) -> None:
        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(observed_at="2026-08-19T00:00:00+08:00")

    def test_unbound_accounting_receipt_is_rejected(self) -> None:
        records = copy.deepcopy(self.fixture.accounting_records)
        records[0]["receipt_ref"] = "artifact://sha256/" + "0" * 64

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(accounting_records=records)

    def test_reference_assertion_must_exist_in_validated_provenance(self) -> None:
        f = self.fixture
        original = f.reference_records[0]
        watch = copy.deepcopy(original["watch"])
        watch["assertion_ids"] = ["assertion-m9-04-forged"]
        trusted_time = {
            "kind": "fixture-clock",
            "trusted_at": "2026-08-17T12:00:00Z",
            "evidence": b"fixture-clock:2026-08-17T12:00:00Z",
        }
        observation = observe_reference(
            watch=watch,
            mode="fixture",
            availability_status="available",
            observed_revision="revision-2",
            observed_content=CHANGED,
            unavailability_evidence=None,
            trusted_time=trusted_time,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        decision = decide_reference_watch(
            observation,
            expected_watch=watch,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        records = [
            {
                "reference_validity_watermark": REFERENCE_WATERMARK,
                "watch": watch,
                "observation": observation,
                "decision": decision,
                "replacement_assertion": None,
            }
        ]

        with self.assertRaisesRegex(
            ContextHealthProjectionError, "validated provenance"
        ):
            self._build_with(reference_records=records)

    def test_reference_watch_must_bind_an_assertion_evidence_source(self) -> None:
        f = self.fixture
        watch = copy.deepcopy(f.reference_records[0]["watch"])
        watch["source_ref"] = "https://example.invalid/different-source"
        trusted_time = {
            "kind": "fixture-clock",
            "trusted_at": "2026-08-17T12:00:00Z",
            "evidence": b"fixture-clock:2026-08-17T12:00:00Z",
        }
        observation = observe_reference(
            watch=watch,
            mode="fixture",
            availability_status="available",
            observed_revision="revision-2",
            observed_content=CHANGED,
            unavailability_evidence=None,
            trusted_time=trusted_time,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        decision = decide_reference_watch(
            observation,
            expected_watch=watch,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        records = [
            {
                "reference_validity_watermark": REFERENCE_WATERMARK,
                "watch": watch,
                "observation": observation,
                "decision": decision,
                "replacement_assertion": None,
            }
        ]

        with self.assertRaisesRegex(ContextHealthProjectionError, "source binding"):
            self._build_with(reference_records=records)

    def test_reference_assertion_cannot_be_current_and_stale(self) -> None:
        f = self.fixture
        watch = copy.deepcopy(f.reference_records[0]["watch"])
        watch["watch_id"] = "watch-m9-04-specification-current"
        trusted_time = {
            "kind": "fixture-clock",
            "trusted_at": "2026-08-17T12:00:00Z",
            "evidence": b"fixture-clock:2026-08-17T12:00:00Z",
        }
        observation = observe_reference(
            watch=watch,
            mode="fixture",
            availability_status="available",
            observed_revision="revision-1",
            observed_content=BASELINE,
            unavailability_evidence=None,
            trusted_time=trusted_time,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        f.artifacts[observation["current_evidence_ref"]] = BASELINE
        decision = decide_reference_watch(
            observation,
            expected_watch=watch,
            artifact_resolver=f.artifact_resolver,
            trusted_time_verifier=f.trusted_time_verifier,
        )
        records = [
            *f.reference_records,
            {
                "reference_validity_watermark": REFERENCE_WATERMARK,
                "watch": watch,
                "observation": observation,
                "decision": decision,
                "replacement_assertion": None,
            },
        ]

        with self.assertRaisesRegex(ContextHealthProjectionError, "conflicting"):
            self._build_with(reference_records=records)

    def test_tampered_postcompact_canary_is_rejected(self) -> None:
        records = copy.deepcopy(self.fixture.compaction_records)
        records[0]["canary_receipt"]["canary_sha256"] = "0" * 64

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(compaction_records=records)

    def test_acknowledged_input_replay_is_visible_as_failed_recovery(self) -> None:
        records = copy.deepcopy(self.fixture.recovery_records)
        records[0]["response_input_id"] = "input-m9-04-answered"

        projection = self._build_with(recovery_records=records)

        self.assertEqual(projection["replay_health"]["failed_recovery_count"], 2)
        self.assertEqual(projection["replay_health"]["status"], "failed")
        self.assertNotIn("input-m9-04-answered", json.dumps(projection))

    def test_recovery_effect_watermark_drift_is_rejected(self) -> None:
        records = copy.deepcopy(self.fixture.recovery_records)
        records[0]["effect_high_watermark"] += 1

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(recovery_records=records)

    def test_unbound_harness_lifecycle_event_is_rejected(self) -> None:
        events = copy.deepcopy(self.fixture.harness_events)
        _append_harness_event(
            events,
            event_type="worker-loss",
            payload={"run_id": "run-m9-04-unknown", "authority_changed": False},
        )

        with self.assertRaises(ContextHealthProjectionError):
            self._build_with(harness_events=events)

    def test_future_valid_trace_chain_fails_closed(self) -> None:
        events = copy.deepcopy(self.fixture.trace_events)
        events[-1]["observed_at"] = "2026-08-18T22:00:00+08:00"
        body = {
            key: value for key, value in events[-1].items() if key != "event_sha256"
        }
        events[-1]["event_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
        f = self.fixture
        with self.assertRaises(ContextHealthProjectionError):
            build_context_health_projection(
                source_projection=f.source_projection,
                decision_evidence_projection=f.decision_projection,
                provenance_bundle=f.provenance_bundle,
                trace_events=events,
                compaction_records=f.compaction_records,
                accounting_records=f.accounting_records,
                reference_records=f.reference_records,
                harness_runs=f.harness_runs,
                harness_events=f.harness_events,
                recovery_records=f.recovery_records,
                artifact_store=f.artifact_store,
                provider_id="provider-docmost-health",
                observed_at=NOW,
                signer=f.signer,
                evidence_resolver=f.evidence_resolver,
                artifact_resolver=f.artifact_resolver,
                trusted_time_verifier=f.trusted_time_verifier,
            )

    def test_projection_time_uses_absolute_rfc3339_order(self) -> None:
        projection = self._build_with(observed_at="2026-08-17T14:30:00Z")

        self.assertEqual(projection["observed_at"], "2026-08-17T14:30:00Z")

    def test_recovery_read_budget_failure_is_not_a_first_action_mismatch(self) -> None:
        records = [copy.deepcopy(self.fixture.recovery_records[0])]
        records[0]["recovery_budget_bytes"] = 1

        projection = self._build_with(recovery_records=records)
        recovery = projection["replay_health"]["recoveries"][0]

        self.assertEqual(recovery["status"], "failed")
        self.assertEqual(recovery["failure_kind"], "read-budget")
        self.assertIsNone(recovery["first_action_match"])
        self.assertEqual(
            projection["replay_health"]["first_action_mismatch_count"], 0
        )

    def test_recovery_read_integrity_failure_is_a_recovery_contract(self) -> None:
        records = [copy.deepcopy(self.fixture.recovery_records[0])]
        records[0]["recovery_reads"].append(
            copy.deepcopy(records[0]["recovery_reads"][0])
        )

        projection = self._build_with(recovery_records=records)
        recovery = projection["replay_health"]["recoveries"][0]

        self.assertEqual(recovery["failure_kind"], "recovery-contract")
        self.assertIsNone(recovery["first_action_match"])
        self.assertEqual(
            projection["replay_health"]["first_action_mismatch_count"], 0
        )

    def _build_with(self, **overrides: Any) -> dict[str, Any]:
        f = self.fixture
        arguments = {
            "source_projection": f.source_projection,
            "decision_evidence_projection": f.decision_projection,
            "provenance_bundle": f.provenance_bundle,
            "trace_events": f.trace_events,
            "compaction_records": f.compaction_records,
            "accounting_records": f.accounting_records,
            "reference_records": f.reference_records,
            "harness_runs": f.harness_runs,
            "harness_events": f.harness_events,
            "recovery_records": f.recovery_records,
            "artifact_store": f.artifact_store,
            "provider_id": "provider-docmost-health",
            "observed_at": NOW,
            "signer": f.signer,
            "evidence_resolver": f.evidence_resolver,
            "artifact_resolver": f.artifact_resolver,
            "trusted_time_verifier": f.trusted_time_verifier,
        }
        arguments.update(overrides)
        return build_context_health_projection(**arguments)

    def test_capacity_preflight_precedes_deep_validation(self) -> None:
        f = self.fixture
        with self.assertRaisesRegex(ContextHealthProjectionError, "capacity"):
            build_context_health_projection(
                source_projection=f.source_projection,
                decision_evidence_projection=f.decision_projection,
                provenance_bundle=f.provenance_bundle,
                trace_events=[{}] * 10_001,
                compaction_records=[],
                accounting_records=[],
                reference_records=[],
                harness_runs=[],
                harness_events=[],
                recovery_records=[],
                artifact_store=f.artifact_store,
                provider_id="provider-docmost-health",
                observed_at=NOW,
                signer=f.signer,
            )

    def test_failure_drilldown_page_is_bounded_to_256_records(self) -> None:
        template = self.fixture.recovery_records[1]
        records = []
        for index in range(257):
            record = copy.deepcopy(template)
            record["recovery_id"] = f"recovery-m9-04-capacity-{index:03d}"
            records.append(record)

        with self.assertRaisesRegex(ContextHealthProjectionError, "drilldown capacity"):
            self._build_with(recovery_records=records)


if __name__ == "__main__":
    unittest.main()
