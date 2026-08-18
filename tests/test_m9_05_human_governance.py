"""M9-05 controlled human governance entry contract."""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from context_control_plane.authorization_audit import (
    AUTHORIZATION_POLICY_SCHEMA_VERSION,
    GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION,
    InMemoryAuthorizationAuditStore,
    SQLiteAuthorizationAuditStore,
    TenantProjectAuthorizer,
    authorization_policy_sha256,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_events import (
    IDEA_EVENT_SCHEMA_VERSION_V2,
    build_state_event,
)
from context_control_plane.state_mcp import RequestContext, StateMCPService
from tests import test_m3_05_experiment_lifecycle as m305_fixtures
from tests import test_m3_07_idea_review as m307_fixtures

ACTIONS = (
    "context.experiment.promotion.propose",
    "context.experiment.promotion.approve",
    "context.idea.correction.protect",
    "context.idea.correction.release",
)
AUTHORIZATION_ACTIONS = {
    action: f"state.{action.removeprefix('context.')}" for action in ACTIONS
}


def _audit_event(
    *,
    tool: str,
    arguments: dict,
    context: RequestContext,
    request_sha256: str,
) -> dict:
    event = {
        "schema_version": GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION,
        "event_type": "authorization_decision",
        "event_id": f"authorization-event-{arguments['request_id']}",
        "sequence_no": 1,
        "policy_id": "policy-test",
        "policy_revision": 1,
        "policy_sha256": "a" * 64,
        "authorization_ref": context.authorization_ref,
        "subject_ref": context.subject_ref,
        "subject_tenant_id": "tenant-test",
        "tenant_id": "tenant-test",
        "project_id": arguments["project_id"],
        "action": AUTHORIZATION_ACTIONS[tool],
        "decision": "allow",
        "reason_code": "grant_matched",
        "observed_at": "2026-08-17T08:29:59+08:00",
        "request_id": arguments["request_id"],
        "request_sha256": request_sha256,
        "previous_event_sha256": None,
    }
    event["event_sha256"] = hashlib.sha256(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return event


def _payload(action: str) -> dict:
    common = {"causation_ref": "work:M9-05", "correlation_ref": "campaign:M9"}
    if action == ACTIONS[0]:
        return {
            **common,
            "work_id": "experiment-one",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": "attempt-one",
            "proposal_id": "proposal-one",
            "criterion_evidence": {"criterion-one": ["evidence-one"]},
        }
    if action == ACTIONS[1]:
        return {
            **common,
            "work_id": "experiment-one",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": "proposal-one",
            "approval_id": "approval-one",
        }
    if action == ACTIONS[2]:
        return {
            **common,
            "idea_id": "idea-one",
            "protection_id": "protection-one",
            "affected_work_ids": ["work-one"],
            "affected_scope_refs": ["file:src/main.py"],
            "reason": "Current evidence requires review.",
            "evidence_ids": ["evidence-one"],
        }
    if action == ACTIONS[3]:
        return {
            **common,
            "idea_id": "idea-one",
            "protection_id": "protection-one",
            "release_reason": "Verified evidence resolves the correction.",
            "release_evidence_ids": ["evidence-one"],
        }
    return {}


def _request(action: str, *, request_id: str = "governance-request") -> dict:
    return {
        "schema_version": "context.human-governance-request/v1alpha1",
        "request_id": request_id,
        "project_id": "project-one",
        "expected_revision": 9,
        "action": action,
        "payload": _payload(action),
    }


class _SessionResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def resolve(self, session_id: str, project_id: str, action: str) -> RequestContext | None:
        self.calls.append((session_id, project_id, action))
        if session_id != "session-valid":
            return None
        return RequestContext("actor-human", "authorization-human")


class _StateMCPProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, RequestContext]] = []
        self.malformed = False
        self.wrong_binding = False
        self.missing_audit = False
        self.wrong_audit_binding = False
        self.audit_events: dict[tuple[str, str, str], dict] = {}

    def call_tool(
        self, tool: str, arguments: dict, *, context: RequestContext
    ) -> dict:
        self.calls.append((tool, arguments, context))
        if self.malformed:
            return {"ok": True}
        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "subject_ref": context.subject_ref,
                    "authorization_ref": context.authorization_ref,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.audit_events[(tool, arguments["request_id"], request_sha256)] = (
            _audit_event(
                tool=tool,
                arguments=arguments,
                context=context,
                request_sha256=request_sha256,
            )
        )
        revision = arguments["expected_revision"] + 1
        if tool.startswith("context.experiment.promotion"):
            record_id = (
                arguments["proposal_id"]
                if tool.endswith("propose")
                else arguments["approval_id"]
            )
            if self.wrong_binding:
                record_id = "record-forged"
            transition = {
                "operation": (
                    "promotion-proposed"
                    if tool.endswith("propose")
                    else "promotion-approved"
                ),
                "request_sha256": request_sha256,
                "attempt_id": None,
                "promotion_id": record_id,
                "proposal_id": (
                    record_id if tool.endswith("propose") else arguments["proposal_id"]
                ),
            }
            event = build_state_event(
                event_id=f"event-{arguments['request_id']}",
                event_type="state-transition",
                project_id=arguments["project_id"],
                sequence_no=revision,
                revision_before=arguments["expected_revision"],
                occurred_at="2026-08-17T08:30:00+08:00",
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "experiment_promotions",
                        "object_id": record_id,
                        "value": {"promotion_id": record_id},
                    }
                ],
                project_after={
                    "project_id": arguments["project_id"],
                    "revision": revision,
                },
                experiment_transition=transition,
            )
        else:
            record_id = arguments["protection_id"]
            if self.wrong_binding:
                record_id = "protection-forged"
            event = build_state_event(
                event_id=f"event-{arguments['request_id']}",
                event_type="state-transition",
                project_id=arguments["project_id"],
                sequence_no=revision,
                revision_before=arguments["expected_revision"],
                occurred_at="2026-08-17T08:30:00+08:00",
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "correction_protections",
                        "object_id": record_id,
                        "value": {"protection_id": record_id},
                    }
                ],
                project_after={
                    "project_id": arguments["project_id"],
                    "revision": revision,
                },
                idea_transition={
                    "operation": (
                        "correction-guarded"
                        if tool.endswith("protect")
                        else "correction-released"
                    ),
                    "request_sha256": request_sha256,
                    "canonical_idea_id": arguments["idea_id"],
                    "submitted_idea_id": None,
                    "occurrence_id": None,
                    "review_id": None,
                    "protection_id": record_id,
                },
                schema_version=IDEA_EVENT_SCHEMA_VERSION_V2,
            )
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": tool,
            "ok": True,
            "result": {
                "revision": revision,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
            },
            "error": None,
        }

    def authorization_receipt(
        self, tool: str, arguments: dict, *, context: RequestContext
    ) -> dict | None:
        if self.missing_audit:
            return None
        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "subject_ref": context.subject_ref,
                    "authorization_ref": context.authorization_ref,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event = copy.deepcopy(
            self.audit_events.get((tool, arguments["request_id"], request_sha256))
        )
        if event is not None and self.wrong_audit_binding:
            event["request_id"] = "request-forged"
            event["event_sha256"] = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in event.items() if key != "event_sha256"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        return event


