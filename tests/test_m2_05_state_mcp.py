import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.postgres_state_store import PostgresStateStore
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    RequestContext,
    StateMCPService,
    state_mcp_tool_definitions,
)
from context_control_plane.state_store import StateStoreCapabilityError


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class _ExplodingAuthorizer:
    def authorize(self, context, action, project_id):
        raise RuntimeError("authorization backend unavailable")


class _CountingSQLiteStore(SQLiteStateStore):
    def __init__(self, path):
        super().__init__(path)
        self.read_project_calls = 0
        self.read_events_calls = 0

    def read_project(self, project_id):
        self.read_project_calls += 1
        return super().read_project(project_id)

    def read_events(self, project_id):
        self.read_events_calls += 1
        return super().read_events(project_id)


class M205StateMCPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture_set = yaml.safe_load(
            (
                cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml"
            ).read_text(encoding="utf-8")
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "completed-work-overlap-blocked"
            )
        )

    def make_service(self, store, *, authorizer=None):
        return StateMCPService(
            store,
            authorizer=authorizer,
            registry_digest="a" * 64,
            clock=lambda: "2026-08-10T05:00:00+08:00",
            event_id_factory=lambda request_id: f"event-mcp-{request_id}",
        )

    def make_commit_request(
        self,
        *,
        request_id="request-commit-1",
        revision=14,
        project_id=None,
    ):
        idea = {
            "idea_id": f"idea-{request_id}",
            "parent_work_id": "work-repeat",
            "source_ref": f"opaque://mcp/{request_id}",
            "summary": "Keep this candidate parked without changing active work.",
            "status": "parked",
            "return_work_id": "work-repeat",
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": "M3",
            "evidence_ids": [],
        }
        return {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": request_id,
            "project_id": project_id or self.snapshot["project"]["project_id"],
            "expected_revision": revision,
            "causation_ref": "work:M2-05",
            "correlation_ref": "campaign:M2",
            "supersedes_event_id": None,
            "changes": [
                {
                    "collection": "ideas",
                    "object_id": idea["idea_id"],
                    "value": idea,
                }
            ],
        }

    def make_ready_snapshot(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["project"]["updated_at"] = "2026-08-10T04:00:00+08:00"
        snapshot["project"]["open_blocker_ids"] = []
        snapshot["blockers"] = []
        work = next(item for item in snapshot["works"] if item["work_id"] == "work-repeat")
        work["status"] = "ready"
        work["dedupe_status"] = "clear"
        work["overlap_candidate_ids"] = []
        work["blocker_ids"] = []
        return snapshot

    def make_claim_request(
        self,
        *,
        request_id="request-claim-1",
        project_id="project-dedupe",
        revision=14,
        scope_ref="src/registry.py",
    ):
        return {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": request_id,
            "project_id": project_id,
            "expected_revision": revision,
            "work_id": "work-repeat",
            "claim_id": f"claim-{request_id}",
            "scope_owners": [{"scope_kind": "file", "scope_ref": scope_ref}],
            "lease_expires_at": "2026-08-10T06:00:00+08:00",
            "causation_ref": "work:M2-05",
            "correlation_ref": "campaign:M2",
        }

    def make_effect_request(
        self,
        *,
        request_id="request-effect-1",
        action="authorize",
        revision=15,
        result_ref=None,
        scope_ref="src/registry.py",
        project_id="project-dedupe",
        claim_id="claim-request-claim-1",
        operation="write-artifact",
    ):
        return {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": request_id,
            "project_id": project_id,
            "expected_revision": revision,
            "action": action,
            "effect_id": "effect-mcp-1",
            "effect_key": "effect-key-mcp-1",
            "work_id": "work-repeat",
            "claim_id": claim_id,
            "operation": operation,
            "scope_ref": {"scope_kind": "file", "scope_ref": scope_ref},
            "result_ref": result_ref,
            "evidence_ids": [],
            "causation_ref": "work:M2-05",
            "correlation_ref": "campaign:M2",
        }

    def test_contract_schema_is_registered_hashed_and_strict(self):
        schema_path = self.root / "schemas" / "m2-05" / "state-mcp.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.state-mcp"
        )

        self.assertEqual(entry["current_wire_version"], "context.state-mcp/v1alpha1")
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        for definition_name in (
            "read_request",
            "commit_request",
            "claim_request",
            "effect_request",
            "effect_gate_request",
            "effect_gate_result",
            "response",
        ):
            with self.subTest(definition_name=definition_name):
                definition = schema["$defs"][definition_name]
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(
                    set(definition["required"]),
                    set(definition["properties"]),
                )
                Draft202012Validator.check_schema(definition)

    def test_effect_gate_response_rejects_a_contradictory_verdict(self):
        schema = json.loads(
            (
                self.root / "schemas" / "m2-05" / "state-mcp.schema.json"
            ).read_text(encoding="utf-8")
        )
        contradictory_response = {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": "request-contradictory-effect-gate",
            "tool": "context.state.effect.gate",
            "ok": True,
            "result": {
                "verdict": {
                    "schema_version": "context.effect-scope-verdict/v1alpha1",
                    "decision": "allow",
                    "read_only": True,
                    "reason": "authorized",
                },
                "revision": 7,
                "registry_digest": "a" * 64,
                "capabilities": {},
            },
            "error": None,
        }
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(contradictory_response)

    def test_contract_root_validates_wire_documents_and_response_state(self):
        schema = json.loads(
            (
                self.root / "schemas" / "m2-05" / "state-mcp.schema.json"
            ).read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            context = RequestContext("actor-root", "authorization-test")
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            read_response = service.call_tool(
                "context.state.read",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-root-response-read",
                    "project_id": self.snapshot["project"]["project_id"],
                },
                context=context,
            )
            commit_response = service.call_tool(
                "context.state.commit",
                self.make_commit_request(request_id="request-root-response-commit"),
                context=context,
            )
            gate_response = service.call_tool(
                "context.state.effect.gate",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-root-response-effect-gate",
                    "project_id": self.snapshot["project"]["project_id"],
                    "expected_revision": self.snapshot["project"]["revision"],
                    "effect_id": "effect-root-gate",
                    "work_id": "work-repeat",
                    "claim_id": "claim-root-gate",
                    "operation": "write-file",
                    "scope_ref": {
                        "scope_kind": "file",
                        "scope_ref": "src/registry.py",
                    },
                },
                context=context,
            )
            denied_response = self.make_service(store).call_tool(
                "context.state.read",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-root-response-denied",
                    "project_id": self.snapshot["project"]["project_id"],
                },
                context=context,
            )
        for document in (
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-root-read",
                "project_id": "project-root",
            },
            self.make_commit_request(request_id="request-root-commit"),
            self.make_claim_request(request_id="request-root-claim"),
            self.make_effect_request(request_id="request-root-effect"),
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-root-effect-gate",
                "project_id": "project-root",
                "expected_revision": 1,
                "effect_id": "effect-root-gate",
                "work_id": "work-root",
                "claim_id": "claim-root",
                "operation": "write-file",
                "scope_ref": {
                    "scope_kind": "file",
                    "scope_ref": "src/root.py",
                },
            },
            read_response,
            commit_response,
            gate_response,
            denied_response,
        ):
            with self.subTest(document=document["request_id"]):
                validator.validate(document)

        for invalid in (
            {},
            {"unknown": "document"},
            {
                **read_response,
                "result": {},
            },
            {
                **commit_response,
                "result": {
                    key: value
                    for key, value in commit_response["result"].items()
                    if key != "event"
                },
            },
            {
                **read_response,
                "error": {"code": "conflict", "message": "must be null on success"},
            },
            {
                **denied_response,
                "ok": True,
                "result": {},
            },
            {
                **self.make_effect_request(request_id="request-root-authorize-result"),
                "result_ref": "artifact://sha256/" + "1" * 64,
            },
            self.make_effect_request(
                request_id="request-root-complete-no-result",
                action="complete",
                result_ref=None,
            ),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validator.validate(invalid)

    def test_tool_definitions_expose_the_versioned_state_tools(self):
        definitions = state_mcp_tool_definitions()

        self.assertEqual(
            {item["name"] for item in definitions},
            {
                "context.state.read",
                "context.state.commit",
                "context.state.claim",
                "context.state.effect",
                "context.state.effect.gate",
                "context.experiment.attempt",
                "context.experiment.effect",
                "context.experiment.promotion.propose",
                "context.experiment.promotion.approve",
                "context.idea.capture",
                "context.idea.review",
                "context.idea.correction.protect",
                "context.idea.correction.release",
            },
        )
        schema_versions = {
            "context.experiment.attempt": "context.experiment-attempt-request/v1alpha1",
            "context.experiment.effect": "context.experiment-effect-request/v1alpha1",
            "context.experiment.promotion.propose": "context.experiment-promotion-proposal-request/v1alpha1",
            "context.experiment.promotion.approve": "context.experiment-promotion-approval-request/v1alpha1",
            "context.idea.capture": "context.idea-capture-request/v1alpha1",
            "context.idea.review": "context.idea-review-request/v1alpha1",
            "context.idea.correction.protect": "context.idea-correction-protection-request/v1alpha1",
            "context.idea.correction.release": "context.idea-correction-release-request/v1alpha1",
        }
        for item in definitions:
            with self.subTest(tool=item["name"]):
                if item["name"] == "context.idea.capture":
                    self.assertEqual(
                        [
                            variant["properties"]["schema_version"]["const"]
                            for variant in item["inputSchema"]["oneOf"]
                        ],
                        [
                            "context.idea-capture-request/v1alpha1",
                            "context.idea-capture-request/v2alpha1",
                        ],
                    )
                    continue
                self.assertEqual(
                    item["inputSchema"]["properties"]["schema_version"]["const"],
                    schema_versions.get(
                        item["name"], "context.state-mcp-request/v1alpha1"
                    ),
                )
                self.assertFalse(item["inputSchema"]["additionalProperties"])

    def test_effect_contract_requires_non_null_execution_identity_fields(self):
        definition = next(
            item for item in state_mcp_tool_definitions()
            if item["name"] == "context.state.effect"
        )["inputSchema"]
        for field in ("effect_key", "work_id", "claim_id", "operation", "scope_ref"):
            with self.subTest(field=field):
                self.assertNotIn("null", definition["properties"][field].get("type", []))

    def test_effect_action_rejects_an_invalid_result_ref_before_backend_access(self):
        requests = (
            {
                **self.make_effect_request(request_id="request-authorize-with-result"),
                "result_ref": "artifact://sha256/" + "2" * 64,
            },
            self.make_effect_request(
                request_id="request-complete-without-result",
                action="complete",
                result_ref=None,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            responses = [
                service.call_tool(
                    "context.state.effect",
                    request,
                    context=RequestContext("actor-second", "authorization-test"),
                )
                for request in requests
            ]

        self.assertEqual(
            [response["error"]["code"] for response in responses],
            ["invalid_request", "invalid_request"],
        )
        self.assertEqual(store.read_project_calls, 0)
        self.assertEqual(store.read_events_calls, 0)

    def test_capability_failures_are_normalized(self):
        class CapabilityChangingStore(SQLiteStateStore):
            def read_project(self, project_id):
                raise StateStoreCapabilityError("operation was withdrawn")

        with tempfile.TemporaryDirectory() as directory:
            store = CapabilityChangingStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            response = service.call_tool(
                "context.state.read",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-capability-error",
                    "project_id": self.snapshot["project"]["project_id"],
                },
                context=RequestContext("actor-reader", "authorization-test"),
            )
        self.assertEqual(response["error"]["code"], "capability")

    def test_postgres_connection_failure_is_busy_without_hiding_programming_errors(self):
        store = PostgresStateStore(
            "postgresql://context_test@127.0.0.1:1/context_test?connect_timeout=1"
        )
        response = self.make_service(
            store,
            authorizer=_AllowAuthorizer(),
        ).call_tool(
            "context.state.read",
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-postgres-unavailable",
                "project_id": "project-unavailable",
            },
            context=RequestContext("actor-reader", "authorization-test"),
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "busy")

        with patch(
            "context_control_plane.postgres_state_store.psycopg.connect",
            side_effect=ValueError("programming failure"),
        ), self.assertRaisesRegex(ValueError, "programming failure"):
            store.read_project("project-unavailable")

    def test_unknown_tool_version_and_fields_are_rejected_before_backend_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            context = RequestContext(
                subject_ref="actor-reader",
                authorization_ref="authorization-test",
            )
            valid = {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-read-1",
                "project_id": self.snapshot["project"]["project_id"],
            }

            unknown_tool = service.call_tool("context.state.drop", valid, context=context)
            wrong_version = service.call_tool(
                "context.state.read",
                {**valid, "schema_version": "context.state-mcp-request/v2"},
                context=context,
            )
            unknown_field = service.call_tool(
                "context.state.read",
                {**valid, "actor_ref": "forged-actor"},
                context=context,
            )

        self.assertEqual(unknown_tool["error"]["code"], "unsupported")
        self.assertEqual(wrong_version["error"]["code"], "invalid_request")
        self.assertEqual(unknown_field["error"]["code"], "invalid_request")
        self.assertEqual(store.read_project_calls, 0)
        self.assertEqual(store.read_events_calls, 0)

    def test_default_deny_and_authorizer_failure_do_not_touch_the_backend(self):
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "request-read-denied",
            "project_id": self.snapshot["project"]["project_id"],
        }
        context = RequestContext(
            subject_ref="actor-reader",
            authorization_ref="authorization-test",
        )

        for authorizer in (None, _ExplodingAuthorizer()):
            with self.subTest(authorizer=type(authorizer).__name__), tempfile.TemporaryDirectory() as directory:
                store = _CountingSQLiteStore(Path(directory) / "state.db")
                store.initialize()
                store.create_project(copy.deepcopy(self.snapshot))
                response = self.make_service(
                    store,
                    authorizer=authorizer,
                ).call_tool("context.state.read", request, context=context)

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "permission_denied")
                self.assertEqual(store.read_project_calls, 0)
                self.assertEqual(store.read_events_calls, 0)

    def test_default_deny_covers_all_state_mutation_tools_before_backend_access(self):
        context = RequestContext(
            subject_ref="actor-denied",
            authorization_ref="authorization-test",
        )
        requests = {
            "context.state.commit": self.make_commit_request(),
            "context.state.claim": self.make_claim_request(),
            "context.state.effect": self.make_effect_request(),
        }

        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store)
            responses = {
                tool: service.call_tool(tool, request, context=context)
                for tool, request in requests.items()
            }

        for tool, response in responses.items():
            with self.subTest(tool=tool):
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "permission_denied")
        self.assertEqual(store.read_project_calls, 0)
        self.assertEqual(store.read_events_calls, 0)

    def test_transport_must_supply_a_trusted_request_context(self):
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "request-untrusted-context",
            "project_id": self.snapshot["project"]["project_id"],
        }

        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            response = self.make_service(
                store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.read", request, context={"subject_ref": "forged"})

        self.assertEqual(response["error"]["code"], "permission_denied")
        self.assertEqual(store.read_project_calls, 0)
        self.assertEqual(store.read_events_calls, 0)

    def test_authorized_read_returns_current_receipt_and_a_defensive_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            request = {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-read-authorized",
                "project_id": self.snapshot["project"]["project_id"],
            }
            context = RequestContext(
                subject_ref="actor-reader",
                authorization_ref="authorization-test",
            )

            response = service.call_tool("context.state.read", request, context=context)
            response["result"]["snapshot"]["project"]["revision"] = 999
            reread = service.call_tool("context.state.read", request, context=context)

        self.assertTrue(response["ok"])
        self.assertEqual(reread["result"]["revision"], 14)
        self.assertEqual(reread["result"]["snapshot"]["project"]["revision"], 14)
        self.assertEqual(reread["result"]["event_head"], None)
        self.assertEqual(reread["result"]["registry_digest"], "a" * 64)
        self.assertEqual(
            reread["result"]["capabilities"]["adapter_id"],
            "context.sqlite",
        )
        self.assertEqual(store.read_project_calls, 2)
        self.assertEqual(store.read_events_calls, 2)

    def test_commit_rejects_invalid_revision_and_dedicated_collections_before_read(self):
        context = RequestContext(
            subject_ref="actor-writer",
            authorization_ref="authorization-test",
        )
        invalid_revision = self.make_commit_request(revision=True)
        claim = {
            "claim_id": "claim-forged",
            "work_id": "work-repeat",
            "actor_ref": "actor-forged",
            "status": "active",
            "expected_project_revision": 15,
            "claimed_at": "2026-08-10T05:00:00+08:00",
            "lease_expires_at": "2026-08-10T06:00:00+08:00",
            "released_at": None,
            "scope_owners": [
                {"scope_kind": "file", "scope_ref": "src/registry.py"}
            ],
        }
        dedicated_collection = {
            **self.make_commit_request(request_id="request-forged-claim"),
            "changes": [
                {
                    "collection": "claims",
                    "object_id": claim["claim_id"],
                    "value": claim,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())

            revision_response = service.call_tool(
                "context.state.commit",
                invalid_revision,
                context=context,
            )
            collection_response = service.call_tool(
                "context.state.commit",
                dedicated_collection,
                context=context,
            )

        self.assertEqual(revision_response["error"]["code"], "invalid_request")
        self.assertEqual(collection_response["error"]["code"], "invalid_request")
        self.assertEqual(store.read_project_calls, 0)
        self.assertEqual(store.read_events_calls, 0)

    def test_commit_builds_the_event_and_projection_from_trusted_context(self):
        context = RequestContext(
            subject_ref="actor-writer",
            authorization_ref="authorization-test",
        )
        request = self.make_commit_request()

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            response = self.make_service(
                store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.commit", request, context=context)
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["result"]["revision"], 15)
        self.assertEqual(stored["project"]["revision"], 15)
        self.assertEqual(stored["project"]["updated_at"], "2026-08-10T05:00:00+08:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_ref"], context.subject_ref)
        self.assertEqual(events[0]["event_id"], "event-mcp-request-commit-1")
        self.assertEqual(events[0]["sequence_no"], 1)
        self.assertIsNone(events[0]["previous_event_sha256"])
        self.assertEqual(events[0]["project_after"], stored["project"])
        self.assertEqual(
            {idea["idea_id"] for idea in stored["ideas"]},
            {"idea-request-commit-1"},
        )

    def test_stale_or_invalid_commit_never_rebases_or_partially_appends(self):
        context = RequestContext(
            subject_ref="actor-writer",
            authorization_ref="authorization-test",
        )
        first = self.make_commit_request(request_id="request-first")
        stale = self.make_commit_request(request_id="request-stale")
        invalid = self.make_commit_request(
            request_id="request-invalid",
            revision=15,
        )
        invalid_decision = {
            "decision_id": "decision-invalid",
            "work_id": "work-unknown",
            "status": "accepted",
            "statement": "This invalid decision must not be committed.",
            "decided_at": "2026-08-10T05:00:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": [],
        }
        invalid["changes"] = [
            {
                "collection": "decisions",
                "object_id": invalid_decision["decision_id"],
                "value": invalid_decision,
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())

            first_response = service.call_tool(
                "context.state.commit",
                first,
                context=context,
            )
            stale_response = service.call_tool(
                "context.state.commit",
                stale,
                context=context,
            )
            invalid_response = service.call_tool(
                "context.state.commit",
                invalid,
                context=context,
            )
            stored = store.read_project(first["project_id"])
            events = store.read_events(first["project_id"])

        self.assertTrue(first_response["ok"])
        self.assertEqual(stale_response["error"]["code"], "conflict")
        self.assertEqual(invalid_response["error"]["code"], "integrity")
        self.assertEqual(stored["project"]["revision"], 15)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            {idea["idea_id"] for idea in stored["ideas"]},
            {"idea-request-first"},
        )

    def test_malformed_nested_commit_value_is_normalized_without_writing(self):
        request = self.make_commit_request(request_id="request-malformed-nested")
        request["changes"] = [
            {
                "collection": "works",
                "object_id": "work-repeat",
                "value": {"work_id": "work-repeat"},
            }
        ]
        context = RequestContext("actor-writer", "authorization-test")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            response = service.call_tool(
                "context.state.commit",
                request,
                context=context,
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "integrity")
        self.assertEqual(stored["project"]["revision"], 14)
        self.assertEqual(events, [])

    def test_same_request_id_replays_the_same_receipt_and_different_payload_conflicts(self):
        context = RequestContext(
            subject_ref="actor-writer",
            authorization_ref="authorization-test",
        )
        request = self.make_commit_request(request_id="request-idempotent")
        different = copy.deepcopy(request)
        different["changes"][0]["value"]["summary"] = "A different intent must conflict."

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            first = service.call_tool("context.state.commit", request, context=context)
            replay = service.call_tool("context.state.commit", request, context=context)
            conflict = service.call_tool("context.state.commit", different, context=context)
            events = store.read_events(request["project_id"])

        self.assertTrue(first["ok"])
        self.assertEqual(replay, first)
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(len(events), 1)

    def test_concurrent_identical_request_id_returns_one_replayed_receipt(self):
        class RendezvousSQLiteStore(SQLiteStateStore):
            def __init__(self, path):
                super().__init__(path)
                self.read_barrier = threading.Barrier(2)

            def read_project(self, project_id):
                snapshot = super().read_project(project_id)
                try:
                    self.read_barrier.wait(timeout=0.2)
                except threading.BrokenBarrierError:
                    pass
                return snapshot

        request = self.make_commit_request(request_id="request-concurrent-replay")
        context = RequestContext("actor-writer", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            store = RendezvousSQLiteStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(
                        lambda _: service.call_tool(
                            "context.state.commit",
                            copy.deepcopy(request),
                            context=context,
                        ),
                        range(2),
                    )
                )
            events = store.read_events(request["project_id"])

        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[1], responses[0])
        self.assertEqual(len(events), 1)

    def test_claim_atomically_activates_ready_work_and_binds_trusted_actor(self):
        context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        request = self.make_claim_request()
        ready_snapshot = self.make_ready_snapshot()

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(ready_snapshot)
            response = self.make_service(
                store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.claim", request, context=context)
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["result"]["revision"], 15)
        self.assertEqual(stored["project"]["active_work_ids"], ["work-repeat"])
        self.assertEqual(stored["project"]["primary_work_id"], "work-repeat")
        work = next(item for item in stored["works"] if item["work_id"] == "work-repeat")
        claim = next(item for item in stored["claims"] if item["claim_id"] == request["claim_id"])
        self.assertEqual(work["status"], "active")
        self.assertEqual(claim["actor_ref"], context.subject_ref)
        self.assertEqual(claim["expected_project_revision"], 15)
        self.assertEqual(claim["claimed_at"], "2026-08-10T05:00:00+08:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["changes"][0]["collection"], "works")
        self.assertEqual(events[0]["changes"][1]["collection"], "claims")
        self.assertFalse(response["result"]["capabilities"]["unique_claim"])
        self.assertEqual(response["result"]["capabilities"]["lease_clock"], "none")

    def test_claim_scope_and_work_readiness_fail_atomically(self):
        context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        non_owner_context = RequestContext(
            subject_ref="actor-not-owner",
            authorization_ref="authorization-test",
        )
        wrong_scope = self.make_claim_request(scope_ref="src/unrelated.py")
        not_ready = self.make_claim_request(request_id="request-not-ready")
        non_owner = self.make_claim_request(request_id="request-non-owner")

        with tempfile.TemporaryDirectory() as directory:
            scope_store = SQLiteStateStore(Path(directory) / "scope.db")
            scope_store.initialize()
            scope_store.create_project(self.make_ready_snapshot())
            wrong_scope_response = self.make_service(
                scope_store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.claim", wrong_scope, context=context)
            non_owner_response = self.make_service(
                scope_store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.claim", non_owner, context=non_owner_context)

            readiness_store = SQLiteStateStore(Path(directory) / "readiness.db")
            readiness_store.initialize()
            readiness_store.create_project(copy.deepcopy(self.snapshot))
            not_ready_response = self.make_service(
                readiness_store,
                authorizer=_AllowAuthorizer(),
            ).call_tool("context.state.claim", not_ready, context=context)

            scope_state = scope_store.read_project(wrong_scope["project_id"])
            readiness_state = readiness_store.read_project(not_ready["project_id"])

        self.assertEqual(wrong_scope_response["error"]["code"], "integrity")
        self.assertEqual(non_owner_response["error"]["code"], "integrity")
        self.assertEqual(not_ready_response["error"]["code"], "integrity")
        self.assertEqual(scope_state["project"]["revision"], 14)
        self.assertEqual(readiness_state["project"]["revision"], 14)

    def test_claim_identity_cannot_be_rebound_after_release(self):
        snapshot = self.make_ready_snapshot()
        snapshot["claims"].append(
            {
                "claim_id": "claim-request-claim-1",
                "work_id": "work-repeat",
                "actor_ref": "actor-second",
                "status": "released",
                "expected_project_revision": 13,
                "claimed_at": "2026-08-10T03:00:00+08:00",
                "lease_expires_at": "2026-08-10T03:45:00+08:00",
                "released_at": "2026-08-10T03:30:00+08:00",
                "scope_owners": [
                    {"scope_kind": "file", "scope_ref": "src/registry.py"}
                ],
            }
        )
        context = RequestContext("actor-second", "authorization-test")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            response = self.make_service(
                store,
                authorizer=_AllowAuthorizer(),
            ).call_tool(
                "context.state.claim",
                self.make_claim_request(),
                context=context,
            )
            stored = store.read_project("project-dedupe")
            events = store.read_events("project-dedupe")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "conflict")
        self.assertEqual(stored["project"]["revision"], 14)
        self.assertEqual(stored["claims"][0]["status"], "released")
        self.assertEqual(events, [])

    def test_effect_authorize_binds_active_claim_and_complete_advances_watermark(self):
        context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        claim_request = self.make_claim_request()
        authorize_request = self.make_effect_request()
        complete_request = self.make_effect_request(
            request_id="request-effect-complete",
            action="complete",
            revision=16,
            result_ref="artifact://sha256/" + "d" * 64,
        )
        ready_snapshot = self.make_ready_snapshot()

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(ready_snapshot)
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            claim_response = service.call_tool(
                "context.state.claim",
                claim_request,
                context=context,
            )
            authorize_response = service.call_tool(
                "context.state.effect",
                authorize_request,
                context=context,
            )
            complete_response = service.call_tool(
                "context.state.effect",
                complete_request,
                context=context,
            )
            stored = store.read_project("project-dedupe")
            events = store.read_events("project-dedupe")

        self.assertTrue(claim_response["ok"])
        self.assertTrue(authorize_response["ok"], authorize_response["error"])
        self.assertTrue(complete_response["ok"], complete_response["error"])
        effect = next(item for item in stored["effects"] if item["effect_id"] == "effect-mcp-1")
        self.assertEqual(effect["status"], "succeeded")
        self.assertEqual(effect["sequence_no"], 1)
        self.assertEqual(stored["project"]["effect_high_watermark"], 1)
        self.assertEqual(stored["project"]["revision"], 17)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["actor_ref"], context.subject_ref)
        self.assertEqual(events[2]["actor_ref"], context.subject_ref)
        self.assertNotIn("execution_grant", authorize_response["result"])

    def test_effect_rejects_actor_scope_claim_and_duplicate_key_without_new_events(self):
        claim_context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        other_context = RequestContext(
            subject_ref="actor-other",
            authorization_ref="authorization-test",
        )
        claim_request = self.make_claim_request()
        wrong_actor = self.make_effect_request(request_id="request-effect-wrong-actor")
        wrong_scope = self.make_effect_request(
            request_id="request-effect-wrong-scope",
            scope_ref="src/other.py",
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(self.make_ready_snapshot())
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            self.assertTrue(
                service.call_tool(
                    "context.state.claim",
                    claim_request,
                    context=claim_context,
                )["ok"]
            )
            wrong_actor_response = service.call_tool(
                "context.state.effect",
                wrong_actor,
                context=other_context,
            )
            wrong_scope_response = service.call_tool(
                "context.state.effect",
                wrong_scope,
                context=claim_context,
            )
            stored = store.read_project("project-dedupe")
            events = store.read_events("project-dedupe")

        self.assertEqual(wrong_actor_response["error"]["code"], "integrity")
        self.assertEqual(wrong_scope_response["error"]["code"], "integrity")
        self.assertEqual(stored["project"]["revision"], 15)
        self.assertEqual(stored["effects"], [])
        self.assertEqual(len(events), 1)

    def test_effect_complete_rejects_an_operation_change_without_new_events(self):
        context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(self.make_ready_snapshot())
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            self.assertTrue(
                service.call_tool(
                    "context.state.claim",
                    self.make_claim_request(),
                    context=context,
                )["ok"]
            )
            self.assertTrue(
                service.call_tool(
                    "context.state.effect",
                    self.make_effect_request(),
                    context=context,
                )["ok"]
            )
            response = service.call_tool(
                "context.state.effect",
                self.make_effect_request(
                    request_id="request-effect-operation-mismatch",
                    action="complete",
                    revision=16,
                    result_ref="artifact://sha256/" + "f" * 64,
                    operation="deploy-production",
                ),
                context=context,
            )
            stored = store.read_project("project-dedupe")
            events = store.read_events("project-dedupe")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "integrity")
        self.assertEqual(stored["project"]["revision"], 16)
        self.assertEqual(len(events), 2)
        self.assertEqual(stored["effects"][0]["status"], "authorized")

    @unittest.skipUnless(
        os.environ.get("CONTEXT_TEST_POSTGRES_DSN"),
        "CONTEXT_TEST_POSTGRES_DSN is required for State MCP adapter parity",
    )
    def test_sqlite_and_postgresql_produce_the_same_state_mcp_receipts(self):
        project_id = f"project-mcp-parity-{uuid.uuid4().hex}"
        snapshot = self.make_ready_snapshot()
        snapshot["project"]["project_id"] = project_id
        context = RequestContext(
            subject_ref="actor-second",
            authorization_ref="authorization-test",
        )
        requests = (
            ("context.state.read", {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-parity-read",
                "project_id": project_id,
            }),
            ("context.state.claim", self.make_claim_request(
                request_id="request-parity-claim",
                project_id=project_id,
            )),
            ("context.state.effect", self.make_effect_request(
                request_id="request-parity-authorize",
                project_id=project_id,
                claim_id="claim-request-parity-claim",
            )),
            ("context.state.effect", self.make_effect_request(
                request_id="request-parity-complete",
                action="complete",
                revision=16,
                result_ref="artifact://sha256/" + "e" * 64,
                project_id=project_id,
                claim_id="claim-request-parity-claim",
            )),
            ("context.state.commit", self.make_commit_request(
                request_id="request-parity-commit",
                revision=17,
                project_id=project_id,
            )),
            ("context.state.commit", self.make_commit_request(
                request_id="request-parity-stale",
                revision=17,
                project_id=project_id,
            )),
        )

        def run_flow(store):
            store.initialize()
            store.create_project(copy.deepcopy(snapshot))
            service = self.make_service(store, authorizer=_AllowAuthorizer())
            receipts = [
                service.call_tool(tool, copy.deepcopy(request), context=context)
                for tool, request in requests
            ]
            for receipt in receipts[:-1]:
                self.assertTrue(receipt["ok"], receipt["error"])
                receipt["result"].pop("capabilities")
            self.assertFalse(receipts[-1]["ok"])
            self.assertEqual(receipts[-1]["error"]["code"], "conflict")
            return receipts, store.read_project(project_id), store.read_events(project_id)

        with tempfile.TemporaryDirectory() as directory:
            sqlite_result = run_flow(SQLiteStateStore(Path(directory) / "state.db"))
        postgres_result = run_flow(
            PostgresStateStore(os.environ["CONTEXT_TEST_POSTGRES_DSN"])
        )

        self.assertEqual(postgres_result, sqlite_result)
        self.assertEqual(sqlite_result[1]["project"]["revision"], 18)
        self.assertEqual(len(sqlite_result[2]), 4)

    def test_read_rejects_a_snapshot_and_event_head_from_different_revisions(self):
        context = RequestContext("actor-reader", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            source = SQLiteStateStore(Path(directory) / "source.db")
            source.initialize()
            source.create_project(copy.deepcopy(self.snapshot))
            commit = self.make_service(
                source,
                authorizer=_AllowAuthorizer(),
            ).call_tool(
                "context.state.commit",
                self.make_commit_request(request_id="request-torn-source"),
                context=RequestContext("actor-writer", "authorization-test"),
            )
            self.assertTrue(commit["ok"])
            event = commit["result"]["event"]

            class TornReadStore:
                capability_manifest = SQLiteStateStore.capability_manifest

                def initialize(self):
                    pass

                def create_project(self, snapshot):
                    pass

                def read_project(self, project_id):
                    return copy.deepcopy(self_snapshot)

                def read_events(self, project_id):
                    return [copy.deepcopy(event)]

                def commit_event(self, **kwargs):
                    raise AssertionError("read must not commit")

            self_snapshot = copy.deepcopy(self.snapshot)
            response = self.make_service(
                TornReadStore(),
                authorizer=_AllowAuthorizer(),
            ).call_tool(
                "context.state.read",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-torn-read",
                    "project_id": self.snapshot["project"]["project_id"],
                },
                context=context,
            )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "conflict")


if __name__ == "__main__":
    unittest.main()
