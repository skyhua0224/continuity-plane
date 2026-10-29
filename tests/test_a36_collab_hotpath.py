"""A36 collaboration hot-path regressions: indexed change pages, lazy start-up, output parity.

Every fast path is compared with the scan it replaces on a synthetic history of
more than 50,000 events so that the output stays byte-identical.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from continuity_plane import collaboration_registry as registry
from continuity_plane import journal_index
from continuity_plane.cli import _parse_args, build_parser, main
from continuity_plane.collaboration_registry import (
    _load_ledger,
    _record_change,
    _text,
    add_task,
    list_changes,
    project_data_dir,
    register_project,
    update_task,
)
from continuity_plane.controller_lease import view as lease_view
from continuity_plane.controller_observation import handoff_view, report_facts

EVENT_COUNT = 50_500
REPO = Path(__file__).resolve().parents[1]


def event_line(cursor: int) -> str:
    row = {
        "cursor": cursor,
        "event": "attempt.finished" if cursor % 997 == 0 else "fixture",
        "observed_at": f"2026-10-29T06:{cursor // 60 % 60:02d}:{cursor % 60:02d}+00:00",
        "task_id": f"task-{cursor % 7}",
        "attempt_id": f"attempt-{cursor}",
        "status": "completed",
    }
    if cursor % 5 == 0:
        row["event_id"] = f"event-{cursor}"
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def scan_changes(data, *, since=0, max_results=50):
    """The pre-index behaviour: parse the whole journal."""
    with patch.object(journal_index, "read_page", return_value=None):
        return list_changes(data, project_id="sample", since=since, max_results=max_results)


class HotPathFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.root = self.base / "repo"
        self.root.mkdir()
        register_project(self.data, project_id="sample", control_root=self.root)
        self.journal = project_data_dir(self.data, "sample") / "changes.jsonl"
        registry._LEDGER_MEMO.clear()

    def write_journal(self, lines, tail=b""):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_bytes(
            b"".join(line.encode("utf-8") + b"\n" for line in lines) + tail
        )

    def large_history(self):
        lines = [event_line(number) for number in range(1, EVENT_COUNT + 1)]
        # Out-of-order and duplicate cursors plus every flavour of corrupt line.
        lines[100], lines[101] = lines[101], lines[100]
        lines[2000] = lines[1999]
        lines[3000] = "{not json"
        lines[3001] = ""
        lines[3002] = "[1, 2]"
        lines[3003] = json.dumps({"cursor": True})
        lines[3004] = json.dumps({"cursor": 1.5})
        lines[3005] = json.dumps({"event": "no-cursor"})
        return lines

    def assert_pages_match_scan(self, grid=None):
        high = scan_changes(self.data, max_results=1)["high_water_cursor"]
        grid = grid or [
            (0, 50), (0, 500), (1, 1), (100, 500), (high - 50, 50), (high - 5, 50),
            (high - 1, 1), (high, 50), (high + 10, 50), (49_999, 500),
        ]
        for since, limit in grid:
            with self.subTest(since=since, limit=limit):
                expected = scan_changes(self.data, since=since, max_results=limit)
                actual = list_changes(self.data, project_id="sample", since=since, max_results=limit)
                self.assertEqual(
                    json.dumps(actual, sort_keys=True), json.dumps(expected, sort_keys=True)
                )


class IndexedChangePageTests(HotPathFixture):
    def test_large_history_pages_are_identical_to_a_scan(self):
        self.write_journal(self.large_history())
        self.assertIsNotNone(journal_index.read_page(self.journal, 0, 5))
        self.assert_pages_match_scan()
        page = list_changes(self.data, project_id="sample", since=EVENT_COUNT - 50)
        self.assertEqual(page["corrupt_lines"], 6)
        self.assertEqual(page["high_water_cursor"], EVENT_COUNT)

    def test_warm_page_does_not_parse_or_write_the_index(self):
        self.write_journal(self.large_history())
        list_changes(self.data, project_id="sample", since=EVENT_COUNT - 50)
        statements = []
        original = journal_index._open

        def traced(journal):
            connection = original(journal)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(journal_index, "_open", traced), patch(
            "continuity_plane.collaboration_registry.json.loads",
            wraps=json.loads,
        ) as loads:
            list_changes(self.data, project_id="sample", since=EVENT_COUNT - 50)
        self.assertLessEqual(loads.call_count, 50)
        self.assertFalse([s for s in statements if s.lstrip().upper().startswith(("BEGIN", "INSERT", "DELETE"))])

    def test_index_follows_appends_rewrites_truncation_and_replacement(self):
        self.write_journal(self.large_history())
        self.assert_pages_match_scan([(EVENT_COUNT - 10, 50), (0, 5)])
        for number in range(3):
            _record_change(self.data, "sample", "appended", None, number=number)
            self.assert_pages_match_scan([(EVENT_COUNT - 10, 50), (0, 5)])
        # In-place rewrite of the same inode with different, longer content.
        self.journal.write_text("\n".join(event_line(n) for n in range(1, 120)) + "\n")
        self.assert_pages_match_scan([(0, 500), (100, 50)])
        # Truncation, then replacement by a new inode.
        self.journal.write_text("\n".join(event_line(n) for n in range(1, 10)) + "\n")
        self.assert_pages_match_scan([(0, 500), (5, 50)])
        replacement = self.journal.with_name("replacement")
        replacement.write_text("\n".join(event_line(n) for n in range(40, 90)) + "\n")
        os.replace(replacement, self.journal)
        self.assert_pages_match_scan([(0, 500), (60, 50)])

    def test_irregular_journals_fall_back_to_the_scan(self):
        cases = {
            "unicode line separator": [event_line(1), '{"cursor": 2, "note": "a b"}', event_line(3)],
            "carriage return": [event_line(1), '{"cursor": 2}\r{"cursor": 3}', event_line(4)],
            "next line": [event_line(1), '{"cursor": 2, "note": "a\x85b"}'],
            "byte order mark": ["﻿" + event_line(1), event_line(2)],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_journal(lines)
                self.assertIsNone(journal_index.read_page(self.journal, 0, 50))
                self.assert_pages_match_scan([(0, 50), (1, 50)])
        with self.subTest("invalid utf-8"):
            self.journal.write_bytes(event_line(1).encode() + b"\n" + b'{"cursor": 2, "x": "\xff"}\n')
            self.assertIsNone(journal_index.read_page(self.journal, 0, 50))
        with self.subTest("unterminated tail"):
            self.write_journal([event_line(1), event_line(2)], tail=event_line(3).encode())
            self.assertIsNone(journal_index.read_page(self.journal, 0, 50))
            self.assert_pages_match_scan([(0, 50), (2, 50)])
            with self.journal.open("ab") as stream:
                stream.write(b"\n")
            self.assertIsNotNone(journal_index.read_page(self.journal, 0, 50))
            self.assert_pages_match_scan([(0, 50), (2, 50)])

    def test_trailing_carriage_return_stays_indexed(self):
        self.write_journal([event_line(1) + "\r", event_line(2) + "\r"])
        self.assertIsNotNone(journal_index.read_page(self.journal, 0, 50))
        self.assert_pages_match_scan([(0, 50), (1, 50)])

    def test_missing_empty_and_unusable_index_still_answer(self):
        self.assertEqual(
            list_changes(self.data, project_id="sample")["changes"], []
        )
        self.write_journal([])
        self.assert_pages_match_scan([(0, 50)])
        self.write_journal([event_line(n) for n in range(1, 30)])
        index = self.journal.with_name("changes-index.sqlite3")
        index.write_bytes(b"invalid cache")
        self.assert_pages_match_scan([(0, 50), (10, 5)])

    def test_index_written_by_the_previous_schema_is_migrated(self):
        self.write_journal([event_line(n) for n in range(1, 400)])
        index = self.journal.with_name("changes-index.sqlite3")
        info = self.journal.stat()
        connection = sqlite3.connect(index)
        connection.execute(
            "CREATE TABLE checkpoint (id INTEGER PRIMARY KEY, device INTEGER, inode INTEGER, offset INTEGER)"
        )
        connection.execute(
            "CREATE TABLE entries (offset INTEGER PRIMARY KEY, cursor INTEGER NOT NULL, event_id TEXT)"
        )
        connection.execute("INSERT INTO entries VALUES (0, 999999, NULL)")  # stale row
        connection.execute(
            "INSERT INTO checkpoint VALUES (1, ?, ?, ?)", (info.st_dev, info.st_ino, info.st_size)
        )
        connection.commit()
        connection.close()
        self.assertEqual(
            list_changes(self.data, project_id="sample", since=395)["high_water_cursor"], 399
        )
        self.assertIsNotNone(journal_index.read_page(self.journal, 0, 5))
        self.assert_pages_match_scan([(0, 500), (390, 50)])

    def test_event_lookup_and_cursor_offsets_use_the_same_index(self):
        for number in range(30):
            _record_change(self.data, "sample", "fixture", None, event_id=f"stable-{number}")
        again = _record_change(self.data, "sample", "fixture", None, event_id="stable-7")
        self.assertEqual(again["event_id"], "stable-7")
        self.assertEqual(list_changes(self.data, project_id="sample")["high_water_cursor"], 30)
        self.assertEqual(
            journal_index.offset_after(self.journal, 30), self.journal.stat().st_size
        )


class OutputParityTests(HotPathFixture):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(list(argv)), 0)
        return out.getvalue()

    def add_cards(self, count=40):
        for number in range(count):
            add_task(
                self.data,
                project_id="sample",
                task_id=f"card-{number}",
                lane_id="lane",
                mode="patch",
                priority="p1",
                title=f"Card {number}",
                objective="Do the work",
                next_action="Continue",
                exit_criteria=["done"],
                assignee="worker-sol" if number % 2 else None,
            )
        update_task(
            self.data, project_id="sample", task_id="card-1", status="ready-for-review", actor="worker-sol"
        )

    def test_changes_command_output_is_byte_identical(self):
        self.write_journal(self.large_history())
        argv = ["collaboration", "changes", "--project-id", "sample", "--data-root", str(self.data)]
        for since in (0, EVENT_COUNT - 50, EVENT_COUNT):
            args = [*argv, "--since", str(since), "--max-output-bytes", "4096"]
            indexed = self.run_cli(*args)
            with patch.object(journal_index, "read_page", return_value=None):
                scanned = self.run_cli(*args)
            self.assertEqual(indexed, scanned)

    def test_task_commands_are_identical_with_and_without_the_ledger_memo(self):
        self.add_cards()
        ledger = project_data_dir(self.data, "sample") / "dispatch.json"
        old = time.time() - 3600
        os.utime(ledger, (old, old))
        commands = [
            ["task", "list", "--project-id", "sample", "--data-root", str(self.data), "--status", "ready-for-review", "--summary"],
            ["task", "list", "--project-id", "sample", "--data-root", str(self.data), "--all"],
            ["task", "show", "--project-id", "sample", "--data-root", str(self.data), "--task-id", "card-3", "--format", "brief"],
        ]
        for command in commands:
            registry._LEDGER_MEMO.clear()
            warm = self.run_cli(*command)
            self.assertTrue(registry._LEDGER_MEMO, "an old ledger should be remembered")
            with patch.object(registry, "_stable_identity", return_value=None):
                cold = self.run_cli(*command)
            self.assertEqual(warm, cold)

    def test_handoff_view_matches_a_journal_scan_on_a_large_history(self):
        self.add_cards(10)
        self.write_journal(self.large_history())

        def stable(view):
            view = json.loads(json.dumps(view))
            view["observed"].pop("as_of")
            view["scene"].pop("as_of")
            return view

        indexed = stable(handoff_view(self.data, "sample"))
        self.assertEqual(indexed["observed"]["change_cursor"], EVENT_COUNT)
        self.assertEqual(indexed["observed"]["changes_since_handoff_total"], EVENT_COUNT - 6)
        with patch.object(journal_index, "offset_after", return_value=0):
            scanned = stable(handoff_view(self.data, "sample"))
        self.assertEqual(indexed, scanned)

    def test_lease_view_reuses_a_loaded_ledger_without_changing_its_answer(self):
        self.add_cards(3)
        ledger = _load_ledger(self.data, "sample")
        self.assertEqual(lease_view(self.data, "sample"), lease_view(self.data, "sample", ledger=ledger))
        ledger["controller_fencing_required"] = True
        self.assertTrue(lease_view(self.data, "sample", ledger=ledger)["read_only"])


class LedgerMemoTests(HotPathFixture):
    def make_card(self, number=0):
        add_task(
            self.data,
            project_id="sample",
            task_id=f"card-{number}",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Card",
            objective="Do the work",
            next_action="Continue",
            exit_criteria=["done"],
        )

    def age(self, path):
        old = time.time() - 3600
        os.utime(path, (old, old))

    def test_recent_writes_are_never_remembered(self):
        self.make_card()
        _load_ledger(self.data, "sample")
        self.assertEqual(registry._LEDGER_MEMO, {})

    def test_remembered_ledger_follows_file_replacement(self):
        self.make_card(0)
        ledger = project_data_dir(self.data, "sample") / "dispatch.json"
        self.age(ledger)
        self.assertEqual(len(_load_ledger(self.data, "sample")["tasks"]), 1)
        self.assertTrue(registry._LEDGER_MEMO)
        self.assertEqual(len(_load_ledger(self.data, "sample")["tasks"]), 1)
        self.make_card(1)  # atomic replace: new inode and mtime
        self.assertEqual(len(_load_ledger(self.data, "sample")["tasks"]), 2)

    def test_each_load_returns_a_private_document(self):
        self.make_card(0)
        ledger = project_data_dir(self.data, "sample") / "dispatch.json"
        self.age(ledger)
        first = _load_ledger(self.data, "sample")
        first["tasks"][0]["title"] = "mutated"
        second = _load_ledger(self.data, "sample")
        third = _load_ledger(self.data, "sample")
        self.assertEqual(second["tasks"][0]["title"], "Card")
        self.assertIsNot(second["tasks"][0], third["tasks"][0])

    def test_invalid_ledger_is_rejected_even_after_a_valid_one_was_remembered(self):
        self.make_card(0)
        ledger = project_data_dir(self.data, "sample") / "dispatch.json"
        self.age(ledger)
        _load_ledger(self.data, "sample")
        document = json.loads(ledger.read_text())
        document["tasks"][0]["status"] = "bogus"
        replacement = ledger.with_name("replacement")
        replacement.write_text(json.dumps(document))
        os.replace(replacement, ledger)
        self.age(ledger)
        for _ in range(2):
            with self.assertRaises(registry.CollaborationRegistryError):
                _load_ledger(self.data, "sample")


class TextValidationTests(unittest.TestCase):
    @staticmethod
    def reference(value, limit=4096, multiline=False):
        return not (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > limit
            or any(
                ord(char) < 32 and (not multiline or char not in "\n\r\t") for char in value
            )
        )

    def test_matches_the_character_loop_it_replaced(self):
        rng = random.Random(7)
        alphabet = "ab c\n\r\t\x00\x01\x08\x0b\x0c\x0e\x1f\x7f é中😀"
        samples = ["", " ", "x", "é" * 40, "\n", "a\tb", "\x1f", "x" * 4096, "x" * 4097, "é" * 2048, "é" * 2049]
        samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(3000)]
        for value in samples:
            for multiline in (False, True):
                for limit in (8, 4096):
                    try:
                        _text(value, "field", limit, multiline=multiline)
                        accepted = True
                    except registry.CollaborationRegistryError:
                        accepted = False
                    self.assertEqual(accepted, self.reference(value, limit, multiline), (value, multiline, limit))
        for value in (None, 3, b"x", ["x"]):
            with self.assertRaises(registry.CollaborationRegistryError):
                _text(value, "field")


class ReportFactsTests(HotPathFixture):
    @staticmethod
    def reference(data_root, project_id, cards, jobs):
        """The loop before the stat precheck: resolve every candidate path."""
        data = Path(data_root)
        directory = project_data_dir(data, project_id)
        work = Path(os.environ.get("CONTINUITY_WORK_ROOT") or data / "work").expanduser().resolve()
        job_root = (directory / "worker-jobs").resolve()
        found = []
        for card in cards:
            job = jobs.get(card["task_id"], {})
            candidates = [
                (work / project_id / card["task_id"] / "report.md", work / project_id, "project-scoped-work"),
                (work / card["task_id"] / "report.md", work, "unattributed-legacy-candidate"),
            ]
            if job.get("report_file") and job.get("task_id") == card["task_id"]:
                candidates.append((Path(job["report_file"]), job_root, "project-job-index"))
            for file, allowed, attribution in candidates:
                try:
                    resolved = file.resolve()
                    if not resolved.is_relative_to(allowed) or not resolved.is_file():
                        continue
                    info = resolved.stat()
                    if info.st_size <= 0:
                        continue
                except OSError:
                    continue
                found.append((card["task_id"], str(resolved), info.st_size, attribution))
        return found

    def test_report_discovery_matches_resolving_every_path(self):
        work = self.data / "work"
        outside = self.base / "outside"
        outside.mkdir()
        jobs_dir = project_data_dir(self.data, "sample") / "worker-jobs"
        jobs_dir.mkdir(parents=True)
        updated = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        cards = [{"task_id": f"task-{n}", "updated_at": updated} for n in range(300)]
        jobs = {}
        for number in range(0, 300, 7):
            folder = work / "sample" / f"task-{number}"
            folder.mkdir(parents=True)
            (folder / "report.md").write_text("report" if number % 14 else "")
        for number in range(3, 300, 11):
            folder = work / f"task-{number}"
            folder.mkdir(parents=True)
            (folder / "report.md").write_text("legacy")
        for number in range(5, 300, 40):
            report = jobs_dir / f"report-{number}.md"
            report.write_text("job report")
            jobs[f"task-{number}"] = {"task_id": f"task-{number}", "report_file": str(report)}
        stray = outside / "report.md"
        stray.write_text("outside")
        jobs["task-1"] = {"task_id": "task-1", "report_file": str(stray)}
        (work / "sample" / "task-2").mkdir(parents=True)
        os.symlink(stray, work / "sample" / "task-2" / "report.md")
        (work / "sample" / "task-4").mkdir(parents=True)
        (work / "sample" / "task-4" / "report.md").mkdir()
        expected = self.reference(self.data, "sample", cards, jobs)
        actual = [
            (row["task_id"], row["path"], row["bytes"], row["attribution"])
            for row in report_facts(self.data, "sample", cards, jobs)
        ]
        self.assertTrue(expected)
        self.assertEqual(actual, expected)


class LazyStartupTests(unittest.TestCase):
    def python(self, code):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CONTINUITY_")}
        env["PYTHONPATH"] = str(REPO)
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        ).stdout

    def test_importing_the_cli_skips_rarely_used_dependencies(self):
        output = self.python(
            "import sys, continuity_plane.cli as cli\n"
            "print(sorted(m for m in ('yaml','sqlite3','tomllib','uuid') if m in sys.modules))\n"
            "print(sorted(m for m in sys.modules if m in ('continuity_plane.state_mcp',"
            "'continuity_plane.code_index','continuity_plane.sqlite_state_store',"
            "'continuity_plane.local_state_bundle')))\n"
        )
        self.assertEqual(output.splitlines(), ["[]", "[]"])

    def test_code_fingerprint_is_hashed_on_first_use_not_at_import(self):
        output = self.python(
            "import continuity_plane.release_version as r\n"
            "print(r._started_fingerprint_cache is None)\n"
            "print(r.runtime_version_issue())\n"
            "print(r._started_fingerprint_cache is not None, r._STARTED_CODE_FINGERPRINT == r._code_fingerprint())\n"
            "print('_STARTED_CODE_FINGERPRINT' in vars(r))\n"
        )
        # Hashed on first use, cached privately, never written into the module
        # namespace where a temporary patch could leave a stale value behind.
        self.assertEqual(output.splitlines(), ["True", "None", "True True", "False"])

    def test_sources_changed_before_the_first_check_are_reported(self):
        from continuity_plane import release_version

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "card_todo.py").write_text("VALUE = 1\n")
            with patch.object(release_version, "__file__", str(root / "release_version.py")):
                started = release_version._code_stat()
                with patch.object(release_version, "_STARTED_CODE_STAT", started), patch.object(
                    release_version, "_installed_version", return_value=None
                ), patch.dict(vars(release_version)):
                    vars(release_version).pop("_STARTED_CODE_FINGERPRINT", None)
                    (root / "card_todo.py").write_text("VALUE = 22\n")
                    self.assertIn("restart this MCP process", release_version.runtime_version_issue())


def leaf_paths(parser, prefix=()):
    action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    yield prefix, parser
    if action is not None:
        for name, child in action.choices.items():
            yield from leaf_paths(child, (*prefix, name))


def required_arguments(parser):
    words = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction, argparse._VersionAction)):
            continue
        value = str(action.choices[0] if action.choices else 1 if action.type in (int, float) else "x")
        if action.option_strings and action.required:
            words += [action.option_strings[0]] if action.nargs == 0 else [action.option_strings[0], value]
        elif not action.option_strings and action.nargs not in ("*", "?"):
            words.append(value)
    return words


class PrunedParserTests(unittest.TestCase):
    @staticmethod
    def outcome(parse, words):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                namespace = parse(words)
            result = {k: getattr(v, "__name__", v) if callable(v) else v for k, v in vars(namespace).items()}
        except SystemExit as exc:
            result = ("exit", exc.code)
        return result, out.getvalue(), err.getvalue()

    def full(self, words):
        return build_parser().parse_args(words)

    def test_every_command_path_parses_like_the_full_parser(self):
        corpus = []
        for path, parser in leaf_paths(build_parser()):
            if not path:
                continue
            corpus.append([*path, *required_arguments(parser)])
            corpus.append([*path, "--bogus-flag"])
            corpus.append([*path, "--help"])
        self.assertGreater(len(corpus), 200)
        for words in corpus:
            with self.subTest(words=words):
                self.assertEqual(self.outcome(_parse_args, words), self.outcome(self.full, words))

    def test_errors_help_and_globals_are_unchanged(self):
        corpus = [
            [], ["bogus"], ["task"], ["task", "bogus"], ["--version"], ["-h"], ["task", "-h"],
            ["--remote", "host", "task", "list", "--project-id", "p"],
            ["--root", "/tmp", "handoff", "show", "--project-id", "p"],
            ["--rem", "host", "worker", "events", "--project-id", "p", "--since-cursor", "1"],
            ["collaboration", "changes", "--project-id", "p", "--since", "notint"],
            ["task", "list", "--project-id", "p", "--status", "ready-for-review", "--summary"],
            ["task", "list", "--project-id", "p", "--status", "conflict", "--mine"],
            ["task", "update", "--project-id", "p", "--task-id", "t", "--pr", "3", "--operation-id", "o"],
            ["task", "claim", "--project-id", "p", "--task-id", "t", "--operation-id", "o"],
            ["task", "handoff-request", "--project-id", "p", "--task-id", "t", "--operation-id", "o",
             "--expected-remote-revision", "1", "--to", "x", "--note", "n"],
            ["memory", "--help"], ["controller", "status", "--project-id", "p"],
        ]
        for words in corpus:
            with self.subTest(words=words):
                self.assertEqual(self.outcome(_parse_args, words), self.outcome(self.full, words))

    def test_benchmarked_commands_use_the_pruned_parser(self):
        for words in (
            ["collaboration", "changes", "--project-id", "p", "--since", "5"],
            ["task", "list", "--project-id", "p", "--summary"],
            ["task", "show", "--project-id", "p", "--task-id", "t"],
            ["handoff", "show", "--project-id", "p"],
            ["worker", "events", "--project-id", "p", "--since-cursor", "5"],
        ):
            with self.subTest(words=words), patch(
                "continuity_plane.cli.build_parser", wraps=build_parser
            ) as built:
                _parse_args(words)
            self.assertEqual(built.call_count, 1, "a fallback to the full parser was needed")


if __name__ == "__main__":
    unittest.main()