class _BarrierFingerprintMap(dict):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def get(self, key, default=None):
        value = super().get(key, default)
        try:
            self._barrier.wait(timeout=0.1)
        except threading.BrokenBarrierError:
            pass
        return value


class _MappedSessionResolver:
    def __init__(self, sessions: dict[str, RequestContext]) -> None:
        self._sessions = sessions

    def resolve(
        self, session_id: str, project_id: str, action: str
    ) -> RequestContext | None:
        return self._sessions.get(session_id)


class _CountingSQLiteStateStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.project_reads = 0
        self.event_reads = 0

    def read_project(self, project_id: str) -> dict:
        self.project_reads += 1
        return super().read_project(project_id)

    def read_events(self, project_id: str) -> list[dict]:
        self.event_reads += 1
        return super().read_events(project_id)


class _FailingAuditStore:
    def append(self, event: dict) -> dict:
        raise OSError("audit storage unavailable")


class _AllowAuthorizer:
    def authorize(
        self, context: RequestContext, action: str, project_id: str
    ) -> bool:
        return True


def _integration_policy(project_id: str) -> dict:
    owner_actions = [
        "state.experiment.attempt.begin",
        "state.experiment.promotion.propose",
        "state.experiment.promotion.approve",
        "state.idea.correction.protect",
        "state.idea.correction.release",
    ]
    verifier_actions = [
        "state.experiment.promotion.approve",
        "state.idea.correction.release",
    ]
    return {
        "schema_version": AUTHORIZATION_POLICY_SCHEMA_VERSION,
        "policy_id": "policy-m9-05-integration",
        "policy_revision": 1,
        "issued_at": "2026-08-14T07:00:00+08:00",
        "expires_at": None,
        "projects": [
            {"tenant_id": "tenant-a", "project_id": project_id, "status": "active"},
            {
                "tenant_id": "tenant-b",
                "project_id": "project-other",
                "status": "active",
            },
        ],
        "grants": [
            {
                "grant_id": "grant-owner",
                "authorization_ref": "authorization://tenant-a/owner",
                "subject_ref": "actor-owner",
                "tenant_id": "tenant-a",
                "project_ids": [project_id],
                "actions": owner_actions,
                "not_before": "2026-08-14T07:00:00+08:00",
                "expires_at": None,
                "status": "active",
            },
            {
                "grant_id": "grant-verifier",
                "authorization_ref": "authorization://tenant-a/verifier",
                "subject_ref": "actor-verifier",
                "tenant_id": "tenant-a",
                "project_ids": [project_id],
                "actions": verifier_actions,
                "not_before": "2026-08-14T07:00:00+08:00",
                "expires_at": None,
                "status": "active",
            },
            {
                "grant_id": "grant-cross-tenant",
                "authorization_ref": "authorization://tenant-b/cross",
                "subject_ref": "actor-cross",
                "tenant_id": "tenant-b",
                "project_ids": ["project-other"],
                "actions": ["state.experiment.promotion.propose"],
                "not_before": "2026-08-14T07:00:00+08:00",
                "expires_at": None,
                "status": "active",
            },
        ],
    }


