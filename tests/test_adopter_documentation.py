"""Adopter entrypoints, executable CLI contracts and privacy-safe workflow evidence."""

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import unittest

from continuity_plane.cli import build_parser
from continuity_plane.release_version import VERSION


class AdopterDocumentationTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]
    docs = root / "public/docs" if (root / "public/docs").exists() else root / "docs"

    def entrypoints(self):
        return [
            self.root / name
            for name in ("README.md", "README.en.md", "USAGE.md", "USAGE.en.md")
        ] + [
            self.docs / name
            for name in (
                "README.md",
                "README.en.md",
                "deployment.md",
                "deployment.en.md",
                "workflows.md",
                "workflows.en.md",
                "state-workflows.md",
                "state-workflows.en.md",
                "validation.md",
                "validation.en.md",
                "releases.md",
                "releases.en.md",
            )
        ]

    def test_entrypoints_have_real_local_link_targets(self):
        for path in self.entrypoints():
            self.assertTrue(path.is_file(), path.name)
            for target in re.findall(
                r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")
            ):
                target = target.split("#", 1)[0]
                if not target or re.match(r"[a-z]+:", target):
                    continue
                self.assertTrue(
                    (path.parent / target).is_file(), f"{path.name}: {target}"
                )

    def test_documented_cli_examples_parse_on_the_current_contract(self):
        parser = build_parser()
        count = 0
        for path in self.entrypoints():
            text = path.read_text(encoding="utf-8")
            for block in re.findall(r"```bash\n(.*?)```", text, re.S):
                for line in block.replace("\\\n", " ").splitlines():
                    argv = shlex.split(line, comments=True)
                    if not argv or Path(argv[0]).name != "continuity":
                        continue
                    with self.subTest(document=path.name, command=line):
                        with contextlib.redirect_stdout(io.StringIO()):
                            try:
                                parser.parse_args(argv[1:])
                            except SystemExit as exc:
                                self.assertEqual(exc.code, 0)
                    count += 1
        self.assertGreater(count, 55)

    def test_current_guides_do_not_present_historical_savings_as_current(self):
        for name in ("README.md", "README.en.md", "USAGE.md", "USAGE.en.md"):
            text = (self.root / name).read_text(encoding="utf-8")
            self.assertIn(f"continuity-plane=={VERSION}", text)
            self.assertNotIn("95.06%", text)
            self.assertNotIn("40.25%", text)
            self.assertNotIn("least-planned-first", text)
        for name in (
            "deployment.md",
            "deployment.en.md",
            "workflows.md",
            "workflows.en.md",
        ):
            text = (self.docs / name).read_text(encoding="utf-8")
            self.assertIn(
                "0.1.0a25" if name.startswith("deployment") else "report", text
            )

    def test_three_primary_paths_have_at_most_ten_commands(self):
        for name, headings in (
            ("workflows.md", ("一个人对抗压缩", "一个主控与两个执行方", "一个团队")),
            (
                "workflows.en.md",
                (
                    "Solo work across compaction",
                    "One controller and two workers",
                    "A team",
                ),
            ),
        ):
            text = (self.docs / name).read_text(encoding="utf-8")
            for heading in headings:
                section = re.search(
                    r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)",
                    text,
                    re.M | re.S,
                )
                block = re.search(r"```bash\n(.*?)```", section[1], re.S)[1]
                count = sum(
                    line.startswith("continuity ") for line in block.splitlines()
                )
                self.assertGreater(count, 0)
                self.assertLessEqual(count, 10)

    def test_checked_in_receipt_contains_only_public_aggregates(self):
        base = (
            self.root / "public"
            if (self.root / "public/benchmarks").exists()
            else self.root
        )
        value = json.loads((base / "benchmarks/adopter-workflows.json").read_text())
        self.assertEqual(
            set(value),
            {
                "schema_version",
                "version",
                "dataset",
                "native_provider_requests",
                "cases",
                "measurements",
                "observed_boundaries",
                "limits",
            },
        )
        self.assertEqual(value["dataset"], "synthetic-only")
        self.assertEqual(value["version"], VERSION)
        self.assertEqual(value["native_provider_requests"], 0)
        self.assertEqual(len(value["cases"]), 13)
        self.assertTrue(all(value["cases"].values()))
        for key in (
            "host",
            "hostname",
            "profile",
            "session_id",
            "thread_id",
            "project_id",
            "root",
            "machine",
            "architecture",
        ):
            self.assertNotIn(key, value)
        for marker in ("/home/", "/Users/", "C:\\Users\\"):
            self.assertNotIn(marker, json.dumps(value))

    def test_synthetic_cli_workflows_reproduce_without_native_agents(self):
        base = (
            self.root / "public"
            if (self.root / "public/examples").exists()
            else self.root
        )
        source = base / "examples/verify_adopter_workflows.py"
        spec = importlib.util.spec_from_file_location("adopter_example", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cli = shutil.which("continuity")
        self.assertIsNotNone(
            cli, "install the package before exercising its console entrypoint"
        )
        with tempfile.TemporaryDirectory() as folder:
            receipt = module.verify(Path(folder) / "owned-fixture", cli)
        self.assertTrue(all(receipt["cases"].values()))
        self.assertLessEqual(receipt["measurements"]["lookup_bytes"], 1024)
        self.assertEqual(receipt["native_provider_requests"], 0)
        self.assertIn(
            receipt["observed_boundaries"]["report_after_early_card_update"],
            {"requires-explicit-attachment", "attached"},
        )


if __name__ == "__main__":
    unittest.main()
