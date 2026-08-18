"""M10-09 local state export, import, and rollback CLI tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateStore


class LocalStateBundleCliTests(unittest.TestCase):
    def test_bundle_manifest_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-09/local-state-bundle.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.local-state-bundle"
        )

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            entry["current_wire_version"], "context.local-state-bundle/v1alpha1"
        )
        self.assertEqual(entry["artifact_path"], schema_path.relative_to(root).as_posix())
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["file"]["additionalProperties"])

    def _init(self, root: Path, project_id: str = "sample-app") -> None:
        root.mkdir(parents=True, exist_ok=True)
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", project_id])

    def _state(self, root: Path) -> dict:
        return SQLiteStateStore(root / ".continuity/state.sqlite3").read_project(
            "sample-app"
        )

    def test_export_import_round_trip_preserves_state_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "state.continuity.zip"
            self._init(source)
            export_output = StringIO()

            with redirect_stdout(export_output):
                export_result = main(
                    ["export", "--root", str(source), "--output", str(bundle)]
                )
            import_output = StringIO()
            with redirect_stdout(import_output):
                import_result = main(
                    ["import", "--root", str(target), "--bundle", str(bundle)]
                )
            with redirect_stdout(StringIO()):
                verify_result = main(["verify", "--root", str(target)])

            exported = json.loads(export_output.getvalue())
            imported = json.loads(import_output.getvalue())
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "schemas/m10-09/local-state-bundle.schema.json"
                ).read_text(encoding="utf-8")
            )
            with zipfile.ZipFile(bundle, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            Draft202012Validator(schema).validate(manifest)
            self.assertEqual(export_result, 0)
            self.assertEqual(import_result, 0)
            self.assertEqual(verify_result, 0)
            self.assertEqual(exported["status"], "exported")
            self.assertEqual(imported["status"], "imported")
            self.assertRegex(exported["bundle_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(imported["bundle_sha256"], exported["bundle_sha256"])
            self.assertEqual(self._state(target), self._state(source))
            self.assertFalse((target / ".continuity/rollback/previous.zip").exists())

    def test_unregistered_traversal_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "state.continuity.zip"
            forged = base / "forged.continuity.zip"
            self._init(source)
            with redirect_stdout(StringIO()):
                main(["export", "--root", str(source), "--output", str(bundle)])
            with zipfile.ZipFile(bundle, "r") as archive, zipfile.ZipFile(
                forged, "w"
            ) as output:
                for item in archive.infolist():
                    output.writestr(item.filename, archive.read(item.filename))
                output.writestr("../escape", b"forbidden")

            with self.assertRaisesRegex(ValueError, "path|member"):
                main(["import", "--root", str(target), "--bundle", str(forged)])

            self.assertFalse((base / "escape").exists())
            self.assertFalse((target / ".continuity").exists())

    def test_replace_import_and_rollback_toggle_without_state_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "state.continuity.zip"
            self._init(source)
            self._init(target)
            original = self._state(target)
            (source / ".continuity/MASTER.md").write_text(
                "# Imported Master\n", encoding="utf-8"
            )
            with redirect_stdout(StringIO()):
                main(["export", "--root", str(source), "--output", str(bundle)])

            with redirect_stdout(StringIO()):
                main(
                    [
                        "import",
                        "--root",
                        str(target),
                        "--bundle",
                        str(bundle),
                        "--replace",
                    ]
                )
            self.assertEqual(
                (target / ".continuity/MASTER.md").read_text(encoding="utf-8"),
                "# Imported Master\n",
            )
            self.assertTrue((target / ".continuity/rollback/previous.zip").is_file())

            first_output = StringIO()
            with redirect_stdout(first_output):
                first_result = main(["rollback", "--root", str(target)])
            self.assertEqual(first_result, 0)
            self.assertEqual(json.loads(first_output.getvalue())["status"], "rolled-back")
            self.assertEqual(self._state(target), original)

            with redirect_stdout(StringIO()):
                main(["rollback", "--root", str(target)])
            self.assertEqual(
                (target / ".continuity/MASTER.md").read_text(encoding="utf-8"),
                "# Imported Master\n",
            )

    def test_tampered_bundle_is_rejected_without_replacing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            bundle = base / "state.continuity.zip"
            tampered = base / "tampered.continuity.zip"
            self._init(source)
            self._init(target)
            before = self._state(target)
            with redirect_stdout(StringIO()):
                main(["export", "--root", str(source), "--output", str(bundle)])
            with zipfile.ZipFile(bundle, "r") as archive, zipfile.ZipFile(
                tampered, "w"
            ) as output:
                for item in archive.infolist():
                    payload = archive.read(item.filename)
                    if item.filename == "state.sqlite3":
                        payload += b"tamper"
                    output.writestr(item.filename, payload)

            with self.assertRaisesRegex(ValueError, "digest|bundle"):
                main(
                    [
                        "import",
                        "--root",
                        str(target),
                        "--bundle",
                        str(tampered),
                        "--replace",
                    ]
                )

            self.assertEqual(self._state(target), before)
            self.assertFalse((target / ".continuity/rollback/previous.zip").exists())


if __name__ == "__main__":
    unittest.main()