def _authorizer(project_id: str, audit_store) -> TenantProjectAuthorizer:
    policy = _integration_policy(project_id)
    return TenantProjectAuthorizer(
        policy,
        expected_policy_sha256=authorization_policy_sha256(policy),
        audit_store=audit_store,
        clock=lambda: "2026-08-14T08:35:00+08:00",
    )


def _service(
    store: SQLiteStateStore,
    *,
    project_id: str,
    audit_store,
    now: str = "2026-08-14T08:35:00+08:00",
) -> StateMCPService:
    return StateMCPService(
        store,
        authorizer=_authorizer(project_id, audit_store),
        registry_digest="a" * 64,
        clock=lambda: now,
        event_id_factory=lambda request_id: f"event-{request_id}",
    )


def _facade(service: StateMCPService):
    module = importlib.import_module("context_control_plane.human_governance")
    resolver = _MappedSessionResolver(
        {
            "session-owner": RequestContext(
                "actor-owner", "authorization://tenant-a/owner"
            ),
            "session-verifier": RequestContext(
                "actor-verifier", "authorization://tenant-a/verifier"
            ),
            "session-cross-tenant": RequestContext(
                "actor-cross", "authorization://tenant-b/cross"
            ),
        }
    )
    return module.HumanGovernanceFacade(service, session_resolver=resolver)


def _human_request(
    *,
    action: str,
    request_id: str,
    project_id: str,
    expected_revision: int,
    payload: dict,
) -> dict:
    return {
        "schema_version": "context.human-governance-request/v1alpha1",
        "request_id": request_id,
        "project_id": project_id,
        "expected_revision": expected_revision,
        "action": action,
        "payload": payload,
    }


def _promotion_payload(
    *,
    proposal_id: str,
    attempt_id: str | None = None,
    approval_id: str | None = None,
    criterion_evidence: dict | None = None,
) -> dict:
    common = {
        "work_id": "experiment-throughput",
        "expected_work_revision": 3,
        "expected_target_work_revision": 2,
        "proposal_id": proposal_id,
        "causation_ref": "work:M9-05",
        "correlation_ref": "campaign:M9",
    }
    if attempt_id is not None:
        return {
            **common,
            "attempt_id": attempt_id,
            "criterion_evidence": criterion_evidence
            or {
                "throughput target": ["evidence-throughput"],
                "recovery target": ["evidence-recovery"],
            },
        }
    return {**common, "approval_id": approval_id}


def _record_attempt(
    service: StateMCPService, *, project_id: str, attempt_id: str
) -> dict:
    return service.call_tool(
        "context.experiment.attempt",
        {
            "schema_version": "context.experiment-attempt-request/v1alpha1",
            "request_id": attempt_id,
            "project_id": project_id,
            "expected_revision": 9,
            "attempt_id": attempt_id,
            "work_id": "experiment-throughput",
            "claim_id": "claim-experiment",
            "causation_ref": "work:M9-05",
            "correlation_ref": "campaign:M9",
        },
        context=RequestContext("actor-owner", "authorization://tenant-a/owner"),
    )


