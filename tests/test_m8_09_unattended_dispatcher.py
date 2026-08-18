"""M8-09 unattended campaign dispatcher contract."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    REQUEST_SCHEMA_VERSION,
    WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
    WORK_COMPLETION_TOOL,
    SharedStateMCPService,
)
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.state_mcp import RequestContext
from context_control_plane.unattended_cursor_store import (
    InMemoryUnattendedCursorStore,
    SQLiteUnattendedCursorStore,
    build_campaign_cursor,
)
from context_control_plane.unattended_dispatcher import (
    UnattendedDispatcher,
    UnattendedDispatcherError,
)
from context_control_plane.unattended_receipt_store import (
    SQLiteUnattendedPortReceiptStore,
)
from context_control_plane.unattended_receipts import (
    UnattendedReceiptError,
    validate_unattended_campaign_receipt,
)
from tests.test_m8_02_shared_state_mcp import _canonical_coordinator_snapshot
from tests.test_m8_09_shared_state_mcp import _CompletionResolver

NOW = "2026-08-17T12:00:00+00:00"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fixture(
    *,
    obligations: list[tuple[str, str, str, str | None]] | None = None,
) -> tuple[dict, dict]:
    source = _canonical_coordinator_snapshot()
    source["project"]["updated_at"] = NOW
    project_id = source["project"]["project_id"]
    template = next(
        item for item in source["works"] if item["work_id"] == "work-active"
    )
    definitions = obligations or [
        ("work-a", "required", "autonomous", None),
        ("work-b", "required", "autonomous", None),
        ("work-c", "required", "autonomous", None),
        ("work-optional", "optional", "manual", None),
    ]
    leaves: list[dict] = []
    for index, (work_id, _mode, _automation, _condition) in enumerate(definitions):
        work = copy.deepcopy(template)
        work.update(
            {
                "work_id": work_id,
                "title": work_id,
                "status": "ready",
                "parent_work_id": "goal",
                "dependency_ids": (
                    [definitions[index - 1][0]]
                    if index > 0 and work_id != "work-optional"
                    else []
                ),
                "owner_refs": ["actor-executor"],
                "scope_refs": [
                    {
                        "scope_kind": "capability",
                        "scope_ref": f"campaign/{work_id}",
                    }
                ],
                "evidence_ids": [],
                "revision": 1,
                "work_identity_sha256": None,
                "work_source_ref": None,
                "source_revision": 0,
            }
        )
        leaves.append(work)
    retained = [
        copy.deepcopy(item)
        for item in source["works"]
        if item["work_id"] in {"campaign", "goal"}
    ]
    source["works"] = retained + leaves
    source["claims"] = []
    source["effects"] = []
    source["project"]["active_work_ids"] = []
    source["project"]["primary_work_id"] = None
    source["project"]["effect_high_watermark"] = 0
    source["evidence"] = [
        {
            "evidence_id": f"evidence-{work_id}",
            "kind": "test",
            "artifact_ref": f"artifact://m8-09/{work_id}",
            "content_sha256": hashlib.sha256(work_id.encode()).hexdigest(),
            "validity": "verified",
            "observed_at": NOW,
            "verified_at": NOW,
        }
        for work_id, _mode, _automation, _condition in definitions
    ]
    work_sources = []
    obligation_records = []
    for work_id, mode, automation, condition_ref in definitions:
        work = next(item for item in leaves if item["work_id"] == work_id)
        work_sources.append(
            {
                "work_source_id": f"source-{work_id}",
                "project_id": project_id,
                "work_id": work_id,
                "source_kind": "state-native",
                "source_ref": f"opaque://state/{work_id}",
                "source_revision": "revision-1",
                "governance_parent_id": "campaign",
                "dependency_ids": list(work["dependency_ids"]),
                "readiness": "ready",
            }
        )
        obligation_records.append(
            {
                "obligation_id": f"obligation-{work_id}",
                "work_id": work_id,
                "mode": mode,
                "condition_ref": condition_ref,
                "authority": {
                    "kind": "project-governance",
                    "ref": source["project"]["governance_ref"],
                },
                "automation_class": automation,
                "verification_profile_ref": "verification://m8-09/default",
                "evidence_refs": [],
                "expires_at": None,
                "status": "pending",
                "revision": 1,
            }
        )
    profile = {
        "schema_version": "context.project-governance-profile/v1alpha1",
        "profile": {
            "profile_id": "profile-m8-09",
            "project_id": project_id,
            "revision": 1,
            "direction_state": "operational",
            "governance_owner_mode": "single-owner",
            "execution_worker_mode": "single-worker",
            "repository_topology": "monolith",
            "requested_runtime_profile": "local-embedded",
            "task_sources": ["state-native"],
            "governance_ref": source["project"]["governance_ref"],
            "updated_at": NOW,
        },
        "charters": [
            {
                "charter_id": "charter-m8-09",
                "project_id": project_id,
                "profile_id": "profile-m8-09",
                "status": "approved",
                "problem_space": "Unattended required Work closure.",
                "intended_users": ["developer"],
                "confirmed_constraint_refs": [],
                "prohibited_side_effects": ["untyped-escalation"],
                "current_evidence_refs": [],
                "unknowns": [],
                "assumptions": [],
                "decision_owner_ref": "actor://owner",
                "discovery_campaign_id": "campaign",
                "attempt_budget": 10,
                "expiry": None,
                "return_point_work_id": definitions[0][0],
                "exit_criteria": ["required-work-closed"],
                "mainline_authority": True,
                "revision": 1,
            }
        ],
        "work_sources": work_sources,
        "obligations": obligation_records,
        "adaptations": [],
    }
    return source, profile


class _Runtime:
    def __init__(
        self,
        state: dict,
        profile: dict,
        *,
        receipt_store: SQLiteUnattendedPortReceiptStore | None = None,
        ledger: WorkLedger | None = None,
    ) -> None:
        self.state = copy.deepcopy(state)
        self.profile = copy.deepcopy(profile)
        self.ledger = ledger or WorkLedger(
            project_id=state["project"]["project_id"],
            project_revision=state["project"]["revision"],
            works=state["works"],
            max_ttl_ms=60_000,
        )
        self.context = RequestContext("actor-executor", "authorization-m8-09")
        self.campaign_run_id = "campaign-run-m8-09"
        self.cursor_store = InMemoryUnattendedCursorStore()
        self.service = SharedStateMCPService(
            self.ledger,
            authorizer=self,
            clock=lambda: NOW,
            completion_evidence_resolver=self,
        )
        self.claimed_work_ids: list[str] = []
        self.executed_work_ids: list[str] = []
        self.completed_work_ids: list[str] = []
        self.composed: dict[str, dict] = {}
        self.executed: dict[str, dict] = {}
        self.verified: dict[str, dict] = {}
        self.condition_decisions: list[dict] = []
        self.blocking_decisions: list[dict] = []
        self.completion_resolvers: list[_CompletionResolver] = []
        self.now = NOW
        self.durable_receipts: dict[tuple[str, str], dict] = {}
        self.receipt_store = receipt_store

    def authorize(self, context, action: str, project_id: str) -> bool:
        return action in {
            "state.claim.acquire",
            "state.claim.release",
            "state.work.complete",
        }

    def trusted_now(self) -> str:
        return self.now

    def resolve_receipt(self, action: str, request_id: str):
        if self.receipt_store is not None:
            stored = self.receipt_store.read(action, request_id)
            if stored is not None:
                return stored
        receipt = self.durable_receipts.get((action, request_id))
        return copy.deepcopy(receipt) if receipt is not None else None

    def _record_receipt(self, action: str, request_id: str, receipt: dict) -> dict:
        if self.receipt_store is not None:
            self.receipt_store.write(action, request_id, receipt)
        self.durable_receipts[(action, request_id)] = copy.deepcopy(receipt)
        return copy.deepcopy(receipt)

    def read(self) -> dict:
        ledger = self.ledger.snapshot()
        state = copy.deepcopy(self.state)
        state["works"] = [
            {key: value for key, value in item.items() if key != "identity_key"}
            for item in ledger["works"]
        ]
        state["claims"] = copy.deepcopy(ledger["claims"])
        state["effects"] = copy.deepcopy(ledger["effects"])
        state["project"]["revision"] = ledger["project_revision"]
        active_ids = [
            item["work_id"] for item in state["works"] if item["status"] == "active"
        ]
        state["project"]["active_work_ids"] = active_ids
        state["project"]["primary_work_id"] = active_ids[0] if active_ids else None
        profile = copy.deepcopy(self.profile)
        status_by_work = {item["work_id"]: item["status"] for item in state["works"]}
        for source in profile["work_sources"]:
            source["readiness"] = status_by_work[source["work_id"]]
        return {
            "state": state,
            "governance_profile": profile,
            "condition_decisions": copy.deepcopy(self.condition_decisions),
            "blocking_decisions": copy.deepcopy(self.blocking_decisions),
        }

    def claim(self, intent: dict) -> dict:
        if intent["work_id"] not in self.claimed_work_ids:
            self.claimed_work_ids.append(intent["work_id"])
        response = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": intent["request_id"],
                "project_id": intent["project_id"],
                "action": "acquire",
                "expected_project_revision": intent["expected_project_revision"],
                "work_id": intent["work_id"],
                "claim_id": intent["claim_id"],
                "requested_ttl_ms": 30_000,
                "scope_owners": intent["scope_owners"],
            },
            context=self.context,
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return self._record_receipt("claim", intent["request_id"], response["result"])

    def compose(self, intent: dict) -> dict:
        request_id = intent["request_id"]
        if request_id not in self.composed:
            body = {
                "status": "accepted",
                "project_id": intent["project_id"],
                "project_revision": intent["project_revision"],
                "work_id": intent["work_id"],
                "work_revision": intent["work_revision"],
                "claim_id": intent["claim_id"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
            }
            self.composed[request_id] = {**body, "packet_sha256": _digest(body)}
        return self._record_receipt("compose", request_id, self.composed[request_id])

    def execute(self, intent: dict) -> dict:
        request_id = intent["request_id"]
        if request_id not in self.executed:
            self.executed_work_ids.append(intent["work_id"])
            body = {
                "status": "succeeded",
                "work_id": intent["work_id"],
                "packet_sha256": intent["packet_sha256"],
                "claim_id": intent["claim_id"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
            }
            self.executed[request_id] = {**body, "execution_sha256": _digest(body)}
        return self._record_receipt("execute", request_id, self.executed[request_id])

    def verify(self, intent: dict) -> dict:
        request_id = intent["request_id"]
        if request_id not in self.verified:
            evidence_ids = [f"evidence-{intent['work_id']}"]
            resolver = _CompletionResolver(
                project_id=intent["project_id"],
                work_id=intent["work_id"],
                project_revision=intent["project_revision"],
                claim_id=intent["claim_id"],
                evidence_ids=evidence_ids,
            )
            self.completion_resolvers.append(resolver)
            body = {
                "decision": "satisfied",
                "work_id": intent["work_id"],
                "execution_sha256": intent["execution_sha256"],
                "evidence_ids": evidence_ids,
                "claim_evidence_verdict_sha256": resolver.verdict_sha256,
                "verifier_ref": "actor-independent-verifier",
                "completion_authority": False,
            }
            self.verified[request_id] = {
                **body,
                "verification_decision_sha256": resolver.verification_sha256,
            }
        return self._record_receipt("verify", request_id, self.verified[request_id])

    def resolve_verification_decision(self, digest: str):
        for resolver in self.completion_resolvers:
            result = resolver.resolve_verification_decision(digest)
            if result is not None:
                return result
        return None

    def resolve_claim_evidence_verdict(self, digest: str):
        for resolver in self.completion_resolvers:
            result = resolver.resolve_claim_evidence_verdict(digest)
            if result is not None:
                return result
        return None

    def complete(self, intent: dict) -> dict:
        if intent["work_id"] not in self.completed_work_ids:
            self.completed_work_ids.append(intent["work_id"])
        response = self.service.call_tool(
            WORK_COMPLETION_TOOL,
            {
                "schema_version": WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
                "request_id": intent["request_id"],
                "project_id": intent["project_id"],
                "work_id": intent["work_id"],
                "claim_id": intent["claim_id"],
                "expected_project_revision": intent["expected_project_revision"],
                "expected_work_revision": intent["expected_work_revision"],
                "expected_claim_revision": intent["expected_claim_revision"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
                "evidence_ids": intent["evidence_ids"],
                "verification_decision_sha256": intent[
                    "verification_decision_sha256"
                ],
                "claim_evidence_verdict_sha256": intent[
                    "claim_evidence_verdict_sha256"
                ],
            },
            context=self.context,
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return self._record_receipt(
            "complete", intent["request_id"], response["result"]
        )

    def block_and_release(self, intent: dict) -> dict:
        raise AssertionError(f"unexpected block: {intent}")


class _VerificationFailureRuntime(_Runtime):
    def __init__(self, state: dict, profile: dict) -> None:
        super().__init__(state, profile)
        self.release_count = 0
        self.blocked_records: dict[str, dict] = {}

    def read(self) -> dict:
        bundle = super().read()
        for work_id, record in self.blocked_records.items():
            work = next(
                item for item in bundle["state"]["works"] if item["work_id"] == work_id
            )
            work.update(copy.deepcopy(record["work"]))
            work["blocker_ids"] = [record["blocker"]["blocker_id"]]
            source = next(
                item
                for item in bundle["governance_profile"]["work_sources"]
                if item["work_id"] == work_id
            )
            source["readiness"] = "blocked"
            if record["blocker"]["blocker_id"] not in bundle["state"]["project"][
                "open_blocker_ids"
            ]:
                bundle["state"]["project"]["open_blocker_ids"].append(
                    record["blocker"]["blocker_id"]
                )
                bundle["state"]["blockers"].append(copy.deepcopy(record["blocker"]))
        active_ids = [
            item["work_id"]
            for item in bundle["state"]["works"]
            if item["status"] == "active"
        ]
        bundle["state"]["project"]["active_work_ids"] = active_ids
        bundle["state"]["project"]["primary_work_id"] = (
            active_ids[0] if active_ids else None
        )
        return bundle

    def verify(self, intent: dict) -> dict:
        body = {
            "decision": "failed",
            "work_id": intent["work_id"],
            "execution_sha256": intent["execution_sha256"],
            "evidence_ids": [f"evidence-{intent['work_id']}"],
            "verifier_ref": "actor-independent-verifier",
            "completion_authority": False,
        }
        return self._record_receipt(
            "verify",
            intent["request_id"],
            {**body, "verification_decision_sha256": _digest(body)},
        )

    def block_and_release(self, intent: dict) -> dict:
        response = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": intent["request_id"],
                "project_id": intent["project_id"],
                "action": "release",
                "expected_project_revision": intent["expected_project_revision"],
                "claim_id": intent["claim_id"],
                "expected_claim_revision": intent["expected_claim_revision"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
            },
            context=self.context,
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        self.release_count += 1
        evidence_ids = sorted(intent["verification"]["evidence_ids"])
        body = {
            "status": "blocked",
            "blocker_id": f"blocker-verification-{intent['work_id']}",
            "work_id": intent["work_id"],
            "project_revision": response["result"]["project_revision"],
            "attempt_id": intent["attempt_id"],
            "attempt_no": intent["attempt_no"],
            "attempt_budget": intent["attempt_budget"],
            "evidence_ids": evidence_ids,
            "resume_condition": {
                "kind": "evidence",
                "refs": [f"evidence://retry/{intent['work_id']}"],
            },
            "work": {
                "work_id": intent["work_id"],
                "status": "blocked",
                "revision": intent["expected_work_revision"] + 1,
            },
            "blocker": {
                "blocker_id": f"blocker-verification-{intent['work_id']}",
                "status": "open",
                "reason": "Independent verification failed.",
                "blocked_work_ids": [intent["work_id"]],
                "evidence_ids": evidence_ids,
                "opened_at": NOW,
                "resolved_at": None,
                "supersedes_blocker_id": None,
            },
            "claim": copy.deepcopy(response["result"]["claim"]),
            "verification_decision_sha256": intent["verification"][
                "verification_decision_sha256"
            ],
            "state_write_authority": False,
            "completion_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        }
        result = {**body, "receipt_sha256": _digest(body)}
        self.blocked_records[intent["work_id"]] = copy.deepcopy(result)
        return self._record_receipt("block", intent["request_id"], result)


class _CrashAfterClaimRuntime(_Runtime):
    def __init__(self, state: dict, profile: dict) -> None:
        super().__init__(state, profile)
        self.crash_once = True

    def compose(self, intent: dict) -> dict:
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("injected crash after claim")
        return super().compose(intent)


class _CrashAfterCursorPhaseStore:
    def __init__(self, target_phase: str) -> None:
        self.delegate = InMemoryUnattendedCursorStore()
        self.target_phase = target_phase
        self.crashed = False

    def read(self, campaign_run_id: str):
        return self.delegate.read(campaign_run_id)

    def compare_and_set(self, campaign_run_id: str, **kwargs):
        committed = self.delegate.compare_and_set(campaign_run_id, **kwargs)
        is_recorded_step = (
            committed["phase"] == "selecting" and committed["completed_steps"]
        )
        if (
            not self.crashed
            and committed["phase"] == self.target_phase
            and (self.target_phase != "selecting" or is_recorded_step)
        ):
            self.crashed = True
            raise RuntimeError(f"injected crash after {self.target_phase} cursor")
        return committed


class _LoseBeforeCursorCommitStore:
    def __init__(self, delegate, target_phase: str) -> None:
        self.delegate = delegate
        self.target_phase = target_phase
        self.lost = False

    def read(self, campaign_run_id: str):
        return self.delegate.read(campaign_run_id)

    def compare_and_set(self, campaign_run_id: str, **kwargs):
        if not self.lost and kwargs["cursor"]["phase"] == self.target_phase:
            self.lost = True
            raise RuntimeError(f"injected loss before {self.target_phase} cursor commit")
        return self.delegate.compare_and_set(campaign_run_id, **kwargs)


def _condition_decision(
    runtime: _Runtime,
    *,
    condition_ref: str,
    outcome: str,
    evidence_ids: list[str],
) -> dict:
    bundle = runtime.read()
    project = bundle["state"]["project"]
    profile = bundle["governance_profile"]
    obligation = next(
        item
        for item in profile["obligations"]
        if item["condition_ref"] == condition_ref
    )
    body = {
        "schema_version": "context.condition-decision/v1alpha1",
        "decision_id": f"condition-decision-{obligation['obligation_id']}",
        "condition_ref": condition_ref,
        "project_id": project["project_id"],
        "project_revision": project["revision"],
        "profile_id": profile["profile"]["profile_id"],
        "governance_revision": profile["profile"]["revision"],
        "obligation_id": obligation["obligation_id"],
        "obligation_revision": obligation["revision"],
        "outcome": outcome,
        "evidence_ids": sorted(evidence_ids),
        "observed_at": NOW,
        "state_write_authority": False,
    }
    return {**body, "decision_sha256": _digest(body)}


class M809UnattendedDispatcherTests(unittest.TestCase):
    def test_three_required_leaves_close_while_optional_is_never_executed(self) -> None:
        state, profile = _fixture()
        runtime = _Runtime(state, profile)
        dispatcher = UnattendedDispatcher(runtime, actor_ref="actor-executor")

        receipt = dispatcher.run(max_steps=10)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["completed_work_ids"], ["work-a", "work-b", "work-c"])
        self.assertEqual(runtime.claimed_work_ids, ["work-a", "work-b", "work-c"])
        self.assertEqual(runtime.executed_work_ids, ["work-a", "work-b", "work-c"])
        self.assertEqual(runtime.completed_work_ids, ["work-a", "work-b", "work-c"])
        self.assertNotIn("work-optional", runtime.claimed_work_ids)
        self.assertEqual(receipt["remaining_optional_work_ids"], ["work-optional"])
        self.assertEqual(receipt["campaign_run_id"], "campaign-run-m8-09")
        self.assertEqual(len(receipt["steps"]), 3)
        self.assertEqual(
            [step["work_id"] for step in receipt["steps"]],
            ["work-a", "work-b", "work-c"],
        )
        self.assertTrue(
            all(
                left["project_revision_after"]
                == right["project_revision_before"]
                for left, right in zip(receipt["steps"], receipt["steps"][1:])
            )
        )
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["completion_authority"])
        self.assertEqual(receipt["provider_authority"], 0)
        self.assertEqual(receipt["external_effect_authority"], 0)

    def test_required_manual_work_without_typed_blocker_fails_closed(self) -> None:
        state, profile = _fixture(
            obligations=[("work-manual", "required", "manual", None)]
        )
        runtime = _Runtime(state, profile)

        with self.assertRaisesRegex(
            UnattendedDispatcherError, "typed blocker"
        ):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=3)

        self.assertEqual(runtime.claimed_work_ids, [])

    def test_current_typed_blocker_allows_a_bounded_stop(self) -> None:
        state, profile = _fixture(
            obligations=[("work-manual", "required", "manual", None)]
        )
        work = next(item for item in state["works"] if item["work_id"] == "work-manual")
        blocker = {
            "blocker_id": "blocker-manual-approval",
            "status": "open",
            "reason": "Owner approval is required.",
            "blocked_work_ids": ["work-manual"],
            "evidence_ids": ["evidence-work-manual"],
            "opened_at": NOW,
            "resolved_at": None,
            "supersedes_blocker_id": None,
        }
        state["blockers"].append(blocker)
        state["project"]["open_blocker_ids"].append(blocker["blocker_id"])
        work["blocker_ids"].append(blocker["blocker_id"])
        work["status"] = "blocked"
        runtime = _Runtime(state, profile)
        runtime.blocking_decisions = [
            {
                "schema_version": "context.blocking-decision/v1alpha1",
                "blocking_decision_id": "decision-manual-approval",
                "project_id": state["project"]["project_id"],
                "project_revision": state["project"]["revision"],
                "blocker_id": blocker["blocker_id"],
                "blocker_kind": "irreversible-effect-authorization-required",
                "reason": blocker["reason"],
                "affected_work_ids": ["work-manual"],
                "evidence_ids": ["evidence-work-manual"],
                "affected_scope_refs": copy.deepcopy(work["scope_refs"]),
                "decision_options": [
                    {"option_id": "approve", "summary": "Approve execution."},
                    {"option_id": "reject", "summary": "Reject execution."},
                ],
                "default_option_id": "reject",
                "resume_condition": {
                    "kind": "authorization",
                    "refs": ["authorization://work-manual"],
                },
                "resolution_actor": "user",
                "safe_reversible_default_available": False,
                "state_write_authority": False,
            }
        ]

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=3)

        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blocker_id"], blocker["blocker_id"])
        self.assertEqual(
            receipt["resume_condition"],
            {"kind": "authorization", "refs": ["authorization://work-manual"]},
        )
        self.assertEqual(receipt["evidence_ids"], ["evidence-work-manual"])
        self.assertEqual(runtime.claimed_work_ids, [])

    def test_conditional_work_executes_only_when_current_evidence_is_met(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        runtime = _Runtime(state, profile)
        runtime.condition_decisions = [
            _condition_decision(
                runtime,
                condition_ref=condition_ref,
                outcome="met",
                evidence_ids=["evidence-work-conditional"],
            )
        ]

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=2)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["completed_work_ids"], ["work-conditional"])
        self.assertEqual(runtime.executed_work_ids, ["work-conditional"])
        self.assertEqual(
            receipt["steps"][0]["condition_decision_sha256"],
            runtime.condition_decisions[0]["decision_sha256"],
        )
        self.assertEqual(
            receipt["steps"][0]["condition_evidence_ids"],
            ["evidence-work-conditional"],
        )

    def test_conditional_not_met_is_non_applicable_only_with_current_evidence(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        runtime = _Runtime(state, profile)
        runtime.condition_decisions = [
            _condition_decision(
                runtime,
                condition_ref=condition_ref,
                outcome="not-met",
                evidence_ids=["evidence-work-conditional"],
            )
        ]

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["completed_work_ids"], [])
        self.assertEqual(
            receipt["conditional_not_applicable_work_ids"], ["work-conditional"]
        )
        self.assertEqual(runtime.claimed_work_ids, [])
        self.assertEqual(runtime.executed_work_ids, [])
        self.assertEqual(receipt["condition_decisions"], runtime.condition_decisions)

    def test_condition_decision_from_an_old_governance_revision_is_rejected(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        runtime = _Runtime(state, profile)
        stale = _condition_decision(
            runtime,
            condition_ref=condition_ref,
            outcome="met",
            evidence_ids=["evidence-work-conditional"],
        )
        runtime.profile["profile"]["revision"] += 1
        runtime.condition_decisions = [stale]

        with self.assertRaisesRegex(UnattendedDispatcherError, "typed blocker"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=1)

        self.assertEqual(runtime.claimed_work_ids, [])

    def test_conditional_unknown_without_typed_blocker_fails_closed(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        runtime = _Runtime(state, profile)

        with self.assertRaisesRegex(UnattendedDispatcherError, "typed blocker"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=1)

        self.assertEqual(runtime.claimed_work_ids, [])

    def test_conditional_unknown_stops_only_with_versioned_evidence_blocker(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        work = next(
            item for item in state["works"] if item["work_id"] == "work-conditional"
        )
        blocker = {
            "blocker_id": "blocker-condition-evidence",
            "status": "open",
            "reason": "Current condition evidence is unavailable.",
            "blocked_work_ids": ["work-conditional"],
            "evidence_ids": ["evidence-work-conditional"],
            "opened_at": NOW,
            "resolved_at": None,
            "supersedes_blocker_id": None,
        }
        state["blockers"].append(blocker)
        state["project"]["open_blocker_ids"].append(blocker["blocker_id"])
        work["status"] = "blocked"
        work["blocker_ids"] = [blocker["blocker_id"]]
        runtime = _Runtime(state, profile)
        runtime.blocking_decisions = [
            {
                "schema_version": "context.blocking-decision/v2alpha1",
                "blocking_decision_id": "decision-condition-evidence",
                "project_id": state["project"]["project_id"],
                "project_revision": state["project"]["revision"],
                "blocker_id": blocker["blocker_id"],
                "blocker_kind": "condition-evidence-unavailable",
                "reason": blocker["reason"],
                "affected_work_ids": ["work-conditional"],
                "evidence_ids": ["evidence-work-conditional"],
                "affected_scope_refs": copy.deepcopy(work["scope_refs"]),
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
        ]

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blocker_id"], blocker["blocker_id"])
        self.assertEqual(runtime.claimed_work_ids, [])

    def test_condition_blocker_requires_exact_condition_evidence_resume(self) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        work = next(
            item for item in state["works"] if item["work_id"] == "work-conditional"
        )
        blocker = {
            "blocker_id": "blocker-condition-evidence",
            "status": "open",
            "reason": "Current condition evidence is unavailable.",
            "blocked_work_ids": ["work-conditional"],
            "evidence_ids": ["evidence-work-conditional"],
            "opened_at": NOW,
            "resolved_at": None,
            "supersedes_blocker_id": None,
        }
        state["blockers"].append(blocker)
        state["project"]["open_blocker_ids"].append(blocker["blocker_id"])
        work["status"] = "blocked"
        work["blocker_ids"] = [blocker["blocker_id"]]
        runtime = _Runtime(state, profile)
        runtime.blocking_decisions = [
            {
                "schema_version": "context.blocking-decision/v2alpha1",
                "blocking_decision_id": "decision-condition-evidence",
                "project_id": state["project"]["project_id"],
                "project_revision": state["project"]["revision"],
                "blocker_id": blocker["blocker_id"],
                "blocker_kind": "condition-evidence-unavailable",
                "reason": blocker["reason"],
                "affected_work_ids": ["work-conditional"],
                "evidence_ids": ["evidence-work-conditional"],
                "affected_scope_refs": copy.deepcopy(work["scope_refs"]),
                "decision_options": [],
                "default_option_id": None,
                "resume_condition": {
                    "kind": "new_ready_work",
                    "refs": ["work-unrelated"],
                },
                "resolution_actor": "external_system",
                "safe_reversible_default_available": False,
                "state_write_authority": False,
            }
        ]

        with self.assertRaisesRegex(UnattendedDispatcherError, "typed blocker"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=1)

    def test_verification_failure_releases_claim_without_completion(self) -> None:
        state, profile = _fixture(
            obligations=[("work-failed", "required", "autonomous", None)]
        )
        runtime = _VerificationFailureRuntime(state, profile)

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blocker_id"], "blocker-verification-work-failed")
        self.assertEqual(runtime.completed_work_ids, [])
        self.assertEqual(runtime.release_count, 1)
        active_claims = [
            claim for claim in runtime.ledger.snapshot()["claims"]
            if claim["status"] == "active"
        ]
        self.assertEqual(active_claims, [])
        cursor = runtime.cursor_store.read("campaign-run-m8-09")
        self.assertEqual(cursor["phase"], "blocked")
        self.assertEqual(cursor["selection"]["attempt_no"], 1)
        self.assertEqual(cursor["selection"]["attempt_budget"], 1)

        replay = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)
        self.assertEqual(replay, receipt)
        self.assertEqual(runtime.release_count, 1)

    def test_restart_after_claim_resumes_without_a_second_claim_or_execution(self) -> None:
        state, profile = _fixture(
            obligations=[("work-restart", "required", "autonomous", None)]
        )
        runtime = _CrashAfterClaimRuntime(state, profile)
        dispatcher = UnattendedDispatcher(runtime, actor_ref="actor-executor")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            dispatcher.step()

        cursor = runtime.cursor_store.read("campaign-run-m8-09")
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor["phase"], "claimed")
        self.assertEqual(cursor["next_action"], "compose")

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=2)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(runtime.claimed_work_ids, ["work-restart"])
        self.assertEqual(runtime.executed_work_ids, ["work-restart"])
        self.assertEqual(runtime.completed_work_ids, ["work-restart"])

    def test_expired_claim_is_rejected_using_trusted_runtime_time(self) -> None:
        state, profile = _fixture(
            obligations=[("work-expired", "required", "autonomous", None)]
        )
        runtime = _CrashAfterClaimRuntime(state, profile)

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").step()

        runtime.now = "2026-08-17T12:01:00+00:00"
        with self.assertRaisesRegex(UnattendedDispatcherError, "lease expired"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").step()

        self.assertEqual(runtime.executed_work_ids, [])

    def test_execute_receipt_is_reconciled_after_cursor_commit_loss(self) -> None:
        state, profile = _fixture(
            obligations=[("work-execute-loss", "required", "autonomous", None)]
        )
        runtime = _Runtime(state, profile)
        runtime.cursor_store = _LoseBeforeCursorCommitStore(
            runtime.cursor_store, "executed"
        )

        with self.assertRaisesRegex(RuntimeError, "executed cursor commit"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=1)

        runtime.executed.clear()
        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(runtime.executed_work_ids, ["work-execute-loss"])

    def test_execute_receipt_survives_runtime_rebuild_after_cursor_commit_loss(self) -> None:
        state, profile = _fixture(
            obligations=[("work-process-loss", "required", "autonomous", None)]
        )
        with tempfile.TemporaryDirectory() as directory:
            receipt_store = SQLiteUnattendedPortReceiptStore(
                Path(directory) / "port-receipts.sqlite3"
            )
            cursor_path = Path(directory) / "cursor.sqlite3"
            runtime = _Runtime(state, profile, receipt_store=receipt_store)
            runtime.cursor_store = _LoseBeforeCursorCommitStore(
                SQLiteUnattendedCursorStore(cursor_path), "executed"
            )

            with self.assertRaisesRegex(RuntimeError, "executed cursor commit"):
                UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=1)

            recovered_state = runtime.read()["state"]
            runtime.cursor_store.delegate.close()
            recovered = _Runtime(
                recovered_state,
                profile,
                receipt_store=receipt_store,
                ledger=runtime.ledger,
            )
            recovered.cursor_store = SQLiteUnattendedCursorStore(cursor_path)
            receipt = UnattendedDispatcher(
                recovered, actor_ref="actor-executor"
            ).run(max_steps=1)

            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(runtime.executed_work_ids, ["work-process-loss"])
            self.assertEqual(recovered.executed_work_ids, [])
            self.assertEqual(recovered.completed_work_ids, ["work-process-loss"])
            recovered.cursor_store.close()
            receipt_store.close()

    def test_completion_receipt_is_reconciled_before_mutable_work_binding(self) -> None:
        state, profile = _fixture(
            obligations=[("work-complete-loss", "required", "autonomous", None)]
        )
        runtime = _Runtime(state, profile)
        runtime.cursor_store = _LoseBeforeCursorCommitStore(
            runtime.cursor_store, "completed"
        )

        with self.assertRaisesRegex(RuntimeError, "completed cursor commit"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=1)

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(runtime.completed_work_ids, ["work-complete-loss"])

    def test_block_receipt_is_reconciled_before_released_claim_binding(self) -> None:
        state, profile = _fixture(
            obligations=[("work-block-loss", "required", "autonomous", None)]
        )
        runtime = _VerificationFailureRuntime(state, profile)
        runtime.cursor_store = _LoseBeforeCursorCommitStore(
            runtime.cursor_store, "blocked"
        )

        with self.assertRaisesRegex(RuntimeError, "blocked cursor commit"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=1)

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(runtime.release_count, 1)

    def test_existing_same_actor_claim_is_adopted_without_a_second_claim(self) -> None:
        state, profile = _fixture(
            obligations=[("work-owned", "required", "autonomous", None)]
        )
        runtime = _Runtime(state, profile)
        initial = build_campaign_cursor(
            campaign_run_id=runtime.campaign_run_id,
            project_id=state["project"]["project_id"],
            profile_id=profile["profile"]["profile_id"],
            governance_revision=profile["profile"]["revision"],
            start_project_revision=state["project"]["revision"],
        )
        runtime.cursor_store.compare_and_set(
            runtime.campaign_run_id,
            expected_cursor_revision=None,
            cursor=initial,
        )
        work = next(item for item in state["works"] if item["work_id"] == "work-owned")
        dispatcher = UnattendedDispatcher(runtime, actor_ref="actor-executor")
        selection_id = dispatcher._operation_id(
            "selection",
            runtime.campaign_run_id,
            state["project"]["project_id"],
            state["project"]["revision"],
            profile["profile"]["profile_id"],
            profile["profile"]["revision"],
            "obligation-work-owned",
            1,
            work["work_id"],
            work["revision"],
            1,
        )
        attempt_id = dispatcher._operation_id("attempt", selection_id, 1)
        claim_id = dispatcher._operation_id("claim-record", selection_id, attempt_id)
        runtime.ledger.acquire_claim(
            work_id=work["work_id"],
            actor_ref="actor-executor",
            expected_project_revision=state["project"]["revision"],
            observed_at=NOW,
            requested_ttl_ms=30_000,
            claim_id=claim_id,
            scope_owners=work["scope_refs"],
        )

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(runtime.claimed_work_ids, [])
        self.assertEqual(runtime.executed_work_ids, ["work-owned"])

    def test_same_actor_claim_from_another_campaign_is_not_adopted(self) -> None:
        state, profile = _fixture(
            obligations=[("work-foreign", "required", "autonomous", None)]
        )
        runtime = _Runtime(state, profile)
        initial = build_campaign_cursor(
            campaign_run_id=runtime.campaign_run_id,
            project_id=state["project"]["project_id"],
            profile_id=profile["profile"]["profile_id"],
            governance_revision=profile["profile"]["revision"],
            start_project_revision=state["project"]["revision"],
        )
        runtime.cursor_store.compare_and_set(
            runtime.campaign_run_id,
            expected_cursor_revision=None,
            cursor=initial,
        )
        work = next(item for item in state["works"] if item["work_id"] == "work-foreign")
        runtime.ledger.acquire_claim(
            work_id=work["work_id"],
            actor_ref="actor-executor",
            expected_project_revision=state["project"]["revision"],
            observed_at=NOW,
            requested_ttl_ms=30_000,
            claim_id="claim-other-campaign",
            scope_owners=work["scope_refs"],
        )

        with self.assertRaisesRegex(UnattendedDispatcherError, "campaign"):
            UnattendedDispatcher(runtime, actor_ref="actor-executor").run(max_steps=1)

    def test_restart_uses_the_same_sqlite_cursor_after_process_reopen(self) -> None:
        state, profile = _fixture(
            obligations=[("work-sqlite", "required", "autonomous", None)]
        )
        runtime = _CrashAfterClaimRuntime(state, profile)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispatcher.sqlite3"
            runtime.cursor_store = SQLiteUnattendedCursorStore(path)

            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                UnattendedDispatcher(
                    runtime, actor_ref="actor-executor"
                ).step()

            runtime.cursor_store.close()
            runtime.cursor_store = SQLiteUnattendedCursorStore(path)
            receipt = UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=1)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(runtime.claimed_work_ids, ["work-sqlite"])
        self.assertEqual(runtime.executed_work_ids, ["work-sqlite"])
        self.assertEqual(runtime.completed_work_ids, ["work-sqlite"])

    def test_every_committed_cursor_boundary_restarts_without_duplicate_work(self) -> None:
        for phase in (
            "prepared",
            "claimed",
            "composed",
            "executed",
            "verified",
            "completed",
            "selecting",
        ):
            with self.subTest(phase=phase):
                state, profile = _fixture(
                    obligations=[("work-boundary", "required", "autonomous", None)]
                )
                runtime = _Runtime(state, profile)
                runtime.cursor_store = _CrashAfterCursorPhaseStore(phase)

                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    UnattendedDispatcher(
                        runtime, actor_ref="actor-executor"
                    ).run(max_steps=1)

                receipt = UnattendedDispatcher(
                    runtime, actor_ref="actor-executor"
                ).run(max_steps=1)

                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(runtime.claimed_work_ids, ["work-boundary"])
                self.assertEqual(runtime.executed_work_ids, ["work-boundary"])
                self.assertEqual(runtime.completed_work_ids, ["work-boundary"])

    def test_exactly_three_steps_can_close_a_three_leaf_campaign(self) -> None:
        state, profile = _fixture()
        runtime = _Runtime(state, profile)

        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=3)

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(receipt["steps"]), 3)

    def test_active_claim_without_prepared_cursor_fails_as_recovery_corruption(self) -> None:
        state, profile = _fixture(
            obligations=[("work-orphan", "required", "autonomous", None)]
        )
        runtime = _Runtime(state, profile)
        work = next(item for item in state["works"] if item["work_id"] == "work-orphan")
        runtime.ledger.acquire_claim(
            work_id="work-orphan",
            actor_ref="actor-executor",
            expected_project_revision=state["project"]["revision"],
            observed_at=NOW,
            requested_ttl_ms=30_000,
            claim_id="claim-orphan",
            scope_owners=work["scope_refs"],
        )

        with self.assertRaisesRegex(UnattendedDispatcherError, "prepared cursor"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=2)

        self.assertEqual(runtime.executed_work_ids, [])

    def test_claimed_cursor_rejects_foreign_authoritative_claim_before_execute(self) -> None:
        state, profile = _fixture(
            obligations=[("work-stale", "required", "autonomous", None)]
        )
        runtime = _CrashAfterClaimRuntime(state, profile)

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).step()

        claim_id = runtime.ledger.snapshot()["claims"][0]["claim_id"]
        runtime.ledger._works["work-stale"]["owner_refs"].append("actor-foreign")
        runtime.ledger._claims[claim_id]["actor_ref"] = "actor-foreign"

        with self.assertRaisesRegex(UnattendedDispatcherError, "active claim"):
            UnattendedDispatcher(
                runtime, actor_ref="actor-executor"
            ).run(max_steps=1)

        self.assertEqual(runtime.executed_work_ids, [])
        self.assertEqual(runtime.completed_work_ids, [])

    def test_closed_campaign_replay_is_byte_identical_and_side_effect_free(self) -> None:
        state, profile = _fixture()
        runtime = _Runtime(state, profile)

        first = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=10)
        second = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=10)

        self.assertEqual(second, first)
        self.assertEqual(runtime.claimed_work_ids, ["work-a", "work-b", "work-c"])
        self.assertEqual(runtime.executed_work_ids, ["work-a", "work-b", "work-c"])
        self.assertEqual(runtime.completed_work_ids, ["work-a", "work-b", "work-c"])

    def test_campaign_receipt_rejects_completed_work_membership_forgery(self) -> None:
        runtime = _Runtime(*_fixture())
        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=3)
        forged = copy.deepcopy(receipt)
        forged["completed_work_ids"] = ["work-a"]
        unsigned = {key: value for key, value in forged.items() if key != "receipt_sha256"}
        forged["receipt_sha256"] = _digest(unsigned)

        with self.assertRaisesRegex(UnattendedReceiptError, "step chain"):
            validate_unattended_campaign_receipt(forged)

    def test_campaign_receipt_rejects_noncontiguous_step_revisions(self) -> None:
        runtime = _Runtime(*_fixture())
        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=3)
        forged = copy.deepcopy(receipt)
        forged["steps"][1]["project_revision_before"] += 100
        forged["steps"][1]["project_revision_after"] += 100
        step_unsigned = {
            key: value
            for key, value in forged["steps"][1].items()
            if key != "step_sha256"
        }
        forged["steps"][1]["step_sha256"] = _digest(step_unsigned)
        unsigned = {key: value for key, value in forged.items() if key != "receipt_sha256"}
        forged["receipt_sha256"] = _digest(unsigned)

        with self.assertRaisesRegex(UnattendedReceiptError, "step chain"):
            validate_unattended_campaign_receipt(forged)

    def test_campaign_receipt_requires_not_met_decision_for_each_non_applicable_work(
        self,
    ) -> None:
        condition_ref = "condition://m8-09/eligible"
        state, profile = _fixture(
            obligations=[("work-conditional", "conditional", "autonomous", condition_ref)]
        )
        runtime = _Runtime(state, profile)
        runtime.condition_decisions = [
            _condition_decision(
                runtime,
                condition_ref=condition_ref,
                outcome="not-met",
                evidence_ids=["evidence-work-conditional"],
            )
        ]
        receipt = UnattendedDispatcher(
            runtime, actor_ref="actor-executor"
        ).run(max_steps=1)
        forged = copy.deepcopy(receipt)
        forged["condition_decisions"] = []
        unsigned = {key: value for key, value in forged.items() if key != "receipt_sha256"}
        forged["receipt_sha256"] = _digest(unsigned)

        with self.assertRaisesRegex(UnattendedReceiptError, "condition decisions"):
            validate_unattended_campaign_receipt(forged)


if __name__ == "__main__":
    unittest.main()
