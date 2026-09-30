"""M9-01 external State MCP read projection contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionError,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
    typed_state_snapshot_sha256,
    validate_external_state_projection,
)
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext, StateMCPService


class _AllowAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return True


class _DenyAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return False


class _CountingSQLiteStore(SQLiteStateStore):
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


class _SourceStub:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def call_tool(self, tool: str, arguments: dict, *, context: RequestContext) -> dict:
        self.calls += 1
        return copy.deepcopy(self.response)


class _BoundSource(_SourceStub):
    def call_tool(self, tool: str, arguments: dict, *, context: RequestContext) -> dict:
        response = super().call_tool(tool, arguments, context=context)
        response["request_id"] = arguments["request_id"]
        return response


class M901ExternalStateProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        fixture_set = yaml.safe_load(
            (
                cls.root / "experiments/state/m2-01-core-fixtures.yaml"
            ).read_text(encoding="utf-8")
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "completed-work-overlap-blocked"
            )
        )
        cls.context = RequestContext(
            subject_ref="actor-docmost-reader",
            authorization_ref="authorization-docmost-reader",
        )
        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-01-test",
            secret=b"m9-01-external-projection-test-key",
        )

    def make_provider(
        self,
        store: SQLiteStateStore,
        *,
        authorizer: object | None = None,
    ) -> ExternalStateProjectionProvider:
        source = StateMCPService(
            store,
            authorizer=authorizer or _AllowAuthorizer(),
            registry_digest="a" * 64,
            clock=lambda: "2026-08-17T16:00:00+08:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )
        return ExternalStateProjectionProvider(
            source,
            provider_id="provider-docmost-reference",
            signer=self.signer,
        )

    def request(
        self,
        *,
        request_id: str = "request-m9-01-read",
        project_id: str | None = None,
        expected_revision: int | None = 14,
    ) -> dict:
        return {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": project_id or self.snapshot["project"]["project_id"],
            "expected_revision": expected_revision,
        }

    def test_authorized_read_returns_same_revision_read_only_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            response = self.make_provider(store).call_tool(
                EXTERNAL_READ_TOOL,
                self.request(),
                context=self.context,
            )

        self.assertTrue(response["ok"])
        projection = response["result"]
        self.assertEqual(projection["project_id"], self.snapshot["project"]["project_id"])
        self.assertEqual(projection["state_revision"], 14)
        self.assertEqual(projection["snapshot"]["project"]["revision"], 14)
        self.assertEqual(projection["source"]["tool"], "context.state.read")
        self.assertEqual(projection["source"]["revision"], 14)
        self.assertEqual(projection["source"]["registry_digest"], "a" * 64)
        self.assertRegex(projection["state_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(projection["projection_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(projection["state_write_authority"])
        self.assertFalse(projection["controlled_action_authority"])
        self.assertEqual(projection["provider_authority"], 0)
        self.assertEqual(projection["external_effect_authority"], 0)
        self.assertEqual(store.project_reads, 1)
        self.assertEqual(store.event_reads, 1)

    def test_unpinned_read_returns_current_revision_and_defensive_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            provider = self.make_provider(store)
            request = self.request(expected_revision=None)
            first = provider.call_tool(EXTERNAL_READ_TOOL, request, context=self.context)
            first["result"]["snapshot"]["project"]["revision"] = 999
            second = provider.call_tool(EXTERNAL_READ_TOOL, request, context=self.context)

        self.assertEqual(second["result"]["state_revision"], 14)
        self.assertEqual(second["result"]["snapshot"]["project"]["revision"], 14)
        self.assertEqual(
            first["result"]["state_sha256"], second["result"]["state_sha256"]
        )

    def test_stale_expected_revision_is_rejected_without_a_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            response = self.make_provider(store).call_tool(
                EXTERNAL_READ_TOOL,
                self.request(expected_revision=13),
                context=self.context,
            )

        self.assertFalse(response["ok"])
        self.assertIsNone(response["result"])
        self.assertEqual(response["error"]["code"], "stale_view")
        self.assertNotIn("14", response["error"]["message"])

    def test_denial_is_generic_and_happens_before_state_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            provider = self.make_provider(store, authorizer=_DenyAuthorizer())
            existing = provider.call_tool(
                EXTERNAL_READ_TOOL,
                self.request(request_id="request-denied-existing"),
                context=self.context,
            )
            unknown = provider.call_tool(
                EXTERNAL_READ_TOOL,
                self.request(
                    request_id="request-denied-unknown",
                    project_id="project-unknown",
                ),
                context=self.context,
            )

        self.assertEqual(existing["error"], unknown["error"])
        self.assertEqual(existing["error"]["code"], "permission_denied")
        self.assertEqual(store.project_reads, 0)
        self.assertEqual(store.event_reads, 0)

    def test_unknown_tool_and_invalid_request_never_reach_state_mcp(self) -> None:
        source = _SourceStub({})
        provider = ExternalStateProjectionProvider(
            source,
            provider_id="provider-docmost-reference",
            signer=self.signer,
        )
        unsupported = provider.call_tool(
            "context.external-state.write",
            self.request(),
            context=self.context,
        )
        invalid = provider.call_tool(
            EXTERNAL_READ_TOOL,
            {**self.request(), "unexpected": True},
            context=self.context,
        )

        self.assertEqual(unsupported["error"]["code"], "unsupported")
        self.assertEqual(invalid["error"]["code"], "invalid_request")
        self.assertEqual(source.calls, 0)

    def test_malformed_or_torn_source_response_fails_closed(self) -> None:
        source_response = {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": "source-request-m9-01-torn",
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": 15,
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {},
            },
            "error": None,
        }
        source = _SourceStub(source_response)
        provider = ExternalStateProjectionProvider(
            source,
            provider_id="provider-docmost-reference",
            signer=self.signer,
        )

        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            self.request(request_id="request-m9-01-torn"),
            context=self.context,
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "source_integrity_error")
        self.assertIsNone(response["result"])

    def test_long_valid_external_ids_use_a_bounded_source_request_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            source = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-17T16:00:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            provider = ExternalStateProjectionProvider(
                source,
                provider_id="p" * 256,
                signer=self.signer,
            )
            response = provider.call_tool(
                EXTERNAL_READ_TOOL,
                self.request(request_id="r" * 200),
                context=self.context,
            )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["state_revision"], 14)

    def test_oversized_request_id_is_rejected_before_source_call(self) -> None:
        source = _SourceStub({})
        provider = ExternalStateProjectionProvider(
            source,
            provider_id="provider-docmost-reference",
            signer=self.signer,
        )
        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            self.request(request_id="r" * 201),
            context=self.context,
        )

        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertIsNone(response["request_id"])
        self.assertEqual(source.calls, 0)

    def test_projection_validator_rejects_resigned_binding_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(copy.deepcopy(self.snapshot))
            projection = self.make_provider(store).call_tool(
                EXTERNAL_READ_TOOL,
                self.request(),
                context=self.context,
            )["result"]

        validate_external_state_projection(projection, signer=self.signer)
        mutations = [
            lambda item: item.__setitem__("state_revision", 15),
            lambda item: item["source"].__setitem__("revision", 15),
            lambda item: item["snapshot"]["project"].__setitem__("revision", 15),
            lambda item: item.__setitem__("state_write_authority", True),
            lambda item: item.__setitem__("state_sha256", "0" * 64),
        ]
        for mutate in mutations:
            forged = copy.deepcopy(projection)
            mutate(forged)
            body = {
                key: value
                for key, value in forged.items()
                if key not in {"projection_sha256", "signature"}
            }
            forged["projection_sha256"] = hashlib.sha256(
                json.dumps(
                    body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaises(ExternalStateProjectionError):
                validate_external_state_projection(forged, signer=self.signer)

        forged = copy.deepcopy(projection)
        forged["snapshot"]["works"][0]["title"] = "Coordinated malicious edit"
        forged["state_sha256"] = typed_state_snapshot_sha256(forged["snapshot"])
        body = {
            key: value
            for key, value in forged.items()
            if key not in {"projection_sha256", "signature"}
        }
        forged["projection_sha256"] = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(ExternalStateProjectionError):
            validate_external_state_projection(forged, signer=self.signer)

        wrong_signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-01-test",
            secret=b"wrong-m9-01-external-projection-key",
        )
        with self.assertRaises(ExternalStateProjectionError):
            validate_external_state_projection(projection, signer=wrong_signer)

        boolean_authority = copy.deepcopy(projection)
        boolean_authority["provider_authority"] = False
        body = {
            key: value
            for key, value in boolean_authority.items()
            if key not in {"projection_sha256", "signature"}
        }
        boolean_authority["projection_sha256"] = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        boolean_authority["signature"] = self.signer.sign(
            {**body, "projection_sha256": boolean_authority["projection_sha256"]}
        )
        with self.assertRaises(ExternalStateProjectionError):
            validate_external_state_projection(
                boolean_authority,
                signer=self.signer,
            )

    def test_malformed_source_error_response_is_not_forwarded(self) -> None:
        source = _SourceStub(
            {
                "schema_version": "context.state-mcp-response/v1alpha1",
                "request_id": "ignored-by-stub",
                "tool": "context.state.read",
                "ok": False,
                "result": None,
                "error": {"code": "permission_denied", "message": "denied"},
                "unexpected": True,
            }
        )
        provider = ExternalStateProjectionProvider(
            source,
            provider_id="provider-docmost-reference",
            signer=self.signer,
        )

        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            self.request(request_id="request-malformed-source-error"),
            context=self.context,
        )

        self.assertEqual(response["error"]["code"], "source_integrity_error")

    def test_partial_unknown_or_invalid_typed_snapshot_fails_closed(self) -> None:
        mutations = [
            lambda snapshot: snapshot.pop("works"),
            lambda snapshot: snapshot.__setitem__("unexpected", []),
            lambda snapshot: snapshot["works"][0].__setitem__("status", "unknown"),
        ]
        for mutate in mutations:
            snapshot = copy.deepcopy(self.snapshot)
            mutate(snapshot)
            source = _BoundSource(
                {
                    "schema_version": "context.state-mcp-response/v1alpha1",
                    "request_id": "rebound-by-source",
                    "tool": "context.state.read",
                    "ok": True,
                    "result": {
                        "snapshot": snapshot,
                        "revision": 14,
                        "event_head": None,
                        "registry_digest": "a" * 64,
                        "capabilities": {},
                    },
                    "error": None,
                }
            )
            provider = ExternalStateProjectionProvider(
                source,
                provider_id="provider-docmost-reference",
                signer=self.signer,
            )

            response = provider.call_tool(
                EXTERNAL_READ_TOOL,
                self.request(request_id=f"request-invalid-snapshot-{source.calls}"),
                context=self.context,
            )

            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "source_integrity_error")
            self.assertIsNone(response["result"])


if __name__ == "__main__":
    unittest.main()
