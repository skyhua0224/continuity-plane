"""Adopter entrypoints, executable CLI contracts and privacy-safe workflow evidence."""

import importlib.util
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import patch

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
                "commands.md",
                "commands.en.md",
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
                "agent-start.md",
                "agent-start.en.md",
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

    def readme_commands(self, path):
        for block in re.findall(r"```bash\n(.*?)```", path.read_text(), re.S):
            for line in block.replace("\\\n", " ").splitlines():
                args = shlex.split(line, comments=True)
                if args and args[0] == "continuity":
                    yield args[1:]

    def test_homepages_explain_core_surfaces_with_real_commands(self):
        required = {
            ("todo", "compile"),
            ("todo", "list"),
            ("todo", "complete"),
            ("todo", "from-cards"),
            ("collaboration", "task", "add"),
            ("collaboration", "task", "update"),
            ("collaboration", "packet"),
            ("collaboration", "inbox", "send"),
            ("collaboration", "changes"),
            ("collaboration", "wait"),
            ("worker", "configure"),
            ("worker", "resume"),
            ("dispatch",),
            ("report",),
            ("context", "lookup"),
            ("context", "dependents"),
            ("collaboration", "rule-add"),
            ("rules", "export"),
            ("memory", "add"),
            ("reference", "add"),
            ("handoff", "write"),
            ("collaboration", "commitment-add"),
            ("observe", "report"),
        }
        # The homepage is a product page; the complete surface lives in the
        # command reference, which must stay complete and executable.
        for name in ("commands.md", "commands.en.md"):
            text = (self.docs / name).read_text()
            families = next(
                a
                for a in build_parser()._actions
                if isinstance(a, argparse._SubParsersAction)
            ).choices
            for family in families:
                self.assertRegex(
                    text.casefold(),
                    r"(?<![\w-])" + re.escape(family) + r"(?![\w-])",
                    (name, family),
                )
            commands = list(self.readme_commands(self.docs / name))
            for prefix in required:
                self.assertTrue(
                    any(tuple(c[: len(prefix)]) == prefix for c in commands),
                    (name, prefix),
                )
            for token in (
                "continuity_plan",
                "continuity_help",
                "continuity_context_lookup",
                "PreCompact",
                "PostCompact",
                "PreToolUse",
                "UserPromptSubmit",
                "SubagentStart",
                "SubagentStop",
                "CONTINUITY_DISABLED",
                "controller",
                "supervisor daemon",
                "verification-json",
                "batch-add",
                "decision-add",
            ):
                self.assertIn(token, text, (name, token))
            for anchor in (
                "todo",
                "collaboration",
                "worker",
                "mcp",
                "hooks",
                "context",
                "knowledge",
                "handoff",
                "verification",
                "usage",
                "advanced",
            ):
                self.assertIn(f'<a id="{anchor}"></a>', text)

    def test_homepage_mcp_examples_match_actual_tool_contracts(self):
        from jsonschema import validate
        from continuity_plane.planning_api import tool_definitions
        from continuity_plane.context_mcp_server import _tool

        planning = next(t for t in tool_definitions() if t["name"] == "continuity_plan")
        for name in ("commands.md", "commands.en.md"):
            configs, actions, lookups = [], [], []
            for block in re.findall(
                r"```json\n(.*?)```", (self.docs / name).read_text(), re.S
            ):
                value = json.loads(block)
                if "mcpServers" in value:
                    configs.append(value)
                    self.assertEqual(
                        {k: v["command"] for k, v in value["mcpServers"].items()},
                        {
                            "continuity": "continuity-mcp",
                            "continuity-search": "continuity-search-mcp",
                        },
                    )
                elif "action" in value:
                    validate(value, planning["inputSchema"])
                    actions.append(value)
                elif "query" in value:
                    validate(value, _tool()["inputSchema"])
                    lookups.append(value)
            self.assertTrue(configs and actions and lookups, name)

    def test_homepage_planning_examples_execute_without_state_or_native_clients(self):
        from continuity_plane.cli import main

        for name in ("commands.md", "commands.en.md"):
            with self.subTest(readme=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "repository"
                root.mkdir()
                report = root / "fixture-report.md"
                report.write_text(
                    "Synthetic README verification report. No model requests.\n"
                )
                (root / "updates.json").write_text(
                    json.dumps(
                        {
                            "updates": [
                                {
                                    "task_id": "parser-fix",
                                    "body": "Synthetic README batch update.",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "CONTINUITY_DATA_ROOT": str(Path(directory) / "data"),
                    "CONTINUITY_ASSIGNEE": "controller",
                    "CONTINUITY_ROLE": "controller",
                }
                item_id = None
                executed = set()

                def call(args):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = main(args)
                    self.assertEqual(code, 0, args)
                    try:
                        return json.loads(output.getvalue())
                    except json.JSONDecodeError:
                        return output.getvalue()

                with patch.dict(os.environ, env, clear=True), contextlib.chdir(root):
                    for argv in self.readme_commands(self.docs / name):
                        selected = argv[0] in {
                            "todo",
                            "memory",
                            "reference",
                            "lesson",
                            "handoff",
                        } or (
                            argv[0] == "collaboration"
                            and argv[1]
                            in {
                                "register",
                                "task",
                                "packet",
                                "inbox",
                                "changes",
                                "rule-add",
                                "commitment-add",
                                "commitment-list",
                            }
                        )
                        if not selected or "--remote" in argv:
                            continue
                        if argv[:3] == ["collaboration", "inbox", "wait"]:
                            # Live streams have separate subprocess acceptance tests.
                            continue
                        argv = [
                            item_id
                            if v == "ITEM_ID"
                            else str(report)
                            if v == "/persistent/path/report.md"
                            else v
                            for v in argv
                        ]
                        result = call(argv)
                        executed.add(tuple(argv[:2]))
                        if argv[:2] == ["todo", "compile"]:
                            item_id = result["items"][0]["item_id"]
                        if argv[:2] == ["collaboration", "packet"]:
                            self.assertEqual(result["task"]["task_id"], "parser-fix")
                    card = call(
                        [
                            "collaboration",
                            "task",
                            "show",
                            "--project-id",
                            "demo",
                            "--task-id",
                            "parser-fix",
                            "--include-report",
                        ]
                    )
                    self.assertEqual(card["status"], "ready-for-review")
                    self.assertEqual(
                        card["details"]["report"]["text"], report.read_text()
                    )
                    self.assertFalse((root / ".continuity").exists())
                    self.assertTrue(
                        {
                            ("todo", "compile"),
                            ("todo", "complete"),
                            ("todo", "from-cards"),
                            ("collaboration", "inbox"),
                            ("handoff", "write"),
                        }
                        <= executed
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
            self.assertIn(VERSION if name.startswith("deployment") else "report", text)

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
        self.assertEqual(len(value["cases"]), 14)
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
        self.assertEqual(
            receipt["observed_boundaries"]["report_after_early_card_update"],
            "attached",
        )


if __name__ == "__main__":
    unittest.main()
