"""Legacy local-embedded claim recovery before v6 migration."""

from __future__ import annotations

import tempfile
import unittest
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.cli import main
from context_control_plane.checkpoint import CheckpointError
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
    LOCAL_CLAIM_RECOVERY_TOOL,
    RequestContext,
    StateMCPService,
)


class _Authorizer:
    def authorize(self, context, action, project_id):
        return context.authorization_ref == "local-recovery-approved"


class LocalClaimRecoveryTests(unittest.TestCase):
    def test_recovery_request_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-09/state-claim-recovery-request.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.state-claim-recovery-request"
        )
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            entry["current_wire_version"],
            LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
        )
    def _attached(self, root: Path) -> SQLiteStateStore:
        (root / "MASTER.md").write_text("# Master\n", encoding="utf-8")
        (root / "STATUS.md").write_text("# Status\n", encoding="utf-8")
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", "sample-app"])
            main(
                [
                    "attach",
                    "plan",
                    "--root",
                    str(root),
                    "--master",
                    "MASTER.md",
                    "--status",
                    "STATUS.md",
                    "--work-id",
                    "work-current",
                    "--work-title",
                    "Current Work",
                    "--owner-ref",
                    "agent-main",
                    "--scope",
                    "capability:main",
                ]
            )
            main(
                [
                    "attach",
                    "approve",
                    "--root",
                    str(root),
                    "--actor-ref",
                    "agent-main",
                    "--claim-id",
                    "claim-current",
                ]
            )
        store = SQLiteStateStore(root / ".continuity/state.sqlite3")
        connection = __import__("sqlite3").connect(root / ".continuity/state.sqlite3")
        try:
            snapshot = store.read_project("sample-app")
            snapshot["project"]["updated_at"] = "2026-08-19T00:00:00+00:00"
            for claim in snapshot["claims"]:
                if claim["claim_id"] == "claim-current":
                    claim["claimed_at"] = "2026-08-19T00:00:00+00:00"
                    claim["lease_expires_at"] = "2026-08-19T21:00:00+00:00"
                    claim["expected_project_revision"] = snapshot["project"]["revision"]
            from context_control_plane.sqlite_state_store import _json_text, _snapshot_sha256
            connection.execute(
                "UPDATE projects SET snapshot=?, snapshot_sha256=? WHERE project_id=?",
                (_json_text(snapshot), _snapshot_sha256(snapshot), "sample-app"),
            )
            connection.commit()
        finally:
            connection.close()
        store.initialize()
        return store

    def _service(self, store: SQLiteStateStore) -> StateMCPService:
        return StateMCPService(
            store,
            authorizer=_Authorizer(),
            registry_digest="a" * 64,
            clock=lambda: "2026-08-19T22:00:00+00:00",
            event_id_factory=lambda request_id: f"event-{request_id}",
        )

    def _reclaim(self, revision: int) -> dict:
        return {
            "schema_version": LOCAL_CLAIM_RECOVERY_REQUEST_SCHEMA_VERSION,
            "request_id": "reclaim-current",
            "project_id": "sample-app",
            "action": "reclaim",
            "expected_revision": revision,
            "claim_id": "claim-current",
            "new_claim_id": "claim-current-reclaimed-1",
            "actor_ref": "agent-main",
            "scope_owners": [{"scope_kind": "capability", "scope_ref": "main"}],
            "lease_ttl_ms": 28_800_000,
            "causation_ref": "recovery:test",
            "correlation_ref": "project:sample-app",
        }

    def test_expired_legacy_claim_reclaims_with_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached(root)
            before = store.read_project("sample-app")
            response = self._service(store).call_tool(
                LOCAL_CLAIM_RECOVERY_TOOL,
                self._reclaim(before["project"]["revision"]),
                context=RequestContext("agent-main", "local-recovery-approved"),
            )

            self.assertTrue(response["ok"], response)
            state = store.read_project("sample-app")
            old = next(item for item in state["claims"] if item["claim_id"] == "claim-current")
            new = next(
                item
                for item in state["claims"]
                if item["claim_id"] == "claim-current-reclaimed-1"
            )
            self.assertEqual(old["status"], "expired")
            self.assertEqual(new["status"], "active")
            self.assertEqual(new["actor_ref"], "agent-main")
            self.assertEqual(state["project"]["revision"], before["project"]["revision"] + 1)
            self.assertEqual(len(store.read_events("sample-app")), 3)

    def test_expired_claim_cannot_be_heartbeat_extended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached(root)
            before = store.read_project("sample-app")
            request = self._reclaim(before["project"]["revision"])
            request.update({"action": "heartbeat", "new_claim_id": None})
            response = self._service(store).call_tool(
                LOCAL_CLAIM_RECOVERY_TOOL,
                request,
                context=RequestContext("agent-main", "local-recovery-approved"),
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "conflict")
            self.assertEqual(store.read_project("sample-app"), before)

    def test_cli_reclaim_uses_the_same_authority_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached(Path(directory))
            # The CLI uses the real trusted clock; make the lease old while
            # preserving the typed-state invariant that it follows claimed_at.
            connection = __import__("sqlite3").connect(root / ".continuity/state.sqlite3")
            try:
                snapshot = store.read_project("sample-app")
                snapshot["project"]["updated_at"] = "2020-01-01T00:00:00+00:00"
                for claim in snapshot["claims"]:
                    if claim["claim_id"] == "claim-current":
                        claim["claimed_at"] = "2020-01-01T00:00:00+00:00"
                        claim["lease_expires_at"] = "2020-01-02T00:00:00+00:00"
                from context_control_plane.sqlite_state_store import _json_text, _snapshot_sha256
                connection.execute(
                    "UPDATE projects SET snapshot=?, snapshot_sha256=? WHERE project_id=?",
                    (_json_text(snapshot), _snapshot_sha256(snapshot), "sample-app"),
                )
                connection.commit()
            finally:
                connection.close()
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "work",
                        "recover",
                        "reclaim",
                        "--root",
                        str(root),
                        "--claim-id",
                        "claim-current",
                        "--new-claim-id",
                        "claim-current-reclaimed-cli",
                        "--actor-ref",
                        "agent-main",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn("claim-current-reclaimed-cli", output.getvalue())

    def test_cli_reclaim_checkpoint_failure_keeps_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached(root)
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")

            with patch(
                "context_control_plane.cli.publish_checkpoint",
                side_effect=CheckpointError("injected checkpoint failure"),
            ):
                with self.assertRaises((CheckpointError, ValueError)):
                    main(
                        [
                            "work",
                            "recover",
                            "reclaim",
                            "--root",
                            str(root),
                            "--claim-id",
                            "claim-current",
                            "--new-claim-id",
                            "claim-current-reclaimed-fault",
                            "--actor-ref",
                            "agent-main",
                        ]
                    )

            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)

    def test_cli_heartbeat_uses_a_new_id_for_each_expected_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MASTER.md").write_text("# Master\n", encoding="utf-8")
            (root / "STATUS.md").write_text("# Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach",
                        "plan",
                        "--root",
                        str(root),
                        "--master",
                        "MASTER.md",
                        "--status",
                        "STATUS.md",
                        "--work-id",
                        "work-current",
                        "--work-title",
                        "Current Work",
                        "--owner-ref",
                        "agent-main",
                        "--scope",
                        "capability:main",
                    ]
                )
                main(
                    [
                        "attach",
                        "approve",
                        "--root",
                        str(root),
                        "--actor-ref",
                        "agent-main",
                        "--claim-id",
                        "claim-current",
                    ]
                )
            store = SQLiteStateStore(root / ".continuity/state.sqlite3")
            connection = __import__("sqlite3").connect(root / ".continuity/state.sqlite3")
            try:
                snapshot = store.read_project("sample-app")
                snapshot["project"]["updated_at"] = "2026-08-19T00:00:00+00:00"
                for claim in snapshot["claims"]:
                    if claim["claim_id"] == "claim-current":
                        claim["claimed_at"] = "2026-08-19T00:00:00+00:00"
                        claim["lease_expires_at"] = "2099-01-01T00:00:00+00:00"
                        claim["expected_project_revision"] = snapshot["project"]["revision"]
                from context_control_plane.sqlite_state_store import _json_text, _snapshot_sha256

                connection.execute(
                    "UPDATE projects SET snapshot=?, snapshot_sha256=? WHERE project_id=?",
                    (_json_text(snapshot), _snapshot_sha256(snapshot), "sample-app"),
                )
                connection.commit()
            finally:
                connection.close()

            first = StringIO()
            with redirect_stdout(first):
                first_result = main(
                    [
                        "work",
                        "recover",
                        "heartbeat",
                        "--root",
                        str(root),
                        "--claim-id",
                        "claim-current",
                        "--actor-ref",
                        "agent-main",
                    ]
                )
            second = StringIO()
            with redirect_stdout(second):
                second_result = main(
                    [
                        "work",
                        "recover",
                        "heartbeat",
                        "--root",
                        str(root),
                        "--claim-id",
                        "claim-current",
                        "--actor-ref",
                        "agent-main",
                    ]
                )

            self.assertEqual(first_result, 0)
            self.assertEqual(second_result, 0)
            self.assertEqual(json.loads(first.getvalue())["revision"], 3)
            second_response = json.loads(second.getvalue())
            self.assertEqual(second_response["revision"], 4)
            self.assertTrue(second_response["checkpoint_verified"])
            self.assertEqual(
                second_response["checkpoint_ref"]["artifact_uri"],
                f"artifact://sha256/{second_response['checkpoint_ref']['digest']}",
            )
            self.assertEqual(len(store.read_events("sample-app")), 4)
            self.assertNotEqual(
                store.read_events("sample-app")[-1]["event_id"],
                store.read_events("sample-app")[-2]["event_id"],
            )
            resumed = StringIO()
            with redirect_stdout(resumed):
                self.assertEqual(main(["resume", "--root", str(root)]), 0)
            packet = json.loads(resumed.getvalue())
            self.assertEqual(packet["revision"], 4)
            self.assertTrue(packet["checkpoint_verified"])
            self.assertFalse(packet["read_only"])


if __name__ == "__main__":
    unittest.main()