class M905HumanGovernanceTests(unittest.TestCase):
    def test_public_contract_exposes_only_four_controlled_actions(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")

        self.assertEqual(
            module.HUMAN_GOVERNANCE_ACTIONS,
            frozenset(
                {
                    "context.experiment.promotion.propose",
                    "context.experiment.promotion.approve",
                    "context.idea.correction.protect",
                    "context.idea.correction.release",
                }
            ),
        )

    def test_submit_maps_only_controlled_actions_without_caller_request_context(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)

        self.assertNotIn("context", inspect.signature(facade.submit).parameters)
        for index, action in enumerate(ACTIONS):
            request = _request(action, request_id=f"request-{index}")
            response = facade.submit(request, session_id="session-valid")

            self.assertTrue(response["ok"], response["error"])
            tool, state_request, context = state.calls[index]
            self.assertEqual(tool, action)
            self.assertEqual(context.subject_ref, "actor-human")
            self.assertEqual(state_request["request_id"], request["request_id"])
            self.assertEqual(state_request["project_id"], request["project_id"])
            self.assertEqual(
                state_request["expected_revision"], request["expected_revision"]
            )
            self.assertNotIn("session_id", state_request)
            self.assertNotIn("authorization_ref", state_request)

    def test_invalid_or_generic_request_is_rejected_before_session_or_state(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)
        cases = []
        generic = _request("context.state.commit")
        generic["payload"] = {"changes": []}
        cases.append(generic)
        extra = _request(ACTIONS[0])
        extra["authorization_ref"] = "caller-forged"
        cases.append(extra)
        missing = _request(ACTIONS[0])
        del missing["expected_revision"]
        cases.append(missing)

        for request in cases:
            with self.subTest(request=request):
                response = facade.submit(request, session_id="session-valid")
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_request")

        self.assertEqual(resolver.calls, [])
        self.assertEqual(state.calls, [])

    def test_unknown_session_is_denied_before_state_lookup(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)

        response = facade.submit(_request(ACTIONS[0]), session_id="session-unknown")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "permission_denied")
        self.assertEqual(state.calls, [])

    def test_duplicate_request_replays_but_payload_drift_conflicts(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)
        request = _request(ACTIONS[0])

        first = facade.submit(request, session_id="session-valid")
        replay = facade.submit(request, session_id="session-valid")
        drifted = _request(ACTIONS[0])
        drifted["payload"]["proposal_id"] = "proposal-drifted"
        conflict = facade.submit(drifted, session_id="session-valid")

        self.assertEqual(replay, first)
        self.assertEqual(len(state.calls), 1)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "conflict")

    def test_facade_request_cache_is_bounded_and_eviction_reexecutes_exactly(
        self,
    ) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(
            state, session_resolver=_SessionResolver()
        )
        facade._request_cache_limit = 2
        requests = [
            _request(ACTIONS[0], request_id=f"bounded-cache-{index}")
            for index in range(3)
        ]

        receipts = [
            facade.submit(request, session_id="session-valid")
            for request in requests
        ]
        replay = facade.submit(requests[0], session_id="session-valid")

        self.assertTrue(all(receipt["ok"] for receipt in receipts), receipts)
        self.assertEqual(replay, receipts[0])
        self.assertEqual(len(state.calls), 4)
        self.assertLessEqual(len(facade._request_fingerprints), 2)
        self.assertLessEqual(len(facade._receipts), 2)

    def test_malformed_state_response_fails_closed(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        state.malformed = True
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)

        response = facade.submit(_request(ACTIONS[0]), session_id="session-valid")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "unavailable")

    def test_unhashable_public_input_returns_invalid_without_resolving_session(
        self,
    ) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)
        invalid_action = _request(ACTIONS[0])
        invalid_action["action"] = []
        invalid_list_item = _request(ACTIONS[3])
        invalid_list_item["payload"]["release_evidence_ids"] = [[]]

        for request in (invalid_action, invalid_list_item):
            with self.subTest(request=request):
                response = facade.submit(request, session_id="session-valid")
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_request")

        self.assertEqual(resolver.calls, [])
        self.assertEqual(state.calls, [])

    def test_state_event_must_bind_the_requested_governance_record(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        state.wrong_binding = True
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)

        response = facade.submit(_request(ACTIONS[0]), session_id="session-valid")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "unavailable")

    def test_missing_or_forged_authorization_audit_binding_fails_closed(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        for attribute in ("missing_audit", "wrong_audit_binding"):
            with self.subTest(attribute=attribute):
                state = _StateMCPProbe()
                setattr(state, attribute, True)
                response = module.HumanGovernanceFacade(
                    state, session_resolver=_SessionResolver()
                ).submit(_request(ACTIONS[0]), session_id="session-valid")

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "unavailable")

    def test_concurrent_request_id_drift_admits_exactly_one_state_write(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)
        facade._request_fingerprints = _BarrierFingerprintMap(
            threading.Barrier(2)
        )
        first = _request(ACTIONS[0])
        second = _request(ACTIONS[0])
        second["payload"]["proposal_id"] = "proposal-concurrent-drift"

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda request: facade.submit(
                        request, session_id="session-valid"
                    ),
                    (first, second),
                )
            )

        self.assertEqual(sum(response["ok"] for response in responses), 1)
        self.assertEqual(
            [
                response["error"]["code"]
                for response in responses
                if not response["ok"]
            ],
            ["conflict"],
        )
        self.assertEqual(len(state.calls), 1)

    def test_same_request_id_is_scoped_by_project_and_action(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)
        first = _request(ACTIONS[0])
        second = _request(ACTIONS[0])
        second["project_id"] = "project-two"

        first_response = facade.submit(first, session_id="session-valid")
        second_response = facade.submit(second, session_id="session-valid")

        self.assertTrue(first_response["ok"], first_response["error"])
        self.assertTrue(second_response["ok"], second_response["error"])
        self.assertEqual(len(state.calls), 2)

    def test_maximum_request_id_can_return_a_committed_event_receipt(self) -> None:
        module = importlib.import_module("context_control_plane.human_governance")
        resolver = _SessionResolver()
        state = _StateMCPProbe()
        facade = module.HumanGovernanceFacade(state, session_resolver=resolver)

        response = facade.submit(
            _request(ACTIONS[0], request_id="r" * 200),
            session_id="session-valid",
        )

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(len(response["result"]["event_id"]), 206)


