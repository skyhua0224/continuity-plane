import copy
import hashlib
import inspect
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane import mcp_admission
from context_control_plane.mcp_admission import (
    MCPAdmissionError,
    decide_mcp_admission,
    validate_mcp_admission_decision,
    validate_mcp_registry_snapshot,
)


def _digest(document: dict, field: str) -> str:
    body = copy.deepcopy(document)
    body.pop(field, None)
    payload = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _seal_snapshot(snapshot: dict) -> dict:
    snapshot["registry_sha256"] = mcp_admission.mcp_registry_snapshot_sha256(snapshot)
    return snapshot


def _seal_decision(decision: dict) -> dict:
    decision["decision_sha256"] = _digest(decision, "decision_sha256")
    return decision


class M606MCPAdmissionTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    def snapshot(self) -> dict:
        return _seal_snapshot({
            "schema_version": "context.mcp-registry-snapshot/v1alpha1",
            "snapshot_id": "mcp-registry/fixture/m6-06",
            "registry_kind": "fixture",
            "registry_url": "https://fixtures.context-control-plane.invalid/mcp/m6-06",
            "registry_revision": "f36b7dd4afe2d540a4ceb9b64d3627085bf5db03",
            "registry_sha256": "",
            "retrieved_at": "2026-08-15T06:30:00Z",
            "servers": [
                {
                    "server_id": "fixture/reference-server",
                    "publisher": "fixture-publisher",
                    "publisher_verified": True,
                    "license_ref": "Apache-2.0",
                    "license_verified": True,
                    "source_url": "https://example.invalid/reference-server",
                    "source_revision": "2" * 40,
                    "source_sha256": "3" * 64,
                    "auth_kind": "token",
                    "tools": [
                        {
                            "name": "reference.search",
                            "scope": "read",
                            "requires_state_mcp": False,
                        },
                        {
                            "name": "evidence.record",
                            "scope": "evidence_write",
                            "requires_state_mcp": False,
                        },
                        {
                            "name": "state.commit",
                            "scope": "state_write",
                            "requires_state_mcp": True,
                        },
                        {
                            "name": "issue.create",
                            "scope": "external_effect",
                            "requires_state_mcp": True,
                        },
                    ],
                }
            ],
        })

    def request(self, snapshot: dict | None = None) -> dict:
        trusted = snapshot or self.snapshot()
        return {
            "request_id": "mcp-admission/m6-06/read",
            "project_id": "context-control-plane",
            "operation_id": "operation/m6-06/research",
            "requested_server_ids": ["fixture/reference-server"],
            "granted_scopes": ["read"],
            "available_auth_kinds": ["token"],
            "allow_external_effects": False,
            "state_mcp_route": None,
            "expected_registry_revision": trusted["registry_revision"],
            "expected_registry_sha256": trusted["registry_sha256"],
        }

    def trusted_state_mcp_routes(self) -> dict[str, str]:
        return {
            "context-control-plane": "state-mcp://project/context-control-plane"
        }

    def test_read_tool_is_admitted_from_fixed_snapshot(self) -> None:
        decision = decide_mcp_admission(self.snapshot(), self.request())
        validate_mcp_admission_decision(decision)
        active = decision["servers"][0]["active_tools"]
        self.assertEqual([tool["name"] for tool in active], ["reference.search"])
        self.assertFalse(decision["state_write_authority"])
        self.assertFalse(decision["tool_invocation_performed"])

    def test_unauthorized_write_tools_never_activate(self) -> None:
        for index in range(1000):
            request = self.request()
            request["request_id"] = f"mcp-admission/m6-06/unauthorized-{index}"
            decision = decide_mcp_admission(self.snapshot(), request)
            active_scopes = {
                tool["scope"]
                for server in decision["servers"]
                for tool in server["active_tools"]
            }
            self.assertNotIn("state_write", active_scopes)
            self.assertNotIn("external_effect", active_scopes)

    def test_authorized_state_tool_is_routed_through_state_mcp(self) -> None:
        request = self.request()
        request["granted_scopes"] = ["read", "state_write"]
        request["state_mcp_route"] = "state-mcp://project/context-control-plane"
        decision = decide_mcp_admission(
            self.snapshot(),
            request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        state_tool = next(
            tool
            for tool in decision["servers"][0]["active_tools"]
            if tool["scope"] == "state_write"
        )
        self.assertEqual(state_tool["invocation_route"], request["state_mcp_route"])
        self.assertFalse(decision["state_write_authority"])

    def test_state_mcp_route_requires_trusted_project_binding(self) -> None:
        snapshot = self.snapshot()
        request = self.request(snapshot)
        request["granted_scopes"] = ["state_write"]
        request["state_mcp_route"] = "state-mcp://project/attacker-project"

        with self.assertRaisesRegex(MCPAdmissionError, "trusted|project"):
            decide_mcp_admission(snapshot, request)

        request["state_mcp_route"] = "state-mcp://project/context-control-plane"
        decision = decide_mcp_admission(
            snapshot,
            request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        self.assertRegex(decision["state_mcp_route_anchor_sha256"], r"^[0-9a-f]{64}$")
        validate_mcp_admission_decision(
            decision,
            expected_request=request,
            expected_snapshot=snapshot,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        with self.assertRaisesRegex(MCPAdmissionError, "trusted"):
            validate_mcp_admission_decision(
                decision,
                expected_request=request,
                expected_snapshot=snapshot,
            )

    def test_external_effect_needs_separate_boolean_gate(self) -> None:
        request = self.request()
        request["granted_scopes"] = ["read", "external_effect"]
        request["state_mcp_route"] = "state-mcp://project/context-control-plane"
        denied = decide_mcp_admission(
            self.snapshot(),
            request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        self.assertNotIn(
            "external_effect",
            {tool["scope"] for tool in denied["servers"][0]["active_tools"]},
        )
        request["allow_external_effects"] = True
        admitted = decide_mcp_admission(
            self.snapshot(),
            request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        self.assertIn(
            "external_effect",
            {tool["scope"] for tool in admitted["servers"][0]["active_tools"]},
        )

    def test_unverified_publisher_or_license_is_quarantined(self) -> None:
        for field in ("publisher_verified", "license_verified"):
            with self.subTest(field=field):
                snapshot = self.snapshot()
                snapshot["servers"][0][field] = False
                _seal_snapshot(snapshot)
                decision = decide_mcp_admission(snapshot, self.request(snapshot))
                self.assertEqual(decision["servers"][0]["status"], "quarantined")
                self.assertEqual(decision["servers"][0]["active_tools"], [])

    def test_snapshot_kind_and_canonical_digest_are_verified(self) -> None:
        snapshot = self.snapshot()
        self.assertEqual(snapshot["registry_kind"], "fixture")
        self.assertTrue(hasattr(mcp_admission, "mcp_registry_snapshot_sha256"))
        self.assertEqual(
            mcp_admission.mcp_registry_snapshot_sha256(snapshot),
            snapshot["registry_sha256"],
        )
        validate_mcp_registry_snapshot(snapshot)

        for mutation in (
            lambda value: value.update(registry_kind="community"),
            lambda value: value.update(registry_sha256="0" * 64),
            lambda value: value["servers"][0].update(publisher="mutated"),
        ):
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(snapshot)
                mutation(invalid)
                with self.assertRaises(MCPAdmissionError):
                    validate_mcp_registry_snapshot(invalid)

    def test_official_registry_requires_explicit_trusted_anchor(self) -> None:
        snapshot = self.snapshot()
        snapshot["registry_kind"] = "official"
        _seal_snapshot(snapshot)
        request = self.request(snapshot)

        with self.assertRaisesRegex(MCPAdmissionError, "trusted|official"):
            decide_mcp_admission(snapshot, request)

        anchor = {
            "registry_kind": snapshot["registry_kind"],
            "registry_url": snapshot["registry_url"],
            "registry_revision": snapshot["registry_revision"],
            "registry_sha256": snapshot["registry_sha256"],
        }
        decision = decide_mcp_admission(
            snapshot,
            request,
            trusted_registry_anchor=anchor,
        )
        self.assertRegex(decision["registry_anchor_sha256"], r"^[0-9a-f]{64}$")
        validate_mcp_admission_decision(
            decision,
            expected_request=request,
            expected_snapshot=snapshot,
            trusted_registry_anchor=anchor,
        )
        with self.assertRaisesRegex(MCPAdmissionError, "trusted|official"):
            validate_mcp_admission_decision(
                decision,
                expected_request=request,
                expected_snapshot=snapshot,
            )

        attacker = copy.deepcopy(snapshot)
        attacker["registry_url"] = "https://attacker.invalid/registry"
        _seal_snapshot(attacker)
        attacker_request = self.request(attacker)
        with self.assertRaisesRegex(MCPAdmissionError, "anchor|registry"):
            decide_mcp_admission(
                attacker,
                attacker_request,
                trusted_registry_anchor=anchor,
            )

    def test_trusted_digest_rejects_resealed_registry_mutation(self) -> None:
        trusted = self.snapshot()
        request = self.request(trusted)
        mutations = {
            "publisher": lambda value: value["servers"][0].update(publisher="attacker"),
            "source": lambda value: value["servers"][0].update(
                source_url="https://attacker.invalid/server"
            ),
            "tool": lambda value: value["servers"][0]["tools"][0].update(
                name="attacker.search"
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                resealed = copy.deepcopy(trusted)
                mutation(resealed)
                _seal_snapshot(resealed)
                with self.assertRaisesRegex(MCPAdmissionError, "digest mismatch"):
                    decide_mcp_admission(resealed, request)

    def test_decision_binds_registry_kind_and_digest(self) -> None:
        snapshot = self.snapshot()
        decision = decide_mcp_admission(snapshot, self.request(snapshot))
        self.assertEqual(decision["registry_kind"], snapshot["registry_kind"])
        self.assertEqual(decision["registry_sha256"], snapshot["registry_sha256"])

    def test_decision_persists_request_digest_and_replays_expected_inputs(self) -> None:
        snapshot = self.snapshot()
        request = self.request(snapshot)
        decision = decide_mcp_admission(snapshot, request)

        self.assertIn("request_sha256", decision)
        self.assertTrue(hasattr(mcp_admission, "mcp_admission_request_sha256"))
        self.assertEqual(
            decision["request_sha256"],
            mcp_admission.mcp_admission_request_sha256(request),
        )
        self.assertIn(
            "expected_request",
            inspect.signature(validate_mcp_admission_decision).parameters,
        )
        self.assertIn(
            "expected_snapshot",
            inspect.signature(validate_mcp_admission_decision).parameters,
        )
        validate_mcp_admission_decision(
            decision,
            expected_request=request,
            expected_snapshot=snapshot,
        )

    def test_replay_rejects_resealed_whole_identity_replacement(self) -> None:
        trusted_snapshot = self.snapshot()
        trusted_request = self.request(trusted_snapshot)
        attacker_snapshot = copy.deepcopy(trusted_snapshot)
        attacker_snapshot["snapshot_id"] = "mcp-registry/fixture/attacker"
        attacker_snapshot["registry_revision"] = "9" * 40
        attacker_snapshot["servers"][0]["server_id"] = "fixture/attacker-server"
        _seal_snapshot(attacker_snapshot)
        attacker_request = self.request(attacker_snapshot)
        attacker_request.update(
            request_id="mcp-admission/m6-06/attacker",
            project_id="attacker-project",
            operation_id="operation/m6-06/attacker",
            requested_server_ids=["fixture/attacker-server"],
        )
        forged = decide_mcp_admission(attacker_snapshot, attacker_request)

        self.assertIn(
            "expected_request",
            inspect.signature(validate_mcp_admission_decision).parameters,
        )
        with self.assertRaisesRegex(MCPAdmissionError, "request|registry|replay"):
            validate_mcp_admission_decision(
                forged,
                expected_request=trusted_request,
                expected_snapshot=trusted_snapshot,
            )

    def test_replay_rejects_resealed_server_tool_omission(self) -> None:
        snapshot = self.snapshot()
        request = self.request(snapshot)
        decision = decide_mcp_admission(snapshot, request)
        decision["servers"][0]["denied_tools"].pop()
        _seal_decision(decision)

        self.assertIn(
            "expected_snapshot",
            inspect.signature(validate_mcp_admission_decision).parameters,
        )
        with self.assertRaisesRegex(MCPAdmissionError, "server|tool|replay"):
            validate_mcp_admission_decision(
                decision,
                expected_request=request,
                expected_snapshot=snapshot,
            )

    def test_snapshot_replay_accepts_interleaved_active_and_denied_tools(self) -> None:
        snapshot = self.snapshot()
        tools = snapshot["servers"][0]["tools"]
        tools[0], tools[1] = tools[1], tools[0]
        _seal_snapshot(snapshot)
        decision = decide_mcp_admission(snapshot, self.request(snapshot))

        validate_mcp_admission_decision(decision, expected_snapshot=snapshot)

    def test_registry_content_digest_excludes_retrieval_time(self) -> None:
        first = self.snapshot()
        second = copy.deepcopy(first)
        second["retrieved_at"] = "2026-08-15T07:30:00Z"
        _seal_snapshot(second)

        self.assertEqual(first["registry_sha256"], second["registry_sha256"])
        first_decision = decide_mcp_admission(first, self.request(first))
        second_decision = decide_mcp_admission(second, self.request(second))
        self.assertNotEqual(first_decision["decision_sha256"], second_decision["decision_sha256"])

    def test_decision_status_and_identity_invariants_fail_closed(self) -> None:
        snapshot = self.snapshot()
        admitted = decide_mcp_admission(snapshot, self.request(snapshot))

        invalid = copy.deepcopy(admitted)
        invalid["servers"][0]["reasons"] = ["publisher_unverified"]
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(_seal_decision(invalid))

        quarantined_snapshot = self.snapshot()
        quarantined_snapshot["servers"][0]["publisher_verified"] = False
        _seal_snapshot(quarantined_snapshot)
        quarantined = decide_mcp_admission(
            quarantined_snapshot, self.request(quarantined_snapshot)
        )
        invalid = copy.deepcopy(quarantined)
        invalid["servers"][0]["active_tools"] = [
            {
                "name": "reference.search",
                "scope": "read",
                "invocation_route": "mcp://fixture/reference-server/reference.search",
            }
        ]
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(_seal_decision(invalid))

        invalid = copy.deepcopy(admitted)
        invalid["servers"].append(copy.deepcopy(invalid["servers"][0]))
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(_seal_decision(invalid))

        invalid = copy.deepcopy(admitted)
        invalid["servers"][0]["denied_tools"][0]["name"] = "reference.search"
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(_seal_decision(invalid))

    def test_decision_scope_routes_and_external_effect_gate_fail_closed(self) -> None:
        cases = (
            ("read", "state-mcp://project/context-control-plane"),
            ("evidence_write", "state-mcp://project/context-control-plane"),
            ("state_write", "mcp://fixture/reference-server/state.commit"),
            ("external_effect", "mcp://fixture/reference-server/issue.create"),
        )
        for scope, invalid_route in cases:
            with self.subTest(scope=scope):
                snapshot = self.snapshot()
                request = self.request(snapshot)
                request["granted_scopes"] = [scope]
                if scope in {"state_write", "external_effect"}:
                    request["state_mcp_route"] = "state-mcp://project/context-control-plane"
                if scope == "external_effect":
                    request["allow_external_effects"] = True
                decision = decide_mcp_admission(
                    snapshot,
                    request,
                    trusted_state_mcp_routes=(
                        self.trusted_state_mcp_routes()
                        if request["state_mcp_route"] is not None
                        else None
                    ),
                )
                active = next(
                    tool
                    for tool in decision["servers"][0]["active_tools"]
                    if tool["scope"] == scope
                )
                active["invocation_route"] = invalid_route
                with self.assertRaises(MCPAdmissionError):
                    validate_mcp_admission_decision(_seal_decision(decision))

        snapshot = self.snapshot()
        request = self.request(snapshot)
        request["granted_scopes"] = ["external_effect"]
        request["allow_external_effects"] = True
        request["state_mcp_route"] = "state-mcp://project/context-control-plane"
        decision = decide_mcp_admission(
            snapshot,
            request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        decision["allow_external_effects"] = False
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(_seal_decision(decision))

    def test_schemas_reject_status_scope_route_and_gate_mismatches(self) -> None:
        snapshot_schema = json.loads(
            (self.root / "schemas/m6-06/mcp-registry-snapshot.schema.json").read_text()
        )
        decision_schema = json.loads(
            (self.root / "schemas/m6-06/mcp-admission-decision.schema.json").read_text()
        )
        snapshot_validator = Draft202012Validator(
            snapshot_schema, format_checker=FormatChecker()
        )
        decision_validator = Draft202012Validator(
            decision_schema, format_checker=FormatChecker()
        )
        self.assertIn("request_sha256", decision_schema["required"])
        self.assertIn("state_mcp_route_anchor_sha256", decision_schema["required"])
        self.assertIn("registry_anchor_sha256", decision_schema["required"])
        snapshot = self.snapshot()
        decision = decide_mcp_admission(snapshot, self.request(snapshot))
        self.assertEqual(list(snapshot_validator.iter_errors(snapshot)), [])
        self.assertEqual(list(decision_validator.iter_errors(decision)), [])

        snapshot_mutations = (
            lambda value: value.pop("registry_kind"),
            lambda value: value.update(registry_kind="community"),
            lambda value: value["servers"][0]["tools"][0].update(
                requires_state_mcp=True
            ),
            lambda value: value["servers"][0]["tools"][2].update(
                requires_state_mcp=False
            ),
        )
        for mutation in snapshot_mutations:
            with self.subTest(snapshot_mutation=mutation):
                invalid = copy.deepcopy(snapshot)
                mutation(invalid)
                self.assertTrue(list(snapshot_validator.iter_errors(invalid)))

        decision_mutations = (
            lambda value: value["servers"][0].update(
                reasons=["publisher_unverified"]
            ),
            lambda value: value["servers"][0]["active_tools"][0].update(
                invocation_route="state-mcp://project/context-control-plane"
            ),
        )
        for mutation in decision_mutations:
            with self.subTest(decision_mutation=mutation):
                invalid = copy.deepcopy(decision)
                mutation(invalid)
                _seal_decision(invalid)
                self.assertTrue(list(decision_validator.iter_errors(invalid)))

        effect_request = self.request(snapshot)
        effect_request["granted_scopes"] = ["external_effect"]
        effect_request["allow_external_effects"] = True
        effect_request["state_mcp_route"] = "state-mcp://project/context-control-plane"
        invalid = decide_mcp_admission(
            snapshot,
            effect_request,
            trusted_state_mcp_routes=self.trusted_state_mcp_routes(),
        )
        missing_route_anchor = copy.deepcopy(invalid)
        missing_route_anchor["state_mcp_route_anchor_sha256"] = None
        _seal_decision(missing_route_anchor)
        self.assertTrue(list(decision_validator.iter_errors(missing_route_anchor)))

        invalid["allow_external_effects"] = False
        _seal_decision(invalid)
        self.assertTrue(list(decision_validator.iter_errors(invalid)))

        official = self.snapshot()
        official["registry_kind"] = "official"
        _seal_snapshot(official)
        official_request = self.request(official)
        official_anchor = {
            "registry_kind": official["registry_kind"],
            "registry_url": official["registry_url"],
            "registry_revision": official["registry_revision"],
            "registry_sha256": official["registry_sha256"],
        }
        official_decision = decide_mcp_admission(
            official,
            official_request,
            trusted_registry_anchor=official_anchor,
        )
        official_decision["registry_anchor_sha256"] = None
        _seal_decision(official_decision)
        self.assertTrue(list(decision_validator.iter_errors(official_decision)))

    def test_snapshot_and_decision_are_strict(self) -> None:
        mutable = self.snapshot()
        mutable["registry_revision"] = "latest"
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_registry_snapshot(mutable)

        decision = decide_mcp_admission(self.snapshot(), self.request())
        invalid = copy.deepcopy(decision)
        invalid["invoked"] = True
        with self.assertRaises(MCPAdmissionError):
            validate_mcp_admission_decision(invalid)


if __name__ == "__main__":
    unittest.main()
