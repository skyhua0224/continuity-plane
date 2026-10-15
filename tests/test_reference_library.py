import json
import tempfile
import unittest
from pathlib import Path

from continuity_plane import reference_library as refs
from continuity_plane.collaboration_registry import register_project
from continuity_plane.planning_api import execute, handle_tool


class ReferenceLibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.root.mkdir()
        self.data = Path(tmp.name) / "external"
        register_project(self.data, project_id="sample", control_root=self.root)

    def add(self, **extra):
        args = dict(
            reference_id="udp-doc",
            title="UDP reference",
            source="https://example.org/spec#section-3",
            kind="standard",
            topics=["udp"],
            platforms=["Linux"],
        )
        args.update(extra)
        return refs.add(self.root, data_root=self.data, **args)

    def test_external_candidate_registration_never_claims_read_or_current(self):
        self.add()
        item = refs.find(self.root, data_root=self.data)["items"][0]
        self.assertEqual(item["review_status"], "candidate")
        self.assertEqual(item["authority"], "declared-not-verified")
        self.assertTrue(item["review_due"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(refs.location(self.root, self.data).is_relative_to(self.data))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.add()

    def test_review_and_use_keep_evidence_separate_from_freshness(self):
        self.add()
        common = dict(
            reference_id="udp-doc",
            actor="tester",
            summary="Applies to supported target",
            data_root=self.data,
        )
        with self.assertRaisesRegex(ValueError, "requires"):
            refs.record(self.root, **common, status="read")
        refs.record(
            self.root, **common, status="read", version="2026-01", evidence="section 3"
        )
        refs.record(self.root, **common, code_ref="src/udp.c:9")
        item = refs.find(self.root, data_root=self.data)["items"][0]
        self.assertEqual(len(item["reviews"]), 1)
        self.assertEqual(len(item["uses"]), 1)
        self.assertEqual(item["freshness"], "not-automatically-verified")
        self.assertFalse(item["review_due"])
        refs.record(self.root, **common, status="unavailable")
        self.assertEqual(
            refs.find(self.root, data_root=self.data)["items"][0]["review_status"],
            "unavailable",
        )

    def test_global_sources_filters_and_project_preference_override(self):
        self.add(scope="global")
        self.add(reference_id="other", platforms=["Windows"], kind="implementation")
        result = refs.find(
            self.root,
            data_root=self.data,
            platform="linux",
            query="UDP",
            kind="standard",
        )
        self.assertEqual([r["reference_id"] for r in result["items"]], ["udp-doc"])
        self.assertEqual(result["items"][0]["scope"], "global")
        self.assertIsNone(refs.hint(self.root, data_root=self.data))
        refs.preference(
            self.root, mode="primary-first", scope="global", data_root=self.data
        )
        self.assertIsNotNone(refs.hint(self.root, data_root=self.data))
        refs.preference(self.root, mode="off", data_root=self.data)
        self.assertIsNone(refs.hint(self.root, data_root=self.data))

    def test_private_urls_and_invalid_ids_rejected_without_writes(self):
        for extra in (
            {"reference_id": "../../secret"},
            {"source": "https://user:secret@example.org/x"},
            {"source": "https://example.org/x?token=secret"},
            {"source": "javascript:alert(1)"},
        ):
            with self.assertRaises(ValueError):
                self.add(**extra)
        self.assertFalse(refs.location(self.root, self.data).exists())
        self.add(source="https://learn.microsoft.com/example?view=windows-11")
        self.add(reference_id="checkout", source=r"C:\references\library")

    def test_cli_mcp_parity_and_strict_output_budget(self):
        value = execute(
            "reference.add",
            dict(
                reference_id="os",
                title="OS docs",
                source="https://example.org/os",
                kind="official-os",
                platform=["Linux"],
            ),
            project_id="sample",
            data_root=str(self.data),
        )
        self.assertEqual(value["review_status"], "candidate")
        for index in range(12):
            self.add(reference_id=f"doc-{index}", title="资料" * 35)
        reply = handle_tool(
            "continuity_plan",
            dict(
                action="reference.list",
                root=str(self.root),
                data_root=str(self.data),
                max_output_bytes=1024,
                format="json",
                parameters={"max_results": 100},
            ),
        )
        text = reply["content"][0]["text"]
        self.assertLessEqual(len(text.encode()), 1024)
        self.assertTrue(json.loads(text)["truncated"])
        self.assertEqual(json.loads(text)["total_items"], 13)
        with self.assertRaisesRegex(ValueError, "different"):
            execute(
                "reference.add",
                {},
                root=str(self.root),
                project_id="other",
                data_root=str(self.data),
            )

    def test_bounded_history_and_unregistered_project_use(self):
        self.add()
        for _ in range(35):
            refs.record(
                self.root,
                reference_id="udp-doc",
                actor="tester",
                summary="comparison",
                data_root=self.data,
            )
        self.assertEqual(
            len(refs.find(self.root, data_root=self.data)["items"][0]["uses"]), 32
        )
        other = self.root.parent / "unregistered"
        other.mkdir()
        result = execute("reference.list", root=str(other), data_root=str(self.data))
        self.assertEqual(result["total_items"], 0)


if __name__ == "__main__":
    unittest.main()
