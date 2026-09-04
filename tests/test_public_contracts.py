from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from continuity_plane import cli as cli_module
from continuity_plane.artifact_store import LocalArtifactStore
from continuity_plane.checkpoint import publish_checkpoint, restore_checkpoint
from continuity_plane.cli import main
from continuity_plane.recovery_envelope import RecoveryEnvelopeError, compose_recovery_envelope
from continuity_plane.sqlite_state_store import SQLiteStateStore
from continuity_plane.state_events import build_state_event
from continuity_plane.state_store import (
    StateStoreConflict,
    capability_manifest_to_document,
)


def _initialized_store(root: Path) -> SQLiteStateStore:
    with redirect_stdout(StringIO()):
        main(["init", "--root", str(root), "--project-id", "sample-app"])
    return SQLiteStateStore(root / ".continuity/state.sqlite3")


def _cli_json(arguments: list[str]) -> tuple[int, dict]:
    output = StringIO()
    with redirect_stdout(output):
        result = main(arguments)
    return result, json.loads(output.getvalue())


class PublicContractTests(unittest.TestCase):
    def test_replanned_same_sources_create_current_attach_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "MASTER.md"
            status = root / "STATUS.md"
            master.write_text("# Existing Master\n", encoding="utf-8")
            status.write_text("# Existing Status\n", encoding="utf-8")
            self.assertEqual(
                _cli_json(
                    ["init", "--root", str(root), "--project-id", "sample-app"]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "work-first", "--work-title", "First Work",
                        "--owner-ref", "actor-one", "--scope", "capability:first",
                    ]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "attach", "approve", "--root", str(root),
                        "--actor-ref", "actor-one", "--claim-id", "claim-first",
                    ]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(["checkpoint", "create", "--root", str(root)])[0],
                0,
            )
            evidence = root / "evidence.txt"
            evidence.write_text("verified completion\n", encoding="utf-8")
            self.assertEqual(
                _cli_json(
                    [
                        "work", "complete", "--root", str(root),
                        "--work-id", "work-first", "--claim-id", "claim-first",
                        "--actor-ref", "actor-one", "--evidence-file", str(evidence),
                    ]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "work-next", "--work-title", "Next Work",
                        "--owner-ref", "actor-two", "--scope", "capability:next",
                    ]
                )[0],
                0,
            )
            proposal = json.loads(
                (root / ".continuity/attach-proposal.json").read_text(encoding="utf-8")
            )
            result, response = _cli_json(
                [
                    "work", "activate", "--root", str(root),
                    "--work-id", "work-next", "--work-title", "Next Work",
                    "--owner-ref", "actor-two", "--claim-id", "claim-next",
                    "--scope", "capability:next",
                ]
            )
            state = SQLiteStateStore(root / ".continuity/state.sqlite3").read_project(
                "sample-app"
            )
            work = next(item for item in state["works"] if item["work_id"] == "work-next")

            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "activated")
            self.assertIn(
                f"evidence-attach-{proposal['proposal_sha256'][:16]}",
                work["evidence_ids"],
            )

    def test_idle_stale_sources_rebind_on_successor_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "MASTER.md"
            status = root / "STATUS.md"
            evidence = root / "evidence.txt"
            master.write_text("master v1\n", encoding="utf-8")
            status.write_text("status v1\n", encoding="utf-8")
            evidence.write_text("verified completion\n", encoding="utf-8")
            self.assertEqual(
                _cli_json(
                    ["init", "--root", str(root), "--project-id", "sample-app"]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "attach",
                        "plan",
                        "--root",
                        str(root),
                        "--master",
                        str(master),
                        "--status",
                        str(status),
                        "--work-id",
                        "work-first",
                        "--work-title",
                        "First Work",
                        "--owner-ref",
                        "actor-one",
                        "--scope",
                        "capability:first",
                    ]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "attach",
                        "approve",
                        "--root",
                        str(root),
                        "--actor-ref",
                        "actor-one",
                        "--claim-id",
                        "claim-first",
                    ]
                )[0],
                0,
            )
            self.assertEqual(
                _cli_json(["checkpoint", "create", "--root", str(root)])[0],
                0,
            )
            self.assertEqual(
                _cli_json(
                    [
                        "work",
                        "complete",
                        "--root",
                        str(root),
                        "--work-id",
                        "work-first",
                        "--claim-id",
                        "claim-first",
                        "--actor-ref",
                        "actor-one",
                        "--evidence-file",
                        str(evidence),
                    ]
                )[0],
                0,
            )
            before_code, before = _cli_json(
                ["state", "show", "--root", str(root)]
            )
            self.assertEqual(before_code, 0)
            checkpoint_path = root / ".continuity/checkpoint-ref.json"
            proposal_path = root / ".continuity/attach-proposal.json"
            checkpoint_before = checkpoint_path.read_bytes()
            proposal_before = proposal_path.read_bytes()

            master.write_text("master v2\n", encoding="utf-8")
            status.write_text("status v2\n", encoding="utf-8")

            resume_code, packet = _cli_json(
                ["resume", "--root", str(root)]
            )
            self.assertEqual(resume_code, 0)
            self.assertTrue(packet["read_only"])
            self.assertFalse(packet["source_fresh"])
            self.assertTrue(packet["checkpoint_verified"])
            self.assertEqual(
                packet["next_action"],
                "rebind-source-and-activate-next-work",
            )
            # Older alpha.11 idle packets remain readable but cannot advertise
            # activation while their canonical sources are stale.
            inputs = {
                key: value
                for key, value in packet.items()
                if key not in {
                    "schema_version", "checkpoint_verified", "read_only", "first_permitted_action",
                    "recovery_read_budget_bytes", "state_write_authority",
                    "completion_authority", "packet_sha256",
                }
            }
            inputs["next_action"] = "remain-read-only"
            self.assertTrue(compose_recovery_envelope(**inputs)["read_only"])
            inputs["next_action"] = "activate-next-work"
            with self.assertRaises(RecoveryEnvelopeError):
                compose_recovery_envelope(**inputs)
            verify_code, denied = _cli_json(
                ["checkpoint", "verify", "--root", str(root)]
            )
            self.assertEqual(verify_code, 2)
            self.assertEqual(denied["failed_gate"], "source_rebind_required")
            self.assertFalse(denied["state_changed"])
            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(proposal_path.read_bytes(), proposal_before)
            self.assertEqual(
                _cli_json(["state", "show", "--root", str(root)])[1]["revision"],
                before["revision"],
            )

            denied_activation_code, denied_activation = _cli_json(
                [
                    "work",
                    "activate",
                    "--root",
                    str(root),
                    "--work-id",
                    "work-denied",
                    "--work-title",
                    "Denied Work",
                    "--owner-ref",
                    "actor-two",
                    "--claim-id",
                    "claim-first",
                    "--scope",
                    "capability:denied",
                ]
            )
            self.assertEqual(denied_activation_code, 2)
            self.assertEqual(denied_activation["failed_gate"], "claim_identity")
            self.assertFalse(denied_activation["state_changed"])
            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(proposal_path.read_bytes(), proposal_before)
            self.assertEqual(
                _cli_json(["state", "show", "--root", str(root)])[1]["revision"],
                before["revision"],
            )

            build_evidence = cli_module._build_attach_evidence

            def mutate_source_after_evidence(proposal: dict) -> dict:
                result = build_evidence(proposal)
                master.write_text("master v3\n", encoding="utf-8")
                return result

            with patch.object(
                cli_module,
                "_build_attach_evidence",
                side_effect=mutate_source_after_evidence,
            ):
                race_code, race_denied = _cli_json(
                    [
                        "work",
                        "activate",
                        "--root",
                        str(root),
                        "--work-id",
                        "work-raced",
                        "--work-title",
                        "Raced Work",
                        "--owner-ref",
                        "actor-two",
                        "--claim-id",
                        "claim-raced",
                        "--scope",
                        "capability:raced",
                    ]
                )
            self.assertEqual(race_code, 2)
            self.assertEqual(race_denied["failed_gate"], "source_fresh")
            self.assertFalse(race_denied["state_changed"])
            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(proposal_path.read_bytes(), proposal_before)
            self.assertEqual(
                _cli_json(["state", "show", "--root", str(root)])[1]["revision"],
                before["revision"],
            )
            master.write_text("master v2\n", encoding="utf-8")

            activation_code, activated = _cli_json(
                [
                    "work",
                    "activate",
                    "--root",
                    str(root),
                    "--work-id",
                    "work-next",
                    "--work-title",
                    "Next Work",
                    "--owner-ref",
                    "actor-two",
                    "--claim-id",
                    "claim-next",
                    "--scope",
                    "capability:next",
                ]
            )
            self.assertEqual(activation_code, 0)
            self.assertEqual(activated["changed_sources"], ["master", "status"])
            self.assertTrue(activated["source_evidence_rebound"])
            self.assertNotEqual(proposal_path.read_bytes(), proposal_before)
            self.assertEqual(
                _cli_json(["checkpoint", "verify", "--root", str(root)])[0],
                0,
            )
            final_code, final_packet = _cli_json(
                ["resume", "--root", str(root)]
            )
            self.assertEqual(final_code, 0)
            self.assertFalse(final_packet["read_only"])
            self.assertTrue(final_packet["source_fresh"])
            self.assertEqual(final_packet["active_work"]["work_id"], "work-next")
            self.assertEqual(final_packet["claim"]["claim_id"], "claim-next")
            final_state = _cli_json(
                ["state", "show", "--root", str(root)]
            )[1]
            self.assertEqual(final_state["revision"], before["revision"] + 1)
            work_by_id = {
                item["work_id"]: item for item in final_state["state"]["works"]
            }
            claim_by_id = {
                item["claim_id"]: item for item in final_state["state"]["claims"]
            }
            self.assertEqual(work_by_id["work-first"]["status"], "completed")
            self.assertEqual(claim_by_id["claim-first"]["status"], "released")
            self.assertEqual(work_by_id["work-next"]["status"], "active")
            self.assertEqual(claim_by_id["claim-next"]["status"], "active")

            # Exercise actual hook subprocesses against the isolated project:
            # a successful auto compaction must return its current packet.
            plugin = Path(__file__).parents[1] / "plugins/continuity-plane"
            environment = os.environ.copy()
            environment.update(
                CONTINUITY_EFFECT_POLICY="auto",
                PLUGIN_ROOT=str(plugin),
                PLUGIN_DATA=str(root / "plugin-data"),
            )
            for event in ("PreCompact", "PostCompact", "SessionStart"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "continuity_plane.codex_hook_launcher",
                        str(plugin / "scripts/continuity-hook.py"),
                    ],
                    cwd=Path(__file__).parents[1],
                    input=json.dumps(
                        {
                            "hook_event_name": event,
                            "cwd": str(root),
                            "session_id": "isolated-compaction",
                            "source": "compact",
                            "trigger": "auto",
                        }
                    ),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env=environment,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                if event == "SessionStart":
                    response = json.loads(completed.stdout)
                    context = response["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("work-next", context)
                    self.assertIn("claim-next", context)
                    self.assertLessEqual(len(context.encode("utf-8")), 12 * 1024)
                else:
                    self.assertEqual(completed.stdout, "")
            observations = [
                json.loads(line)
                for path in (root / "plugin-data/live-events").glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [item["event_type"] for item in observations],
                ["precompact", "postcompact", "session-start"],
            )
            self.assertTrue(all(item["success"] for item in observations))
            self.assertTrue(observations[1]["canary_passed"])
            self.assertEqual(
                _cli_json(["state", "show", "--root", str(root)])[1]["revision"],
                final_state["revision"],
            )

    def test_sqlite_event_commit_is_revision_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _initialized_store(Path(directory))
            initial = store.read_project("sample-app")
            initial_events = store.read_events("sample-app")
            expected = copy.deepcopy(initial)
            expected["project"]["revision"] = initial["project"]["revision"] + 1
            expected["project"]["updated_at"] = (
                (
                    datetime.fromisoformat(initial["project"]["updated_at"])
                    + timedelta(seconds=1)
                )
                .astimezone(UTC)
                .isoformat()
            )
            expected["works"][0]["status"] = "ready"
            expected["works"][0]["revision"] += 1
            event = build_state_event(
                event_id="event-ready-initial",
                event_type="state-transition",
                project_id="sample-app",
                sequence_no=initial_events[-1]["sequence_no"] + 1,
                revision_before=initial["project"]["revision"],
                occurred_at=expected["project"]["updated_at"],
                actor_ref="local-user",
                causation_ref="work:work-initial",
                correlation_ref="project:sample-app",
                previous_event_sha256=initial_events[-1]["event_sha256"],
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "works",
                        "object_id": "work-initial",
                        "value": expected["works"][0],
                    }
                ],
                project_after=expected["project"],
            )

            store.commit_event(
                project_id="sample-app",
                expected_revision=initial["project"]["revision"],
                event=event,
                expected_snapshot=expected,
            )

            self.assertEqual(
                store.read_project("sample-app")["project"]["revision"],
                initial["project"]["revision"] + 1,
            )
            self.assertEqual(len(store.read_events("sample-app")), len(initial_events) + 1)
            with self.assertRaises(StateStoreConflict):
                store.commit_event(
                    project_id="sample-app",
                    expected_revision=initial["project"]["revision"],
                    event=event,
                    expected_snapshot=expected,
                )

    def test_checkpoint_round_trip_binds_state_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_store = _initialized_store(root)
            artifact_store = LocalArtifactStore(root / "artifacts")
            artifact_store.initialize()
            snapshot = state_store.read_project("sample-app")
            events = state_store.read_events("sample-app")
            read_result = {
                "snapshot": snapshot,
                "revision": snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": events[-1]["sequence_no"],
                    "event_sha256": events[-1]["event_sha256"],
                },
                "registry_digest": "b" * 64,
                "capabilities": capability_manifest_to_document(
                    state_store.capability_manifest
                ),
            }

            checkpoint_ref = publish_checkpoint(
                read_result,
                artifact_store,
                canonical_plan_sha256="a" * 64,
            )
            restored = restore_checkpoint(
                checkpoint_ref,
                artifact_store,
                expected_project_id="sample-app",
                expected_revision=snapshot["project"]["revision"],
                expected_event_head=read_result["event_head"],
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256="a" * 64,
                expected_registry_digest="b" * 64,
            )

            self.assertEqual(restored.snapshot, snapshot)
            self.assertRegex(
                restored.manifest["critical_projection_sha256"], r"^[0-9a-f]{64}$"
            )
