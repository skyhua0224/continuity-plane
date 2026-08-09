import hashlib
import unittest

from context_control_plane.archive_extraction import CandidateEvent
from context_control_plane.fixture_extraction import (
    FragmentExtractionError,
    extract_sanitized_fragment,
)
from context_control_plane.sanitizer import sanitize_text


class FragmentExtractionTests(unittest.TestCase):
    def test_anchor_fragment_is_bounded_redacted_and_reconstructable(self):
        raw_uuid = "019fe216-111c-71e3-a1af-497306ba2391"
        text = (
            "Unrelated historical paragraph.\n\n"
            "N-68 was executed and reverted after the PTO veto fired. "
            f"source={raw_uuid} token=private-value\n\n"
            "Another unrelated paragraph."
        )
        event = CandidateEvent(
            event_kind="compaction_checkpoint",
            text=text,
            range_sha256="a" * 64,
            metadata={"line_number": 10},
        )

        fragment = extract_sanitized_fragment(
            event,
            anchors=("N-68", "PTO"),
            max_bytes=512,
        )

        self.assertIn("N-68", fragment.content)
        self.assertIn("PTO", fragment.content)
        self.assertNotIn(raw_uuid, fragment.content)
        self.assertNotIn("private-value", fragment.content)
        self.assertLessEqual(len(fragment.content.encode()), 512)
        self.assertEqual(sanitize_text(fragment.content).findings, ())
        self.assertEqual(
            fragment.receipt.normalized_event_sha256,
            hashlib.sha256(text.encode()).hexdigest(),
        )
        source_fragment = text[
            fragment.receipt.content_start : fragment.receipt.content_end
        ]
        self.assertEqual(
            fragment.receipt.source_fragment_sha256,
            hashlib.sha256(source_fragment.encode()).hexdigest(),
        )
        self.assertEqual(
            fragment.receipt.findings_by_category,
            {"provider-id": 1, "secret": 1},
        )

    def test_fragment_requires_every_anchor(self):
        event = CandidateEvent("user_message", "only ALTP", "b" * 64, {})

        with self.assertRaises(FragmentExtractionError):
            extract_sanitized_fragment(
                event,
                anchors=("ALTP", "missing"),
                max_bytes=128,
            )

    def test_receipt_range_reconstructs_fragment_when_paragraph_has_padding(self):
        text = "prefix\n\n  N-68 reverted after the PTO veto.  \n\nsuffix"
        event = CandidateEvent("compaction_checkpoint", text, "c" * 64, {})

        fragment = extract_sanitized_fragment(
            event,
            anchors=("N-68", "PTO"),
            max_bytes=128,
        )

        source_fragment = text[
            fragment.receipt.content_start : fragment.receipt.content_end
        ]
        self.assertEqual(source_fragment, "N-68 reverted after the PTO veto.")
        self.assertEqual(
            fragment.receipt.source_fragment_sha256,
            hashlib.sha256(source_fragment.encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
