"""M10-11 checkpoint-bound recovery envelope and CLI integration."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane.cli import main
from context_control_plane.recovery_envelope import (
    RecoveryEnvelopeError,
    compose_recovery_envelope,
    validate_interaction_cursor,
    validate_recovery_envelope,
)


class M1011RecoveryEnvelopeTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "MASTER.md").write_text("# Portable project\n", encoding="utf-8")
        (root / "STATUS.md").write_text("# Current work\n", encoding="utf-8")
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", "portable-project"])
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
                    "work-active",
                    "--work-title",
                    "Continue portable work",
                    "--owner-ref",
                    "agent-main",
                    "--scope",
                    "capability:portable-work",
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
                    "claim-active",
                ]
            )
            main(["checkpoint", "create", "--root", str(root)])

    def _cursor(self) -> dict:
        cursor = {
            "schema_version": "context.interaction-cursor/v1alpha1",
            "current_input_ref": "input://sha256/" + "1" * 64,
            "current_input_sha256": "1" * 64,
            "current_turn_sha256": "2" * 64,
            "confirmed_input_refs": ["input://sha256/" + "1" * 64],
            "visible_output_high_watermark_sha256": "3" * 64,
            "visible_output_phase": "final_answer",
            "response_mode": "continue-silently",
            "no_restate": True,
            "raw_transcript_admission": False,
            "state_write_authority": False,
            "completion_authority": False,
            "cursor_sha256": "",
        }
        cursor["cursor_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in cursor.items() if key != "cursor_sha256"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return cursor

    def test_resume_emits_checkpoint_bound_bounded_recovery_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            cursor_path = root / ".continuity/interaction-cursor.json"
            cursor_path.write_text(
                json.dumps(self._cursor(), sort_keys=True) + "\n", encoding="utf-8"
            )
            skill_lock_path = root / ".continuity/skill-lock.json"
            skill_lock_path.write_text(
                json.dumps(
                    {
                        "status": "measured",
                        "selected_rule_ids": [
                            "continuity.answer.direct",
                            "continuity.resume.current-state",
                        ],
                        "compiled_packet_sha256": "4" * 64,
                        "unavailable_reason": None,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "resume",
                        "--root",
                        str(root),
                        "--interaction-cursor",
                        str(cursor_path),
                        "--skill-lock",
                        str(skill_lock_path),
                    ]
                )
            envelope = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        validate_recovery_envelope(envelope)
        self.assertEqual(envelope["schema_version"], "context.recovery-envelope/v1alpha1")
        self.assertEqual(envelope["active_work"]["work_id"], "work-active")
        self.assertEqual(envelope["claim"]["claim_id"], "claim-active")
        self.assertEqual(envelope["interaction_cursor"]["response_mode"], "continue-silently")
        self.assertEqual(envelope["interaction_cursor"]["confirmed_input_refs"], ["input://sha256/" + "1" * 64])
        self.assertTrue(envelope["checkpoint_verified"])
        self.assertEqual(envelope["effect_high_watermark"], 0)
        self.assertEqual(envelope["return_point_work_id"], None)
        self.assertEqual(envelope["first_permitted_action"]["target"], "continue-active-work")
        self.assertEqual(envelope["skill_lock"]["status"], "measured")
        self.assertEqual(
            envelope["skill_lock"]["selected_rule_ids"],
            ["continuity.answer.direct", "continuity.resume.current-state"],
        )
        self.assertLessEqual(len(json.dumps(envelope, separators=(",", ":")).encode()), 12 * 1024)
        self.assertFalse(envelope["state_write_authority"])
        self.assertFalse(envelope["completion_authority"])

        schema = json.loads(
            (
                Path(__file__).parents[1]
                / "schemas/m10-11/recovery-envelope.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(envelope)

    def test_cursor_tamper_and_missing_checkpoint_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            cursor = self._cursor()
            validate_interaction_cursor(cursor)
            cursor["no_restate"] = False
            cursor_path = root / "cursor.json"
            cursor_path.write_text(json.dumps(cursor), encoding="utf-8")
            with self.assertRaises((RecoveryEnvelopeError, ValueError)):
                with redirect_stdout(StringIO()):
                    main(
                        [
                            "resume",
                            "--root",
                            str(root),
                            "--interaction-cursor",
                            str(cursor_path),
                        ]
                    )

            (root / ".continuity/checkpoint-ref.json").unlink()
            with self.assertRaises(ValueError):
                with redirect_stdout(StringIO()):
                    main(["resume", "--root", str(root)])

    def test_envelope_digest_and_exact_action_are_not_self_mutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project(root)
            output = StringIO()
            with redirect_stdout(output):
                main(["resume", "--root", str(root)])
            envelope = json.loads(output.getvalue())

        mutations = []
        action = copy.deepcopy(envelope)
        action["first_permitted_action"]["target"] = "restart-from-plan"
        mutations.append(action)
        authority = copy.deepcopy(envelope)
        authority["state_write_authority"] = True
        mutations.append(authority)
        oversized = copy.deepcopy(envelope)
        oversized["current_decisions"] = [
            {
                "decision_id": f"decision-{index}",
                "statement": "x" * 1024,
                "evidence_ids": [],
            }
            for index in range(32)
        ]
        mutations.append(oversized)
        for mutation in mutations:
            with self.subTest(), self.assertRaises(RecoveryEnvelopeError):
                validate_recovery_envelope(mutation)

    def test_idle_envelope_is_verified_writable_and_requests_activation(self) -> None:
        envelope = compose_recovery_envelope(
            project_id="portable-project",
            revision=3,
            event_head={"sequence_no": 3, "event_sha256": "1" * 64},
            checkpoint_ref={
                "schema_version": "context.artifact-ref/v1alpha1",
                "artifact_uri": "artifact://sha256/" + "2" * 64,
                "digest_algorithm": "sha-256",
                "digest": "2" * 64,
                "size_bytes": 100,
            },
            active_work=None,
            claim=None,
            current_decisions=[],
            current_constraints=[],
            open_blockers=[],
            return_point_work_id=None,
            effect_high_watermark=0,
            proposal_sha256="3" * 64,
            source_fresh=True,
            lease_valid=True,
            next_action="activate-next-work",
            interaction_cursor=None,
        )

        validate_recovery_envelope(envelope)
        self.assertIsNone(envelope["active_work"])
        self.assertIsNone(envelope["claim"])
        self.assertTrue(envelope["checkpoint_verified"])
        self.assertFalse(envelope["read_only"])
        self.assertEqual(envelope["next_action"], "activate-next-work")

    def test_idle_stale_source_envelope_is_read_only_without_a_fake_activation(self) -> None:
        envelope = compose_recovery_envelope(
            project_id="portable-project",
            revision=3,
            event_head={"sequence_no": 3, "event_sha256": "1" * 64},
            checkpoint_ref={
                "schema_version": "context.artifact-ref/v1alpha1",
                "artifact_uri": "artifact://sha256/" + "2" * 64,
                "digest_algorithm": "sha-256",
                "digest": "2" * 64,
                "size_bytes": 100,
            },
            active_work=None,
            claim=None,
            current_decisions=[],
            current_constraints=[],
            open_blockers=[],
            return_point_work_id=None,
            effect_high_watermark=0,
            proposal_sha256="3" * 64,
            source_fresh=False,
            lease_valid=True,
            next_action="remain-read-only",
            interaction_cursor=None,
        )

        validate_recovery_envelope(envelope)
        self.assertTrue(envelope["read_only"])
        self.assertEqual(envelope["next_action"], "remain-read-only")
        self.assertEqual(
            envelope["first_permitted_action"]["target"], "remain-read-only"
        )


if __name__ == "__main__":
    unittest.main()
