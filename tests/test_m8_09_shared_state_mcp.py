"""M8-09 Shared State MCP Work completion boundary."""

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
    shared_state_mcp_tool_definitions,
)
from context_control_plane.verification_profile import (
    build_verification_adapter,
    build_verification_profile,
    validate_verification_decision,
)
from context_control_plane.claim_evidence_gate import (
    validate_claim_evidence_verdict,
)
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext
from context_control_plane.state_store_capabilities_v2 import (
    SQLiteLocalCoordinatorStateStore,
)
from tests.test_m8_02_shared_state_mcp import _canonical_coordinator_snapshot


NOW = "2026-08-17T10:00:00+00:00"
LATER = "2026-08-17T10:00:01+00:00"


def _scope() -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": "campaign/work-a"}


def _ledger() -> WorkLedger:
    return WorkLedger(
        project_id="project-m8-09",
        project_revision=10,
        works=[
            {
                "work_id": "work-a",
                "status": "ready",
                "identity_key": "identity-a",
                "scope_refs": [_scope()],
                "revision": 3,
                "evidence_ids": [],
            }
        ],
        max_ttl_ms=60_000,
    )


class _Clock:
    def __init__(self, value: str = NOW) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class _Authorizer:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.actions: list[str] = []

    def authorize(self, context, action: str, project_id: str) -> bool:
        self.actions.append(action)
        return self.allowed


def _acquire_request(
    *,
    project_id: str = "project-m8-09",
    work_id: str = "work-a",
    expected_project_revision: int = 10,
    scope: dict[str, str] | None = None,
) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": "request-acquire-work-a",
        "project_id": project_id,
        "action": "acquire",
        "expected_project_revision": expected_project_revision,
        "work_id": work_id,
        "claim_id": "claim-work-a",
        "requested_ttl_ms": 30_000,
        "scope_owners": [scope or _scope()],
    }


def _completion_request(
    acquired: dict,
    *,
    project_id: str = "project-m8-09",
    work_id: str = "work-a",
    work_revision: int = 3,
    evidence_id: str = "evidence-work-a",
    resolver=None,
) -> dict:
    claim = acquired["result"]["claim"]
    resolver = resolver or _CompletionResolver(
        project_id=project_id,
        work_id=work_id,
        project_revision=acquired["result"]["project_revision"],
        claim_id=claim["claim_id"],
        evidence_ids=[evidence_id],
    )
    return {
        "schema_version": WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
        "request_id": "request-complete-work-a",
        "project_id": project_id,
        "work_id": work_id,
        "claim_id": claim["claim_id"],
        "expected_project_revision": acquired["result"]["project_revision"],
        "expected_work_revision": work_revision,
        "expected_claim_revision": claim["claim_revision"],
        "lease_epoch": claim["lease_epoch"],
        "fence": claim["lease_epoch"],
        "evidence_ids": [evidence_id],
        "verification_decision_sha256": resolver.verification_sha256,
        "claim_evidence_verdict_sha256": resolver.verdict_sha256,
    }


