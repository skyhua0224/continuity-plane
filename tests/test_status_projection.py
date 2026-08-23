"""Current-only STATUS projection behavior."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.status_projection import render_status_projection


class StatusProjectionTests(unittest.TestCase):
    def _packet(self) -> dict:
        return {
            "project_id": "sample-app",
            "revision": 7,
            "event_head": {"sequence_no": 7, "event_sha256": "a" * 64},
            "active_work": {
                "work_id": "work-current",
                "title": "Continue the current delivery",
                "status": "active",
                "scope_refs": [{"scope_kind": "repo", "scope_ref": "repo://sample-app"}],
                "evidence_ids": ["evidence-current"],
            },
            "claim": {"claim_id": "claim-current", "status": "active", "lease_expires_at": "2026-08-22T00:00:00+00:00"},
            "checkpoint_ref": {"artifact_uri": "artifact://sha256/" + "b" * 64},
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "read_only": False,
            "open_blockers": [],
        }

    def test_projection_contains_current_route_and_no_history_section(self) -> None:
        packet = self._packet()
        rendered = render_status_projection(packet, language="zh-CN")
        self.assertIn("work-current", rendered)
        self.assertIn("claim-current", rendered)
        self.assertIn("continue-active-work", rendered)
        self.assertIn("revision", rendered)
        self.assertNotIn("历史", rendered)
        self.assertNotIn("completed", rendered)
        self.assertNotIn("return point", rendered.lower())

    def test_projection_rejects_unknown_language_and_missing_current_work(self) -> None:
        with self.assertRaises(ValueError):
            render_status_projection(self._packet(), language="fr")
        packet = self._packet()
        packet["active_work"] = None
        with self.assertRaises(ValueError):
            render_status_projection(packet, language="en")

    def test_projection_schema_is_strict_and_registry_hash_matches(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-11/status-projection.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.status-projection"
        )
        self.assertEqual(
            entry["content_sha256"], hashlib.sha256(schema_path.read_bytes()).hexdigest()
        )

    def test_projection_escapes_markdown_cells(self) -> None:
        packet = self._packet()
        packet["active_work"]["title"] = "Measure A | B\nwithout another row"
        rendered = render_status_projection(packet, language="en")
        self.assertIn("Measure A \\| B<br>without another row", rendered)
        self.assertNotIn("| Measure A | B\n", rendered)


if __name__ == "__main__":
    unittest.main()