class M905HumanGovernanceIntegrationTests(unittest.TestCase):
    @staticmethod
    def _experiment_snapshot() -> dict:
        snapshot = m305_fixtures.M305ExperimentLifecycleTests.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://verification/{evidence_id}",
                "content_sha256": digest * 64,
                "validity": "verified",
                "observed_at": "2026-08-14T08:31:00+08:00",
                "verified_at": "2026-08-14T08:32:00+08:00",
            }
            for evidence_id, digest in (
                ("evidence-throughput", "a"),
                ("evidence-recovery", "b"),
            )
        ]
        return snapshot

    def test_real_promotion_requires_independent_approval_and_audits_each_action(
        self,
    ) -> None:
        snapshot = self._experiment_snapshot()
        project_id = snapshot["project"]["project_id"]
        audit = InMemoryAuthorizationAuditStore()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = _service(store, project_id=project_id, audit_store=audit)
            facade = _facade(service)

            attempt = _record_attempt(
                service, project_id=project_id, attempt_id="attempt-m9-05"
            )
            proposal = facade.submit(
                _human_request(
                    action=ACTIONS[0],
                    request_id="proposal-m9-05",
                    project_id=project_id,
                    expected_revision=10,
                    payload=_promotion_payload(
                        proposal_id="proposal-m9-05", attempt_id="attempt-m9-05"
                    ),
                ),
                session_id="session-owner",
            )
            self_approval = facade.submit(
                _human_request(
                    action=ACTIONS[1],
                    request_id="approval-self-m9-05",
                    project_id=project_id,
                    expected_revision=11,
                    payload=_promotion_payload(
                        proposal_id="proposal-m9-05",
                        approval_id="approval-self-m9-05",
                    ),
                ),
                session_id="session-owner",
            )
            approval = facade.submit(
                _human_request(
                    action=ACTIONS[1],
                    request_id="approval-m9-05",
                    project_id=project_id,
                    expected_revision=11,
                    payload=_promotion_payload(
                        proposal_id="proposal-m9-05",
                        approval_id="approval-m9-05",
                    ),
                ),
                session_id="session-verifier",
            )
            stored = store.read_project(project_id)
            events = store.read_events(project_id)

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertFalse(self_approval["ok"])
        self.assertEqual(self_approval["error"]["code"], "integrity")
        self.assertTrue(approval["ok"], approval["error"])
        self.assertEqual(stored["project"]["revision"], 12)
        self.assertEqual(
            [record["kind"] for record in stored["experiment_promotions"]],
            ["proposed", "approved"],
        )
        self.assertEqual(len(events), 3)
        audit_events = audit.read_events(tenant_id="tenant-a", project_id=project_id)
        self.assertEqual(len(audit_events), 4)
        self.assertEqual(
            [event["decision"] for event in audit_events],
            ["allow", "allow", "allow", "allow"],
        )
        approval_audit = approval["result"]["authorization_audit"]
        bound_event = next(
            event
            for event in audit_events
            if event["event_id"] == approval_audit["event_id"]
        )
        self.assertEqual(
            bound_event["schema_version"],
            GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION,
        )
        self.assertEqual(bound_event["request_id"], "approval-m9-05")
        self.assertEqual(
            bound_event["request_sha256"], approval_audit["request_sha256"]
        )
        self.assertEqual(bound_event["event_sha256"], approval_audit["event_sha256"])
        self.assertEqual(bound_event["policy_sha256"], approval_audit["policy_sha256"])
        self.assertEqual(set(approval["result"]), {
            "state_revision",
            "state_mcp_tool",
            "record_kind",
            "record_id",
            "event_id",
            "event_sha256",
            "actor_ref",
            "state_event_binding_sha256",
            "authorization_audit",
        })

    def test_stale_revision_and_unknown_evidence_leave_no_promotion_record(
        self,
    ) -> None:
        snapshot = self._experiment_snapshot()
        project_id = snapshot["project"]["project_id"]
        audit = InMemoryAuthorizationAuditStore()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = _service(store, project_id=project_id, audit_store=audit)
            facade = _facade(service)
            attempt = _record_attempt(
                service, project_id=project_id, attempt_id="attempt-rejections"
            )
            stale = facade.submit(
                _human_request(
                    action=ACTIONS[0],
                    request_id="proposal-stale",
                    project_id=project_id,
                    expected_revision=9,
                    payload=_promotion_payload(
                        proposal_id="proposal-stale",
                        attempt_id="attempt-rejections",
                    ),
                ),
                session_id="session-owner",
            )
            unknown_evidence = facade.submit(
                _human_request(
                    action=ACTIONS[0],
                    request_id="proposal-unknown-evidence",
                    project_id=project_id,
                    expected_revision=10,
                    payload=_promotion_payload(
                        proposal_id="proposal-unknown-evidence",
                        attempt_id="attempt-rejections",
                        criterion_evidence={
                            "throughput target": ["evidence-missing"],
                            "recovery target": ["evidence-recovery"],
                        },
                    ),
                ),
                session_id="session-owner",
            )
            stored = store.read_project(project_id)
            events = store.read_events(project_id)

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertEqual(stale["error"]["code"], "conflict")
        self.assertEqual(unknown_evidence["error"]["code"], "integrity")
        self.assertEqual(stored["project"]["revision"], 10)
        self.assertEqual(stored["experiment_promotions"], [])
        self.assertEqual(len(events), 1)

    def test_promotion_receipt_replays_after_later_revision_and_service_restart(
        self,
    ) -> None:
        snapshot = self._experiment_snapshot()
        project_id = snapshot["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            audit_path = Path(directory) / "authorization-audit.sqlite3"
            first_audit = SQLiteAuthorizationAuditStore(audit_path)
            first_audit.initialize()
            first_service = _service(
                store, project_id=project_id, audit_store=first_audit
            )
            first_service._request_cache_limit = 1
            first_facade = _facade(first_service)
            attempt = _record_attempt(
                first_service,
                project_id=project_id,
                attempt_id="attempt-durable-replay",
            )
            proposal_request = _human_request(
                action=ACTIONS[0],
                request_id="proposal-durable-replay",
                project_id=project_id,
                expected_revision=10,
                payload=_promotion_payload(
                    proposal_id="proposal-durable-replay",
                    attempt_id="attempt-durable-replay",
                ),
            )
            proposal = first_facade.submit(
                proposal_request, session_id="session-owner"
            )
            approval = first_facade.submit(
                _human_request(
                    action=ACTIONS[1],
                    request_id="approval-after-proposal",
                    project_id=project_id,
                    expected_revision=11,
                    payload=_promotion_payload(
                        proposal_id="proposal-durable-replay",
                        approval_id="approval-after-proposal",
                    ),
                ),
                session_id="session-verifier",
            )

            cache_evicted_replay = _facade(first_service).submit(
                proposal_request, session_id="session-owner"
            )

            revoked_policy = _integration_policy(project_id)
            revoked_policy["policy_revision"] = 2
            revoked_policy["grants"][0]["status"] = "revoked"
            restarted_service = StateMCPService(
                store,
                authorizer=TenantProjectAuthorizer(
                    revoked_policy,
                    expected_policy_sha256=authorization_policy_sha256(
                        revoked_policy
                    ),
                    audit_store=SQLiteAuthorizationAuditStore(audit_path),
                    clock=lambda: "2026-08-14T08:36:00+08:00",
                ),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:36:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            replay = _facade(restarted_service).submit(
                proposal_request, session_id="session-owner"
            )
            events = store.read_events(project_id)
            audit_events = first_audit.read_events(
                tenant_id="tenant-a", project_id=project_id
            )

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertTrue(approval["ok"], approval["error"])
        self.assertEqual(cache_evicted_replay, proposal)
        self.assertEqual(replay, proposal)
        self.assertEqual(len(events), 3)
        self.assertEqual(len(audit_events), 3)
        self.assertLessEqual(len(first_service._request_receipts), 1)
        self.assertLessEqual(len(first_service._authorization_receipts), 1)
        self.assertEqual(
            sum(
                event["request_id"] == proposal_request["request_id"]
                for event in audit_events
            ),
            1,
        )

    def test_audit_only_allow_is_reevaluated_after_policy_revocation(self) -> None:
        snapshot = self._experiment_snapshot()
        project_id = snapshot["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            audit_path = Path(directory) / "authorization-audit.sqlite3"
            audit = SQLiteAuthorizationAuditStore(audit_path)
            audit.initialize()
            service = _service(store, project_id=project_id, audit_store=audit)
            attempt = _record_attempt(
                service,
                project_id=project_id,
                attempt_id="attempt-audit-only",
            )
            request = _human_request(
                action=ACTIONS[0],
                request_id="proposal-audit-only",
                project_id=project_id,
                expected_revision=10,
                payload=_promotion_payload(
                    proposal_id="proposal-audit-only",
                    attempt_id="attempt-audit-only",
                ),
            )
            original_read = store.read_project
            store.read_project = lambda project_id: (_ for _ in ()).throw(
                OSError("state unavailable")
            )
            unavailable = _facade(service).submit(
                request, session_id="session-owner"
            )
            store.read_project = original_read

            revoked_policy = _integration_policy(project_id)
            revoked_policy["policy_revision"] = 2
            revoked_policy["grants"][0]["status"] = "revoked"
            restarted = StateMCPService(
                store,
                authorizer=TenantProjectAuthorizer(
                    revoked_policy,
                    expected_policy_sha256=authorization_policy_sha256(
                        revoked_policy
                    ),
                    audit_store=SQLiteAuthorizationAuditStore(audit_path),
                    clock=lambda: "2026-08-14T08:36:00+08:00",
                ),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:36:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            denied = _facade(restarted).submit(
                request, session_id="session-owner"
            )
            stored = store.read_project(project_id)
            state_events = store.read_events(project_id)
            audit_events = audit.read_events(
                tenant_id="tenant-a", project_id=project_id
            )

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertEqual(unavailable["error"]["code"], "unavailable")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "permission_denied")
        self.assertEqual(stored["project"]["revision"], 10)
        self.assertEqual(stored["experiment_promotions"], [])
        self.assertEqual(len(state_events), 1)
        self.assertEqual(
            sum(
                event.get("request_id") == request["request_id"]
                for event in audit_events
            ),
            1,
        )

    def test_audit_only_correction_is_denied_after_policy_revocation(self) -> None:
        snapshot = m307_fixtures.M307IdeaReviewTests().snapshot_with_release_evidence()
        project_id = snapshot["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            audit_path = Path(directory) / "authorization-audit.sqlite3"
            audit = SQLiteAuthorizationAuditStore(audit_path)
            audit.initialize()
            service = _service(store, project_id=project_id, audit_store=audit)
            request = _human_request(
                action=ACTIONS[2],
                request_id="protect-audit-only",
                project_id=project_id,
                expected_revision=9,
                payload={
                    "idea_id": "idea-first",
                    "protection_id": "protection-audit-only",
                    "affected_work_ids": ["work-active"],
                    "affected_scope_refs": ["capability:idea/active"],
                    "reason": "Protect the correction until current review.",
                    "evidence_ids": [],
                    "causation_ref": "work:M9-05",
                    "correlation_ref": "campaign:M9",
                },
            )
            original_read = store.read_project
            store.read_project = lambda project_id: (_ for _ in ()).throw(
                OSError("state unavailable")
            )
            unavailable = _facade(service).submit(
                request, session_id="session-owner"
            )
            store.read_project = original_read

            revoked_policy = _integration_policy(project_id)
            revoked_policy["policy_revision"] = 2
            revoked_policy["grants"][0]["status"] = "revoked"
            restarted = StateMCPService(
                store,
                authorizer=TenantProjectAuthorizer(
                    revoked_policy,
                    expected_policy_sha256=authorization_policy_sha256(
                        revoked_policy
                    ),
                    audit_store=SQLiteAuthorizationAuditStore(audit_path),
                    clock=lambda: "2026-08-14T09:11:00+08:00",
                ),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T09:11:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            denied = _facade(restarted).submit(
                request, session_id="session-owner"
            )
            stored = store.read_project(project_id)

        self.assertEqual(unavailable["error"]["code"], "unavailable")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "permission_denied")
        self.assertEqual(stored["project"]["revision"], 9)
        self.assertEqual(stored["correction_protections"], [])

    def test_correction_protection_releases_only_with_verified_evidence(self) -> None:
        snapshot = m307_fixtures.M307IdeaReviewTests().snapshot_with_release_evidence()
        project_id = snapshot["project"]["project_id"]
        audit = InMemoryAuthorizationAuditStore()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = _service(
                store,
                project_id=project_id,
                audit_store=audit,
                now="2026-08-14T09:10:00+08:00",
            )
            facade = _facade(service)
            protected = facade.submit(
                _human_request(
                    action=ACTIONS[2],
                    request_id="protect-m9-05",
                    project_id=project_id,
                    expected_revision=9,
                    payload={
                        "idea_id": "idea-first",
                        "protection_id": "protection-m9-05",
                        "affected_work_ids": ["work-active"],
                        "affected_scope_refs": ["capability:idea/active"],
                        "reason": "Current evidence requires bounded correction review.",
                        "evidence_ids": [],
                        "causation_ref": "work:M9-05",
                        "correlation_ref": "campaign:M9",
                    },
                ),
                session_id="session-owner",
            )
            invalid_release = facade.submit(
                _human_request(
                    action=ACTIONS[3],
                    request_id="release-invalid-m9-05",
                    project_id=project_id,
                    expected_revision=10,
                    payload={
                        "idea_id": "idea-first",
                        "protection_id": "protection-m9-05",
                        "release_reason": "Missing evidence cannot release the guard.",
                        "release_evidence_ids": ["evidence-missing"],
                        "causation_ref": "work:M9-05",
                        "correlation_ref": "campaign:M9",
                    },
                ),
                session_id="session-verifier",
            )
            released = facade.submit(
                _human_request(
                    action=ACTIONS[3],
                    request_id="release-m9-05",
                    project_id=project_id,
                    expected_revision=10,
                    payload={
                        "idea_id": "idea-first",
                        "protection_id": "protection-m9-05",
                        "release_reason": "Verified evidence resolves the correction.",
                        "release_evidence_ids": ["evidence-correction-verified"],
                        "causation_ref": "work:M9-05",
                        "correlation_ref": "campaign:M9",
                    },
                ),
                session_id="session-verifier",
            )
            stored = store.read_project(project_id)
            events = store.read_events(project_id)

        self.assertTrue(protected["ok"], protected["error"])
        self.assertEqual(invalid_release["error"]["code"], "integrity")
        self.assertTrue(released["ok"], released["error"])
        self.assertEqual(stored["project"]["revision"], 11)
        protection = stored["correction_protections"][0]
        self.assertEqual(protection["status"], "released")
        self.assertEqual(protection["released_by_ref"], "actor-verifier")
        self.assertEqual(len(events), 2)

    def test_cross_tenant_or_audit_failure_denies_before_state_lookup(self) -> None:
        snapshot = self._experiment_snapshot()
        project_id = snapshot["project"]["project_id"]
        request = _human_request(
            action=ACTIONS[0],
            request_id="proposal-denied",
            project_id=project_id,
            expected_revision=9,
            payload=_promotion_payload(
                proposal_id="proposal-denied", attempt_id="attempt-denied"
            ),
        )
        for audit_store, session_id in (
            (InMemoryAuthorizationAuditStore(), "session-cross-tenant"),
            (_FailingAuditStore(), "session-owner"),
        ):
            with self.subTest(session_id=session_id), tempfile.TemporaryDirectory() as directory:
                store = _CountingSQLiteStateStore(Path(directory) / "state.sqlite3")
                store.initialize()
                store.create_project(copy.deepcopy(snapshot))
                service = _service(
                    store, project_id=project_id, audit_store=audit_store
                )
                response = _facade(service).submit(request, session_id=session_id)

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "permission_denied")
                self.assertEqual(store.project_reads, 0)
                self.assertEqual(store.event_reads, 0)
                self.assertEqual(
                    store.read_project(project_id)["project"]["revision"], 9
                )
                self.assertEqual(store.read_events(project_id), [])

    def test_future_verified_evidence_cannot_approve_or_release(self) -> None:
        experiment = self._experiment_snapshot()
        experiment["evidence"][0]["verified_at"] = "2026-08-14T08:40:00+08:00"
        project_id = experiment["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            audit = InMemoryAuthorizationAuditStore()
            store = SQLiteStateStore(Path(directory) / "experiment.sqlite3")
            store.initialize()
            store.create_project(experiment)
            service = _service(store, project_id=project_id, audit_store=audit)
            facade = _facade(service)
            attempt = _record_attempt(
                service, project_id=project_id, attempt_id="attempt-future-evidence"
            )
            proposal = facade.submit(
                _human_request(
                    action=ACTIONS[0],
                    request_id="proposal-future-evidence",
                    project_id=project_id,
                    expected_revision=10,
                    payload=_promotion_payload(
                        proposal_id="proposal-future-evidence",
                        attempt_id="attempt-future-evidence",
                    ),
                ),
                session_id="session-owner",
            )
            approval = facade.submit(
                _human_request(
                    action=ACTIONS[1],
                    request_id="approval-future-evidence",
                    project_id=project_id,
                    expected_revision=11,
                    payload=_promotion_payload(
                        proposal_id="proposal-future-evidence",
                        approval_id="approval-future-evidence",
                    ),
                ),
                session_id="session-verifier",
            )
            experiment_after = store.read_project(project_id)
            experiment_events = store.read_events(project_id)

        self.assertTrue(attempt["ok"], attempt["error"])
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertEqual(approval["error"]["code"], "integrity")
        self.assertEqual(experiment_after["project"]["revision"], 11)
        self.assertEqual(len(experiment_events), 2)

        correction = m307_fixtures.M307IdeaReviewTests().snapshot_with_release_evidence()
        correction["evidence"][-1]["verified_at"] = "2026-08-14T09:20:00+08:00"
        correction_project_id = correction["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            audit = InMemoryAuthorizationAuditStore()
            store = SQLiteStateStore(Path(directory) / "correction.sqlite3")
            store.initialize()
            store.create_project(correction)
            service = _service(
                store,
                project_id=correction_project_id,
                audit_store=audit,
                now="2026-08-14T09:10:00+08:00",
            )
            facade = _facade(service)
            protection = facade.submit(
                _human_request(
                    action=ACTIONS[2],
                    request_id="protect-future-evidence",
                    project_id=correction_project_id,
                    expected_revision=9,
                    payload={
                        "idea_id": "idea-first",
                        "protection_id": "protection-future-evidence",
                        "affected_work_ids": ["work-active"],
                        "affected_scope_refs": ["capability:idea/active"],
                        "reason": "Hold correction until evidence is current.",
                        "evidence_ids": [],
                        "causation_ref": "work:M9-05",
                        "correlation_ref": "campaign:M9",
                    },
                ),
                session_id="session-owner",
            )
            release = facade.submit(
                _human_request(
                    action=ACTIONS[3],
                    request_id="release-future-evidence",
                    project_id=correction_project_id,
                    expected_revision=10,
                    payload={
                        "idea_id": "idea-first",
                        "protection_id": "protection-future-evidence",
                        "release_reason": "Future evidence cannot release the guard.",
                        "release_evidence_ids": ["evidence-correction-verified"],
                        "causation_ref": "work:M9-05",
                        "correlation_ref": "campaign:M9",
                    },
                ),
                session_id="session-verifier",
            )
            correction_after = store.read_project(correction_project_id)
            correction_events = store.read_events(correction_project_id)

        self.assertTrue(protection["ok"], protection["error"])
        self.assertEqual(release["error"]["code"], "integrity")
        self.assertEqual(correction_after["project"]["revision"], 10)
        self.assertEqual(correction_after["correction_protections"][0]["status"], "active")
        self.assertEqual(len(correction_events), 1)

    def test_state_mcp_request_cache_is_project_scoped(self) -> None:
        first_snapshot = self._experiment_snapshot()
        second_snapshot = copy.deepcopy(first_snapshot)
        second_snapshot["project"]["project_id"] = "project-experiment-two"
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(first_snapshot)
            store.create_project(second_snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-14T08:35:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            responses = []
            for snapshot in (first_snapshot, second_snapshot):
                responses.append(
                    service.call_tool(
                        "context.experiment.attempt",
                        {
                            "schema_version": "context.experiment-attempt-request/v1alpha1",
                            "request_id": "attempt-shared-request-id",
                            "project_id": snapshot["project"]["project_id"],
                            "expected_revision": 9,
                            "attempt_id": "attempt-shared-request-id",
                            "work_id": "experiment-throughput",
                            "claim_id": "claim-experiment",
                            "causation_ref": "work:M9-05",
                            "correlation_ref": "campaign:M9",
                        },
                        context=RequestContext("actor-owner", "authorization-owner"),
                    )
                )

            revisions = [
                store.read_project(snapshot["project"]["project_id"])["project"][
                    "revision"
                ]
                for snapshot in (first_snapshot, second_snapshot)
            ]

        self.assertTrue(all(response["ok"] for response in responses), responses)
        self.assertEqual(revisions, [10, 10])


if __name__ == "__main__":
    unittest.main()