def _unsigned_digest(value: dict, digest_field: str) -> str:
    body = {key: item for key, item in value.items() if key != digest_field}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _CompletionResolver:
    def __init__(
        self,
        *,
        project_id: str,
        work_id: str,
        project_revision: int,
        claim_id: str,
        evidence_ids: list[str],
        verification_status: str = "satisfied",
    ) -> None:
        self.profile = build_verification_profile(
            profile_id="verification/m8-09/default",
            project_id=project_id,
            profile_version="1.0.0-alpha.1",
            revision=1,
            valid_from="2026-08-17T00:00:00+00:00",
            valid_until=None,
            gates=[
                {
                    "gate_id": "focused",
                    "gate_kind": "tdd",
                    "mode": "required",
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "depends_on_gate_ids": [],
                    "evidence_requirements": ["red", "green", "artifact-digest"],
                    "thresholds": [],
                }
            ],
        )
        self.adapter = build_verification_adapter(
            adapter_id="adapter/m8-09/local",
            adapter_version="1.0.0-alpha.1",
            project_id=project_id,
            profile=self.profile,
            bindings=[
                {
                    "gate_id": "focused",
                    "runner_kind": "local-process",
                    "executable": "python3",
                    "arguments": ["-m", "unittest"],
                    "working_directory_ref": "repo://context-control-plane",
                    "environment_refs": [],
                    "timeout_ms": 120_000,
                    "output_budget_bytes": 1_048_576,
                }
            ],
        )
        outcome_status = "satisfied" if verification_status == "satisfied" else "failed"
        outcome_reason = "gate-satisfied" if outcome_status == "satisfied" else "run-failed"
        decision = {
            "schema_version": "context.verification-decision/v1alpha1",
            "decision_id": f"verification/{work_id}",
            "work_id": work_id,
            "project_id": project_id,
            "project_revision": project_revision,
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "adapter_id": self.adapter["adapter_id"],
            "adapter_sha256": self.adapter["adapter_sha256"],
            "gate_outcomes": [
                {
                    "gate_id": "focused",
                    "gate_kind": "tdd",
                    "mode": "required",
                    "status": outcome_status,
                    "reason": outcome_reason,
                    "non_blocking": False,
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "run_receipt_sha256": "e" * 64,
                    "evidence_refs": [f"artifact://m8-09/{work_id}"],
                }
            ],
            "overall_status": verification_status,
            "evaluated_at": NOW,
            "state_write_authority": False,
            "completion_authority": False,
            "decision_sha256": "",
        }
        decision["decision_sha256"] = _unsigned_digest(
            decision, "decision_sha256"
        )
        validate_verification_decision(
            decision, profile=self.profile, adapter=self.adapter
        )
        claim = {
            "claim_id": claim_id,
            "work_id": work_id,
            "claim_kind": "completion",
            "statement": f"{work_id} has current completion evidence.",
            "evidence_assertion_ids": sorted(evidence_ids),
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": f"campaign/{work_id}"}
            ],
        }
        claim_sha256 = hashlib.sha256(
            json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        verdict = {
            "schema_version": "context.claim-evidence-gate/v1alpha1",
            "gate_id": f"claim-gate/{work_id}",
            "claim_id": claim_id,
            "claim_kind": "completion",
            "claim_sha256": claim_sha256,
            "decision": "allow",
            "reason": "evidence_satisfied",
            "required_authority_groups": [["current_code", "current_state"]],
            "matched_authority_kinds": ["current_state"],
            "evidence_ids": sorted(evidence_ids),
            "evaluated_at": NOW,
            "state_write_authority": False,
            "completion_authority": False,
            "verdict_sha256": "",
        }
        verdict["verdict_sha256"] = _unsigned_digest(verdict, "verdict_sha256")
        validate_claim_evidence_verdict(verdict)
        self.verification = {
            "decision": decision,
            "profile": self.profile,
            "adapter": self.adapter,
        }
        self.claim_evidence = {"verdict": verdict, "claim": claim}
        self.verification_sha256 = decision["decision_sha256"]
        self.verdict_sha256 = verdict["verdict_sha256"]

    def resolve_verification_decision(self, digest: str):
        if digest != self.verification_sha256:
            return None
        return copy.deepcopy(self.verification)

    def resolve_claim_evidence_verdict(self, digest: str):
        if digest != self.verdict_sha256:
            return None
        return copy.deepcopy(self.claim_evidence)


class M809SharedStateMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.authorizer = _Authorizer()
        self.ledger = _ledger()
        self.resolver = _CompletionResolver(
            project_id="project-m8-09",
            work_id="work-a",
            project_revision=11,
            claim_id="claim-work-a",
            evidence_ids=["evidence-work-a"],
        )
        self.service = SharedStateMCPService(
            self.ledger,
            authorizer=self.authorizer,
            clock=self.clock,
            completion_evidence_resolver=self.resolver,
        )
        self.context = RequestContext("actor-executor", "authorization-m8-09")

    def _acquire(self) -> dict:
        acquired = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            _acquire_request(),
            context=self.context,
        )
        self.assertTrue(acquired["ok"], acquired["error"])
        self.clock.value = LATER
        return acquired

    def test_authorized_completion_atomically_closes_work_and_claim(self) -> None:
        acquired = self._acquire()

        completed = self.service.call_tool(
            WORK_COMPLETION_TOOL,
            _completion_request(acquired),
            context=self.context,
        )

        self.assertTrue(completed["ok"], completed["error"])
        self.assertEqual(self.authorizer.actions[-1], "state.work.complete")
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["works"][0]["status"], "completed")
        self.assertEqual(snapshot["claims"][0]["status"], "released")
        self.assertEqual(
            snapshot["claims"][0]["close_reason"], "worker_release"
        )

    def test_stale_tokens_and_missing_verification_leave_zero_partial_writes(self) -> None:
        cases = (
            ("expected_project_revision", 10, "completion_rejected", "stale_revision"),
            ("expected_work_revision", 2, "completion_rejected", "work_revision_mismatch"),
            ("expected_claim_revision", 2, "completion_rejected", "claim_revision_mismatch"),
            ("lease_epoch", 2, "completion_rejected", "lease_epoch_mismatch"),
            ("fence", 2, "completion_rejected", "fence_mismatch"),
            ("evidence_ids", [], "invalid_request", "invalid_evidence_ids"),
            (
                "verification_decision_sha256",
                "bad",
                "invalid_request",
                "invalid_verification_decision_sha256",
            ),
            (
                "claim_evidence_verdict_sha256",
                "bad",
                "invalid_request",
                "invalid_claim_evidence_verdict_sha256",
            ),
        )
        for field, value, error_code, reason in cases:
            with self.subTest(field=field):
                ledger = _ledger()
                clock = _Clock()
                service = SharedStateMCPService(
                    ledger,
                    authorizer=_Authorizer(),
                    clock=clock,
                    completion_evidence_resolver=self.resolver,
                )
                acquired = service.call_tool(
                    CLAIM_LIFECYCLE_TOOL,
                    _acquire_request(),
                    context=self.context,
                )
                clock.value = LATER
                request = _completion_request(acquired)
                request[field] = value
                before = ledger.snapshot()

                response = service.call_tool(
                    WORK_COMPLETION_TOOL,
                    request,
                    context=self.context,
                )

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], error_code)
                self.assertEqual(response["error"]["reason"], reason)
                self.assertEqual(ledger.snapshot(), before)

    def test_exact_replay_is_idempotent_and_changed_payload_conflicts(self) -> None:
        acquired = self._acquire()
        request = _completion_request(acquired)

        first = self.service.call_tool(
            WORK_COMPLETION_TOOL, request, context=self.context
        )
        after_first = self.ledger.snapshot()
        replay = self.service.call_tool(
            WORK_COMPLETION_TOOL, copy.deepcopy(request), context=self.context
        )
        changed = {
            **request,
            "verification_decision_sha256": "c" * 64,
        }
        conflict = self.service.call_tool(
            WORK_COMPLETION_TOOL, changed, context=self.context
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.ledger.snapshot(), after_first)
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(conflict["error"]["reason"], "request_id_reused")

    def test_tool_definition_is_strict_and_rejects_client_authority_fields(self) -> None:
        definitions = {
            item["name"]: item for item in shared_state_mcp_tool_definitions()
        }
        schema = definitions[WORK_COMPLETION_TOOL]["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for field in ("actor_ref", "observed_at", "completion_authority"):
            self.assertNotIn(field, schema["properties"])

        acquired = self._acquire()
        request = {**_completion_request(acquired), "actor_ref": "forged"}
        before = self.ledger.snapshot()
        rejected = self.service.call_tool(
            WORK_COMPLETION_TOOL, request, context=self.context
        )
        self.assertEqual(rejected["error"]["code"], "invalid_request")
        self.assertEqual(rejected["error"]["reason"], "unexpected_fields")
        self.assertEqual(self.ledger.snapshot(), before)

    def test_unresolved_or_non_satisfied_verification_cannot_complete_work(self) -> None:
        for label, resolver, reason in (
            ("missing", None, "completion_evidence_resolver_required"),
            (
                "failed",
                _CompletionResolver(
                    project_id="project-m8-09",
                    work_id="work-a",
                    project_revision=11,
                    claim_id="claim-work-a",
                    evidence_ids=["evidence-work-a"],
                    verification_status="failed",
                ),
                "verification_not_satisfied",
            ),
            (
                "wrong-work",
                _CompletionResolver(
                    project_id="project-m8-09",
                    work_id="work-other",
                    project_revision=11,
                    claim_id="claim-work-a",
                    evidence_ids=["evidence-work-a"],
                ),
                "verification_binding_mismatch",
            ),
            (
                "wrong-evidence",
                _CompletionResolver(
                    project_id="project-m8-09",
                    work_id="work-a",
                    project_revision=11,
                    claim_id="claim-work-a",
                    evidence_ids=["evidence-other"],
                ),
                "claim_evidence_binding_mismatch",
            ),
        ):
            with self.subTest(label=label):
                ledger = _ledger()
                service = SharedStateMCPService(
                    ledger,
                    authorizer=_Authorizer(),
                    clock=self.clock,
                    completion_evidence_resolver=resolver,
                )
                acquired = service.call_tool(
                    CLAIM_LIFECYCLE_TOOL,
                    _acquire_request(),
                    context=self.context,
                )
                request_resolver = resolver or self.resolver
                request = _completion_request(
                    acquired,
                    resolver=request_resolver,
                )
                if label == "wrong-evidence":
                    request["evidence_ids"] = ["evidence-work-a"]
                before = ledger.snapshot()

                response = service.call_tool(
                    WORK_COMPLETION_TOOL,
                    request,
                    context=self.context,
                )

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "completion_rejected")
                self.assertEqual(response["error"]["reason"], reason)
                self.assertEqual(ledger.snapshot(), before)

    def test_sqlite_completion_persists_event_and_replays_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database)
            store.initialize()
            source = _canonical_coordinator_snapshot()
            verified_evidence = {
                "evidence_id": "evidence-work-a",
                "kind": "test",
                "artifact_ref": "artifact://m8-09/work-a",
                "content_sha256": "d" * 64,
                "validity": "verified",
                "observed_at": NOW,
                "verified_at": NOW,
            }
            source["evidence"].append(verified_evidence)
            store.create_project(source)
            coordinator = SQLiteLocalCoordinatorStateStore(store)
            coordinator.initialize_work_ledger(
                project_id=source["project"]["project_id"],
                project_revision=source["project"]["revision"],
                works=source["works"],
                max_ttl_ms=60_000,
            )
            context = RequestContext("actor-a", "authorization-m8-09")
            service = SharedStateMCPService(
                coordinator,
                authorizer=self.authorizer,
                clock=_Clock(NOW),
            )
            work = next(
                item for item in source["works"] if item["work_id"] == "work-active"
            )
            acquired = service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                _acquire_request(
                    project_id=source["project"]["project_id"],
                    work_id=work["work_id"],
                    expected_project_revision=source["project"]["revision"],
                    scope=work["scope_refs"][0],
                ),
                context=context,
            )
            self.assertTrue(acquired["ok"], acquired["error"])
            request = _completion_request(
                acquired,
                project_id=source["project"]["project_id"],
                work_id=work["work_id"],
                work_revision=work["revision"],
            )
            first = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock(LATER),
                completion_evidence_resolver=_CompletionResolver(
                    project_id=source["project"]["project_id"],
                    work_id=work["work_id"],
                    project_revision=acquired["result"]["project_revision"],
                    claim_id=acquired["result"]["claim"]["claim_id"],
                    evidence_ids=["evidence-work-a"],
                ),
            ).call_tool(WORK_COMPLETION_TOOL, request, context=context)
            replay = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock("2026-08-17T10:00:02+00:00"),
                completion_evidence_resolver=_CompletionResolver(
                    project_id=source["project"]["project_id"],
                    work_id=work["work_id"],
                    project_revision=acquired["result"]["project_revision"],
                    claim_id=acquired["result"]["claim"]["claim_id"],
                    evidence_ids=["evidence-work-a"],
                ),
            ).call_tool(WORK_COMPLETION_TOOL, copy.deepcopy(request), context=context)

            self.assertTrue(first["ok"], first["error"])
            self.assertEqual(replay, first)
            state = SQLiteStateStore(database).read_project(
                source["project"]["project_id"]
            )
            persisted_work = next(
                item for item in state["works"] if item["work_id"] == work["work_id"]
            )
            persisted_claim = next(
                item
                for item in state["claims"]
                if item["claim_id"] == "claim-work-a"
            )
            self.assertEqual(persisted_work["status"], "completed")
            self.assertEqual(persisted_claim["status"], "released")
            events = SQLiteStateStore(database).read_events(
                source["project"]["project_id"]
            )
            self.assertEqual(events[-1]["revision_after"], first["result"]["project_revision"])


if __name__ == "__main__":
    unittest.main()
