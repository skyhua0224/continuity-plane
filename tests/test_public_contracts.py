from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from continuity_plane.artifact_store import LocalArtifactStore
from continuity_plane.checkpoint import publish_checkpoint, restore_checkpoint
from continuity_plane.cli import main
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


class PublicContractTests(unittest.TestCase):
    def test_replanned_same_sources_create_current_attach_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "MASTER.md"
            status = root / "STATUS.md"
            master.write_text("# Existing Master\n", encoding="utf-8")
            status.write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "work-first", "--work-title", "First Work",
                        "--owner-ref", "actor-one", "--scope", "capability:first",
                    ]
                )
                main(
                    [
                        "attach", "approve", "--root", str(root),
                        "--actor-ref", "actor-one", "--claim-id", "claim-first",
                    ]
                )
                main(["checkpoint", "create", "--root", str(root)])
            evidence = root / "evidence.txt"
            evidence.write_text("verified completion\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(
                    [
                        "work", "complete", "--root", str(root),
                        "--work-id", "work-first", "--claim-id", "claim-first",
                        "--actor-ref", "actor-one", "--evidence-file", str(evidence),
                    ]
                )
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "work-next", "--work-title", "Next Work",
                        "--owner-ref", "actor-two", "--scope", "capability:next",
                    ]
                )
            proposal = json.loads(
                (root / ".continuity/attach-proposal.json").read_text(encoding="utf-8")
            )
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "work", "activate", "--root", str(root),
                        "--work-id", "work-next", "--work-title", "Next Work",
                        "--owner-ref", "actor-two", "--claim-id", "claim-next",
                        "--scope", "capability:next",
                    ]
                )
            response = json.loads(output.getvalue())
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
