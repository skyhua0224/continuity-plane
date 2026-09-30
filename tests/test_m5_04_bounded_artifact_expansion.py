from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.artifact_store import (
    ArtifactIntegrityError,
    ArtifactRangeError,
    LocalArtifactStore,
)
from context_control_plane.bounded_artifact_expansion import (
    BoundedArtifactBudgetError,
    BoundedArtifactInputError,
    expand_artifact,
    validate_bounded_artifact_expansion_receipt,
)


class M504BoundedArtifactExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = LocalArtifactStore(
            Path(self.temporary_directory.name) / "artifacts",
            max_range_bytes=64,
        )
        self.store.initialize()
        self.payload = b"header\nalpha evidence\nmiddle\nomega proof\nfooter\n"
        self.ref = self.store.put_bytes(self.payload)

    def test_prompt_contains_only_summary_ref_and_necessary_excerpts(self):
        alpha_offset = self.payload.index(b"alpha")
        omega_offset = self.payload.index(b"omega")

        result = expand_artifact(
            self.store,
            artifact_ref=self.ref,
            summary="Two current evidence lines",
            necessary_ranges=[
                {
                    "purpose": "terminal result",
                    "offset_bytes": omega_offset,
                    "length_bytes": len(b"omega proof"),
                },
                {
                    "purpose": "initial result",
                    "offset_bytes": alpha_offset,
                    "length_bytes": len(b"alpha evidence"),
                },
            ],
            returned_byte_budget=64,
            scanned_byte_budget=2 * len(self.payload),
        )

        self.assertEqual(
            set(result.prompt),
            {"summary", "artifact_ref", "necessary_excerpts"},
        )
        self.assertEqual(result.prompt["artifact_ref"], self.ref.to_document())
        self.assertEqual(
            [item["text"] for item in result.prompt["necessary_excerpts"]],
            ["alpha evidence", "omega proof"],
        )
        self.assertNotIn("middle", result.prompt_bytes.decode("utf-8"))
        self.assertEqual(result.receipt["requested_bytes"], 25)
        self.assertEqual(result.receipt["returned_bytes"], 25)
        self.assertEqual(result.receipt["scanned_bytes"], 2 * len(self.payload))
        self.assertEqual(len(result.receipt["ranges"]), 2)
        self.assertFalse(result.receipt["state_write_authority"])

    def test_total_returned_budget_is_rejected_before_artifact_access(self):
        self.store.object_path(self.ref).write_bytes(b"x" * len(self.payload))

        with self.assertRaisesRegex(
            BoundedArtifactBudgetError,
            "returned byte budget",
        ):
            expand_artifact(
                self.store,
                artifact_ref=self.ref,
                summary="Budget preflight",
                necessary_ranges=[
                    {
                        "purpose": "too large",
                        "offset_bytes": 0,
                        "length_bytes": 17,
                    }
                ],
                returned_byte_budget=16,
                scanned_byte_budget=len(self.payload),
            )

    def test_total_scan_budget_accounts_for_full_integrity_scan_per_range(self):
        self.store.object_path(self.ref).write_bytes(b"x" * len(self.payload))

        with self.assertRaisesRegex(
            BoundedArtifactBudgetError,
            "scanned byte budget",
        ):
            expand_artifact(
                self.store,
                artifact_ref=self.ref,
                summary="Scan budget preflight",
                necessary_ranges=[
                    {"purpose": "first", "offset_bytes": 0, "length_bytes": 1},
                    {"purpose": "second", "offset_bytes": 2, "length_bytes": 1},
                ],
                returned_byte_budget=2,
                scanned_byte_budget=(2 * len(self.payload)) - 1,
            )

    def test_all_ranges_are_validated_before_the_first_artifact_read(self):
        self.store.object_path(self.ref).write_bytes(b"x" * len(self.payload))

        with self.assertRaisesRegex(ArtifactRangeError, "bounds"):
            expand_artifact(
                self.store,
                artifact_ref=self.ref,
                summary="Range preflight",
                necessary_ranges=[
                    {"purpose": "valid", "offset_bytes": 0, "length_bytes": 1},
                    {
                        "purpose": "outside object",
                        "offset_bytes": len(self.payload),
                        "length_bytes": 1,
                    },
                ],
                returned_byte_budget=2,
                scanned_byte_budget=2 * len(self.payload),
            )

    def test_digest_mismatch_fails_closed_without_returning_a_prompt(self):
        self.store.object_path(self.ref).write_bytes(b"x" * len(self.payload))

        with self.assertRaises(ArtifactIntegrityError):
            expand_artifact(
                self.store,
                artifact_ref=self.ref,
                summary="Corrupted object",
                necessary_ranges=[
                    {
                        "purpose": "required",
                        "offset_bytes": 0,
                        "length_bytes": 1,
                    }
                ],
                returned_byte_budget=1,
                scanned_byte_budget=len(self.payload),
            )

    def test_unbounded_or_malformed_prompt_inputs_are_rejected_before_read(self):
        self.store.object_path(self.ref).write_bytes(b"x" * len(self.payload))

        with self.assertRaises(BoundedArtifactInputError):
            expand_artifact(
                self.store,
                artifact_ref=self.ref,
                summary="",
                necessary_ranges=[
                    {"purpose": "required", "offset_bytes": 0, "length_bytes": 1}
                ],
                returned_byte_budget=1,
                scanned_byte_budget=len(self.payload),
            )

    def test_invalid_utf8_excerpt_is_rejected_without_a_prompt(self):
        binary_ref = self.store.put_bytes(b"\xff")

        with self.assertRaises(BoundedArtifactInputError):
            expand_artifact(
                self.store,
                artifact_ref=binary_ref,
                summary="Binary object",
                necessary_ranges=[
                    {"purpose": "required", "offset_bytes": 0, "length_bytes": 1}
                ],
                returned_byte_budget=1,
                scanned_byte_budget=1,
            )

    def test_receipt_is_strict_and_digest_bound(self):
        result = expand_artifact(
            self.store,
            artifact_ref=self.ref,
            summary="One line",
            necessary_ranges=[
                {"purpose": "line", "offset_bytes": 0, "length_bytes": 6}
            ],
            returned_byte_budget=6,
            scanned_byte_budget=len(self.payload),
        )

        validate_bounded_artifact_expansion_receipt(
            result.receipt,
            prompt_bytes=result.prompt_bytes,
        )
        forged = copy.deepcopy(result.receipt)
        forged["returned_bytes"] += 1
        with self.assertRaises(ValueError):
            validate_bounded_artifact_expansion_receipt(
                forged,
                prompt_bytes=result.prompt_bytes,
            )

        forged_prompt = result.prompt
        forged_prompt["necessary_excerpts"][0]["length_bytes"] += 1
        forged_prompt_bytes = json.dumps(
            forged_prompt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        forged_prompt_receipt = copy.deepcopy(result.receipt)
        forged_prompt_receipt["prompt_sha256"] = hashlib.sha256(
            forged_prompt_bytes
        ).hexdigest()
        with self.assertRaises(ValueError):
            validate_bounded_artifact_expansion_receipt(
                forged_prompt_receipt,
                prompt_bytes=forged_prompt_bytes,
            )

    def test_summary_and_ref_can_be_composed_without_expanding_content(self):
        result = expand_artifact(
            self.store,
            artifact_ref=self.ref,
            summary="No excerpt is necessary",
            necessary_ranges=[],
            returned_byte_budget=1,
            scanned_byte_budget=0,
        )

        self.assertEqual(result.prompt["necessary_excerpts"], [])
        self.assertEqual(result.receipt["returned_bytes"], 0)
        self.assertEqual(result.receipt["scanned_bytes"], 0)
        validate_bounded_artifact_expansion_receipt(
            result.receipt,
            prompt_bytes=result.prompt_bytes,
        )


if __name__ == "__main__":
    unittest.main()
