import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.effect_scope_gate import (
    evaluate_effect_scope_gate,
    scopes_overlap,
    validate_scope,
)
from context_control_plane.state_mcp import (
    RequestContext,
    StateMCPService,
    state_mcp_tool_definitions,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.typed_state import (
    TypedStateError,
    round_trip_typed_state,
    validate_typed_state,
)


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M304EffectScopeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        fixtures = yaml.safe_load(
            (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixtures["cases"]
                if case["case_id"] == "solo-active-work"
            )
        )

    def make_service(self, store):
        return StateMCPService(
            store,
            authorizer=_AllowAuthorizer(),
            registry_digest="a" * 64,
            clock=lambda: "2026-08-13T08:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

    def make_claim_request(self, *, request_id, work_id, revision, scopes):
        return {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": request_id,
            "project_id": self.snapshot["project"]["project_id"],
            "expected_revision": revision,
            "work_id": work_id,
            "claim_id": f"claim-{request_id}",
            "scope_owners": scopes,
            "lease_expires_at": "2026-08-13T09:00:00+08:00",
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }

    @staticmethod
    def as_v2(document):
        upgraded = copy.deepcopy(document)
        upgraded["schema_version"] = "context.typed-state/v2alpha1"
        active_work = upgraded["works"][0]
        active_work["parent_work_id"] = "goal-m3-04"
        for scope in (
            *active_work["scope_refs"],
            *upgraded["claims"][0]["scope_owners"],
            *[upgraded["effects"][0]["scope_ref"]],
        ):
            if scope["scope_kind"] in {"repo", "directory", "file", "symbol"}:
                scope["scope_ref"] = f"repo://control-plane/{scope['scope_ref']}"
        for work in upgraded["works"]:
            work.update(
                {
                    "return_point_work_id": None,
                    "exit_criteria": [],
                    "attempt_budget": None,
                    "expires_at": None,
                    "promotion_target_work_id": None,
                    "mainline_authority": True,
                }
            )
        common = {
            "status": "ready",
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": 1,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }
        upgraded["works"] = [
            {
                **common,
                "work_id": "campaign-m3-04",
                "kind": "campaign",
                "title": "M3-04 campaign",
                "parent_work_id": None,
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "m3-04/campaign"}
                ],
            },
            {
                **common,
                "work_id": "goal-m3-04",
                "kind": "goal",
                "title": "M3-04 goal",
                "parent_work_id": "campaign-m3-04",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "m3-04/goal"}
                ],
            },
            *upgraded["works"],
        ]
        return upgraded

    def test_scope_hierarchy_covers_repo_directory_file_and_symbol(self):
        cases = (
            (
                {"scope_kind": "repo", "scope_ref": "repo://control-plane"},
                {"scope_kind": "file", "scope_ref": "repo://control-plane/src/gate.py"},
                True,
            ),
            (
                {"scope_kind": "directory", "scope_ref": "repo://control-plane/src"},
                {"scope_kind": "file", "scope_ref": "repo://control-plane/src/gate.py"},
                True,
            ),
            (
                {"scope_kind": "file", "scope_ref": "repo://control-plane/src/gate.py"},
                {
                    "scope_kind": "symbol",
                    "scope_ref": "repo://control-plane/src/gate.py#authorize",
                },
                True,
            ),
            (
                {"scope_kind": "directory", "scope_ref": "repo://control-plane/src"},
                {"scope_kind": "file", "scope_ref": "repo://control-plane/src-old/gate.py"},
                False,
            ),
            (
                {"scope_kind": "capability", "scope_ref": "git/commit"},
                {"scope_kind": "capability", "scope_ref": "git/push"},
                False,
            ),
            (
                {"scope_kind": "effect", "scope_ref": "deploy/production"},
                {"scope_kind": "effect", "scope_ref": "deploy/production"},
                True,
            ),
        )
        for owner, requested, expected in cases:
            with self.subTest(owner=owner, requested=requested):
                self.assertEqual(scopes_overlap(owner, requested), expected)

    def test_scope_contract_rejects_ambiguous_or_escaping_refs(self):
        invalid = (
            {"scope_kind": "directory", "scope_ref": "src/../secrets"},
            {"scope_kind": "file", "scope_ref": "/absolute/file.py"},
            {"scope_kind": "file", "scope_ref": "src\\file.py"},
            {"scope_kind": "repo", "scope_ref": "src"},
            {"scope_kind": "directory", "scope_ref": "src"},
            {"scope_kind": "file", "scope_ref": "repo:/control-plane/src/core.py"},
            {"scope_kind": "file", "scope_ref": "repo:///control-plane/src/core.py"},
            {"scope_kind": "repo", "scope_ref": "repo:"},
            {"scope_kind": "file", "scope_ref": "other://control-plane/src/core.py"},
            {"scope_kind": "symbol", "scope_ref": "src/file.py#"},
            {"scope_kind": "file", "scope_ref": "src/file.py#symbol"},
            {"scope_kind": "unknown", "scope_ref": "src"},
        )
        for scope in invalid:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                validate_scope(scope)

    def test_state_mcp_rejects_noncanonical_scope_without_backend_write(self):
        snapshot = self.as_v2(self.snapshot)
        snapshot["effects"] = []
        active_work = next(
            item for item in snapshot["works"] if item["work_id"] == "work-solo"
        )
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-invalid-scope",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "action": "authorize",
            "effect_id": "effect-invalid-scope",
            "effect_key": "effect-key-invalid-scope",
            "work_id": active_work["work_id"],
            "claim_id": snapshot["claims"][0]["claim_id"],
            "operation": "write-file",
            "scope_ref": {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/../secret",
            },
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            response = self.make_service(store).call_tool(
                "context.state.effect",
                request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_verdict_schema_is_registered_hashed_and_strict(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.effect-scope-verdict"
        )
        schema_path = root / entry["artifact_path"]
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            entry["current_wire_version"],
            "context.effect-scope-verdict/v1alpha1",
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        self.assertFalse(schema["additionalProperties"])
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(
            {
                "schema_version": "context.effect-scope-verdict/v1alpha1",
                "decision": "deny",
                "read_only": True,
                "reason": "scope_conflict",
            }
        )

    def test_ten_thousand_gate_evaluations_are_deterministic_and_bounded(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["effects"] = []
        claim = snapshot["claims"][0]
        work = snapshot["works"][0]
        latencies = []
        reasons = set()
        for sample in range(10_000):
            requested_scope = (
                copy.deepcopy(claim["scope_owners"][0])
                if sample % 2 == 0
                else {"scope_kind": "file", "scope_ref": "src/unrelated.py"}
            )
            started = time.perf_counter_ns()
            verdict = evaluate_effect_scope_gate(
                snapshot,
                actor_ref=claim["actor_ref"],
                work_id=work["work_id"],
                claim_id=claim["claim_id"],
                expected_revision=snapshot["project"]["revision"],
                operation="write-file",
                requested_scope=requested_scope,
                effect_id=f"effect-{sample}",
                observed_at="2026-08-09T10:00:00+08:00",
            )
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            reasons.add(verdict["reason"])
        p95 = sorted(latencies)[9499]
        self.assertEqual(reasons, {"authorized", "scope_not_owned"})
        self.assertLess(p95, 1.0)

    def test_effect_gate_uses_one_contract_for_all_side_effect_classes(self):
        operation_scopes = (
            (
                "write-file",
                {"scope_kind": "file", "scope_ref": "repo://control-plane/src/core.py"},
            ),
            ("git-commit", {"scope_kind": "capability", "scope_ref": "git/commit"}),
            ("deploy", {"scope_kind": "effect", "scope_ref": "deploy/staging"}),
            ("external-effect", {"scope_kind": "effect", "scope_ref": "issue/create"}),
        )
        for operation, scope in operation_scopes:
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["effects"] = []
            work = snapshot["works"][0]
            claim = snapshot["claims"][0]
            work["scope_refs"] = [copy.deepcopy(scope)]
            claim["scope_owners"] = [copy.deepcopy(scope)]
            verdict = evaluate_effect_scope_gate(
                snapshot,
                actor_ref=claim["actor_ref"],
                work_id=work["work_id"],
                claim_id=claim["claim_id"],
                expected_revision=snapshot["project"]["revision"],
                operation=operation,
                requested_scope=scope,
            )
            with self.subTest(operation=operation):
                self.assertEqual(verdict["decision"], "allow")
                self.assertFalse(verdict["read_only"])

    def test_effect_operation_requires_its_declared_scope_class(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["effects"] = []
        work = snapshot["works"][0]
        claim = snapshot["claims"][0]
        file_scope = {
            "scope_kind": "file",
            "scope_ref": "repo://control-plane/src/core.py",
        }
        work["scope_refs"] = [copy.deepcopy(file_scope)]
        claim["scope_owners"] = [copy.deepcopy(file_scope)]
        for operation in ("deploy-production", "git-commit", "external-effect"):
            verdict = evaluate_effect_scope_gate(
                snapshot,
                actor_ref=claim["actor_ref"],
                work_id=work["work_id"],
                claim_id=claim["claim_id"],
                expected_revision=snapshot["project"]["revision"],
                operation=operation,
                requested_scope=file_scope,
            )
            with self.subTest(operation=operation):
                self.assertEqual(verdict["decision"], "deny")
                self.assertTrue(verdict["read_only"])
                self.assertEqual(verdict["reason"], "operation_scope_mismatch")

    def test_file_scope_does_not_cover_path_prefix_children(self):
        owner = {
            "scope_kind": "file",
            "scope_ref": "repo://control-plane/src/core.py",
        }
        child = {
            "scope_kind": "file",
            "scope_ref": "repo://control-plane/src/core.py/child.py",
        }
        self.assertFalse(scopes_overlap(owner, child))

    def test_legacy_v1_scope_replays_without_canonicalization_but_v2_rejects_it(self):
        legacy = copy.deepcopy(self.snapshot)
        legacy_scope = {"scope_kind": "file", "scope_ref": "C:\\legacy\\src\\core.py"}
        legacy["works"][0]["scope_refs"] = [copy.deepcopy(legacy_scope)]
        legacy["claims"][0]["scope_owners"] = [copy.deepcopy(legacy_scope)]
        legacy["effects"][0]["scope_ref"] = copy.deepcopy(legacy_scope)
        validate_typed_state(legacy)
        self.assertEqual(round_trip_typed_state(legacy), legacy)

        current = self.as_v2(legacy)
        with self.assertRaisesRegex(TypedStateError, "not canonical"):
            validate_typed_state(current)

    def test_effect_gate_preflight_returns_structured_read_only_verdict_without_mutation(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["effects"] = []
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-gate-deploy-with-file",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "effect_id": "effect-gate-deploy-with-file",
            "work_id": snapshot["works"][0]["work_id"],
            "claim_id": snapshot["claims"][0]["claim_id"],
            "operation": "deploy-production",
            "scope_ref": {"scope_kind": "file", "scope_ref": "src/core.py"},
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            response = self.make_service(store).call_tool(
                "context.state.effect.gate",
                request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])
        self.assertIn(
            "context.state.effect.gate",
            {item["name"] for item in state_mcp_tool_definitions()},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["verdict"]["decision"], "deny")
        self.assertTrue(response["result"]["verdict"]["read_only"])
        self.assertEqual(response["result"]["verdict"]["reason"], "operation_scope_mismatch")
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_pending_effect_overlap_and_expired_claim_are_read_only(self):
        snapshot = copy.deepcopy(self.snapshot)
        claim = snapshot["claims"][0]
        work = snapshot["works"][0]
        base = {
            "actor_ref": claim["actor_ref"],
            "work_id": work["work_id"],
            "claim_id": claim["claim_id"],
            "expected_revision": snapshot["project"]["revision"],
            "operation": "write-file",
            "requested_scope": copy.deepcopy(claim["scope_owners"][0]),
        }
        overlap = evaluate_effect_scope_gate(
            snapshot,
            effect_id="effect-other",
            observed_at="2026-08-09T10:00:00+08:00",
            **base,
        )
        expired = evaluate_effect_scope_gate(
            snapshot,
            effect_id="effect-solo",
            observed_at="2026-08-09T11:00:00+08:00",
            **base,
        )
        self.assertEqual(overlap["reason"], "effect_scope_conflict")
        self.assertTrue(overlap["read_only"])
        self.assertEqual(expired["reason"], "claim_expired")
        self.assertTrue(expired["read_only"])

    def test_completion_of_already_authorized_effect_does_not_reauthorize_expired_lease(self):
        snapshot = copy.deepcopy(self.snapshot)
        effect = snapshot["effects"][0]
        from context_control_plane.effect_scope_gate import evaluate_effect_completion_gate

        verdict = evaluate_effect_completion_gate(
            snapshot,
            actor_ref="actor-owner",
            work_id=effect["work_id"],
            claim_id=effect["claim_id"],
            expected_revision=snapshot["project"]["revision"],
            effect_id=effect["effect_id"],
            effect_key=effect["effect_key"],
            operation=effect["operation"],
            requested_scope=effect["scope_ref"],
        )
        self.assertEqual(verdict["decision"], "allow")
        self.assertFalse(verdict["read_only"])

    def test_completion_binds_the_authorized_claim_actor(self):
        snapshot = copy.deepcopy(self.snapshot)
        effect = snapshot["effects"][0]
        snapshot["project"]["updated_at"] = "2026-08-13T08:00:00+08:00"
        snapshot["claims"][0]["claimed_at"] = "2026-08-13T07:00:00+08:00"
        snapshot["claims"][0]["lease_expires_at"] = "2026-08-13T09:00:00+08:00"
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-complete-actor-bound",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "action": "complete",
            "effect_id": effect["effect_id"],
            "effect_key": effect["effect_key"],
            "work_id": effect["work_id"],
            "claim_id": effect["claim_id"],
            "operation": effect["operation"],
            "scope_ref": copy.deepcopy(effect["scope_ref"]),
            "result_ref": "artifact://sha256/" + "2" * 64,
            "evidence_ids": [],
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            service = self.make_service(store)
            rejected = service.call_tool(
                "context.state.effect",
                request,
                context=RequestContext("actor-other", "authorization-test"),
            )
            self.assertFalse(rejected["ok"])
            self.assertIn("completion_actor_mismatch", rejected["error"]["message"])
            self.assertEqual(store.read_project(request["project_id"]), snapshot)
            self.assertEqual(store.read_events(request["project_id"]), [])

            accepted = service.call_tool(
                "context.state.effect",
                {**request, "request_id": "effect-complete-owner"},
                context=RequestContext("actor-owner", "authorization-test"),
            )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["result"]["snapshot"]["effects"][0]["status"], "succeeded")

    def test_typed_state_uses_hierarchy_for_claim_effect_and_overlap(self):
        valid = self.as_v2(self.snapshot)
        active_work = next(
            item for item in valid["works"] if item["work_id"] == "work-solo"
        )
        active_work["scope_refs"] = [
            {"scope_kind": "directory", "scope_ref": "repo://control-plane/src"}
        ]
        valid["claims"][0]["scope_owners"] = [
            {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/core.py",
            }
        ]
        valid["effects"][0]["scope_ref"] = {
            "scope_kind": "symbol",
            "scope_ref": "repo://control-plane/src/core.py#authorize",
        }
        validate_typed_state(valid)

        conflict = copy.deepcopy(valid)
        second_work = copy.deepcopy(
            next(item for item in conflict["works"] if item["work_id"] == "work-solo")
        )
        second_work.update(
            {
                "work_id": "work-second",
                "owner_refs": ["actor-second"],
                "scope_refs": [
                    {
                        "scope_kind": "directory",
                        "scope_ref": "repo://control-plane/src/core.py",
                    }
                ],
            }
        )
        second_claim = copy.deepcopy(conflict["claims"][0])
        second_claim.update(
            {
                "claim_id": "claim-second",
                "work_id": "work-second",
                "actor_ref": "actor-second",
                "scope_owners": [
                    {
                        "scope_kind": "directory",
                        "scope_ref": "repo://control-plane/src/core.py",
                    }
                ],
            }
        )
        conflict["works"].append(second_work)
        conflict["claims"].append(second_claim)
        conflict["project"]["active_work_ids"].append("work-second")
        conflict["project"]["primary_work_id"] = "work-second"
        with self.assertRaisesRegex(TypedStateError, "scope ownership conflict"):
            validate_typed_state(conflict)

        effect_conflict = copy.deepcopy(valid)
        second_effect = copy.deepcopy(effect_conflict["effects"][0])
        second_effect.update(
            {
                "effect_id": "effect-second",
                "effect_key": "effect-key-second",
                "scope_ref": {
                    "scope_kind": "file",
                    "scope_ref": "repo://control-plane/src/core.py",
                },
                "sequence_no": 2,
            }
        )
        effect_conflict["effects"].append(second_effect)
        with self.assertRaisesRegex(TypedStateError, "pending effect scope conflict"):
            validate_typed_state(effect_conflict)

    def test_v2_typed_state_replays_legacy_effect_scope_provenance(self):
        historical = self.as_v2(self.snapshot)
        historical["effects"][0]["operation"] = "deploy-production"
        historical["effects"][0]["scope_ref"] = {
            "scope_kind": "file",
            "scope_ref": "repo://control-plane/src/core.py",
        }
        validate_typed_state(historical)

    def test_expired_authorized_effect_completes_with_atomic_claim_recovery(self):
        snapshot = self.as_v2(self.snapshot)
        snapshot["project"]["updated_at"] = "2026-08-13T07:00:00+08:00"
        claim = snapshot["claims"][0]
        claim["claimed_at"] = "2026-08-13T06:00:00+08:00"
        claim["lease_expires_at"] = "2026-08-13T07:30:00+08:00"
        effect = snapshot["effects"][0]
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-complete-after-expiry",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "action": "complete",
            "effect_id": effect["effect_id"],
            "effect_key": effect["effect_key"],
            "work_id": effect["work_id"],
            "claim_id": effect["claim_id"],
            "operation": effect["operation"],
            "scope_ref": copy.deepcopy(effect["scope_ref"]),
            "result_ref": "artifact://sha256/" + "3" * 64,
            "evidence_ids": [],
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-13T08:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            response = service.call_tool(
                "context.state.effect",
                request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertTrue(response["ok"], response)
        self.assertEqual(stored["effects"][0]["status"], "succeeded")
        self.assertEqual(stored["claims"][0]["status"], "expired")
        self.assertEqual(stored["claims"][0]["released_at"], "2026-08-13T08:00:00+08:00")
        self.assertEqual(stored["works"][-1]["status"], "verifying")
        self.assertEqual(stored["project"]["active_work_ids"], [])
        self.assertEqual(
            {change["collection"] for change in events[0]["changes"]},
            {"effects", "claims", "works"},
        )

    def test_state_mcp_authorizes_descendant_file_and_denies_after_lease_expiry(self):
        snapshot = self.as_v2(self.snapshot)
        work = next(item for item in snapshot["works"] if item["work_id"] == "work-solo")
        claim = snapshot["claims"][0]
        snapshot["effects"] = []
        work["scope_refs"] = [
            {"scope_kind": "directory", "scope_ref": "repo://control-plane/src"}
        ]
        claim["scope_owners"] = [
            {"scope_kind": "file", "scope_ref": "repo://control-plane/src/core.py"}
        ]
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-descendant",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "action": "authorize",
            "effect_id": "effect-descendant",
            "effect_key": "effect-key-descendant",
            "work_id": work["work_id"],
            "claim_id": claim["claim_id"],
            "operation": "write-file",
            "scope_ref": {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/core.py",
            },
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            allowed_service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-09T10:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            allowed = allowed_service.call_tool(
                "context.state.effect",
                request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            expired_request = {
                **request,
                "request_id": "effect-expired",
                "effect_id": "effect-expired",
                "effect_key": "effect-key-expired",
                "expected_revision": snapshot["project"]["revision"] + 1,
                "scope_ref": {
                    "scope_kind": "file",
                    "scope_ref": "repo://control-plane/src/other.py",
                },
            }
            expired_service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-09T11:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            expired = expired_service.call_tool(
                "context.state.effect",
                expired_request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertTrue(allowed["ok"], allowed["error"])
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["error"]["code"], "conflict")
        self.assertIn("read-only: claim_expired", expired["error"]["message"])
        self.assertEqual(stored["project"]["revision"], snapshot["project"]["revision"] + 1)
        self.assertEqual(len(events), 1)

    def test_state_mcp_rejects_second_pending_effect_on_overlapping_scope(self):
        snapshot = self.as_v2(self.snapshot)
        work = next(item for item in snapshot["works"] if item["work_id"] == "work-solo")
        claim = snapshot["claims"][0]
        work["scope_refs"] = [
            {"scope_kind": "directory", "scope_ref": "repo://control-plane/src"}
        ]
        claim["scope_owners"] = [
            {"scope_kind": "file", "scope_ref": "repo://control-plane/src/core.py"}
        ]
        snapshot["effects"][0]["scope_ref"] = {
            "scope_kind": "file",
            "scope_ref": "repo://control-plane/src/core.py",
        }
        request = {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "effect-overlap",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
            "action": "authorize",
            "effect_id": "effect-overlap",
            "effect_key": "effect-key-overlap",
            "work_id": work["work_id"],
            "claim_id": claim["claim_id"],
            "operation": "write-file",
            "scope_ref": {
                "scope_kind": "symbol",
                "scope_ref": "repo://control-plane/src/core.py#authorize",
            },
            "result_ref": None,
            "evidence_ids": [],
            "causation_ref": "work:M3-04",
            "correlation_ref": "campaign:M3",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            response = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-09T10:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            ).call_tool(
                "context.state.effect",
                request,
                context=RequestContext("actor-owner", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])
        self.assertEqual(response["error"]["code"], "conflict")
        self.assertIn("effect_scope_conflict", response["error"]["message"])
        self.assertEqual(stored, snapshot)
        self.assertEqual(events, [])

    def test_gate_returns_read_only_verdict_for_each_authority_mismatch(self):
        snapshot = copy.deepcopy(self.snapshot)
        claim = snapshot["claims"][0]
        work = snapshot["works"][0]
        base = {
            "actor_ref": claim["actor_ref"],
            "work_id": work["work_id"],
            "claim_id": claim["claim_id"],
            "expected_revision": snapshot["project"]["revision"],
            "operation": "write-file",
            "requested_scope": copy.deepcopy(claim["scope_owners"][0]),
        }
        mutations = (
            ("stale_revision", {"expected_revision": base["expected_revision"] - 1}),
            ("actor_mismatch", {"actor_ref": "actor-other"}),
            ("inactive_work", {"work_id": "work-completed"}),
            ("claim_mismatch", {"claim_id": "claim-missing"}),
            (
                "scope_not_owned",
                {
                    "requested_scope": {
                        "scope_kind": "file",
                        "scope_ref": "src/unrelated.py",
                    }
                },
            ),
        )
        for reason, update in mutations:
            arguments = {**base, **update}
            verdict = evaluate_effect_scope_gate(snapshot, **arguments)
            with self.subTest(reason=reason):
                self.assertEqual(verdict["decision"], "deny")
                self.assertTrue(verdict["read_only"])
                self.assertEqual(verdict["reason"], reason)

    def test_claim_rejects_another_work_with_overlapping_scope_before_event(self):
        snapshot = self.as_v2(self.snapshot)
        active_work = snapshot["works"][0]
        ready_work = copy.deepcopy(active_work)
        ready_work.update(
            {
                "work_id": "work-ready",
                "status": "ready",
                "dedupe_status": "clear",
                "overlap_candidate_ids": [],
                "blocker_ids": [],
                "owner_refs": ["actor-second"],
                "scope_refs": [
                    {
                        "scope_kind": "directory",
                        "scope_ref": "repo://control-plane/src",
                    }
                ],
            }
        )
        snapshot["works"].append(ready_work)
        active_work["scope_refs"] = [
            {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/core.py",
            }
        ]
        snapshot["claims"][0]["scope_owners"] = copy.deepcopy(active_work["scope_refs"])
        request = self.make_claim_request(
            request_id="overlap",
            work_id=ready_work["work_id"],
            revision=snapshot["project"]["revision"],
            scopes=copy.deepcopy(ready_work["scope_refs"]),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.db")
            store.initialize()
            store.create_project(snapshot)
            response = self.make_service(store).call_tool(
                "context.state.claim",
                request,
                context=RequestContext("actor-second", "authorization-test"),
            )
            stored = store.read_project(request["project_id"])
            events = store.read_events(request["project_id"])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "conflict")
        self.assertIn("read-only: scope_conflict", response["error"]["message"])
        self.assertEqual(stored["project"]["revision"], snapshot["project"]["revision"])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
