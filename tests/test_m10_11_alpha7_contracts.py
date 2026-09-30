"""Strict contracts added by the alpha.7 cross-project pilot repair."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


class M1011Alpha7ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).parents[1]
        cls.registry = yaml.safe_load(
            (cls.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )

    def test_workspace_and_delivery_contracts_are_strict_and_registered(self) -> None:
        expected = {
            "context.git-workspace-binding": "schemas/m10-11/git-workspace-binding.schema.json",
            "context.delivery-activation": "schemas/m10-11/delivery-activation.schema.json",
            "context.delivery-workspace-registry": "schemas/m10-15/delivery-workspace-registry.schema.json",
            "context.codex-session-project-bindings": "schemas/m10-15/codex-session-project-bindings.schema.json",
        }
        for schema_id, relative in expected.items():
            with self.subTest(schema_id=schema_id):
                path = self.root / relative
                schema = json.loads(path.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in self.registry["schemas"]
                    if item["schema_id"] == schema_id
                )
                Draft202012Validator.check_schema(schema)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(entry["artifact_path"], relative)
                self.assertEqual(
                    entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
                )


if __name__ == "__main__":
    unittest.main()
