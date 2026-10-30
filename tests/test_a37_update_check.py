"""A37 update detection and explicit self-update.

The network is never reached: every fetch and spawn is injected, and the default
fetcher is replaced by one that fails the test if it is called.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane import update_check as uc
from continuity_plane.cli import entrypoint

DAY = 86400


class NetworkTouched(BaseException):
    """Not an Exception, so the module's silent-failure handlers cannot hide it."""


def _forbid_network(*_args, **_kwargs):
    raise NetworkTouched("the network must not be reached")


def _pypi(*versions, yanked=()):
    return {
        "info": {"version": versions[-1] if versions else ""},
        "releases": {
            v: [{"filename": f"x-{v}.whl", "yanked": v in yanked}] for v in versions
        },
    }


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._dir.cleanup)
        self.data = Path(self._dir.name) / "data"
        self.data.mkdir()
        env = patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CONTINUITY_UPDATE_CHECK", None)
        os.environ.pop("CONTINUITY_AUTO_UPDATE", None)


class VersionOrderTests(unittest.TestCase):
    def test_pep440_ordering_including_alpha_forms(self):
        parse = uc.parse_version
        self.assertLess(parse("0.1.0a9"), parse("0.1.0a10"))
        self.assertLess(parse("0.1.0a36"), parse("0.1.0b1"))
        self.assertLess(parse("0.1.0b1"), parse("0.1.0rc1"))
        self.assertLess(parse("0.1.0rc1"), parse("0.1.0"))
        self.assertLess(parse("0.1.0"), parse("0.1.0.post1"))
        self.assertLess(parse("0.1.0.dev1"), parse("0.1.0a1"))
        self.assertLess(parse("0.1.0"), parse("0.1.1"))
        self.assertLess(parse("0.9"), parse("0.10"))
        self.assertEqual(parse("0.1.0-alpha.34"), parse("0.1.0a34"))
        self.assertEqual(parse("1.0"), parse("1.0.0"))

    def test_unparsable_versions_are_none(self):
        for text in ("", "latest", "1.x", None, 5):
            self.assertIsNone(uc.parse_version(text))


class LatestSelectionTests(unittest.TestCase):
    def test_prerelease_current_sees_prereleases(self):
        data = _pypi("0.1.0a35", "0.1.0a36", "0.1.0a37")
        self.assertEqual(uc.latest_from_metadata(data, "0.1.0a36"), "0.1.0a37")

    def test_final_current_ignores_prereleases(self):
        data = _pypi("0.1.0", "0.2.0a1")
        self.assertEqual(uc.latest_from_metadata(data, "0.1.0"), "0.1.0")

    def test_yanked_and_empty_releases_are_skipped(self):
        data = _pypi("0.1.0a36", "0.1.0a37", yanked=("0.1.0a37",))
        data["releases"]["0.1.0a38"] = []
        self.assertEqual(uc.latest_from_metadata(data, "0.1.0a36"), "0.1.0a36")

    def test_garbage_metadata_is_none(self):
        for data in (None, [], {}, {"releases": []}, {"releases": {"x": 1}}):
            self.assertIsNone(uc.latest_from_metadata(data, "0.1.0a36"))


class EligibilityTests(_Tmp):
    def test_private_package_and_source_install_do_not_report(self):
        with patch.object(uc, "_distribution", return_value="continuity"):
            self.assertFalse(uc.reporting_enabled())
        with patch.object(uc, "_distribution", return_value="continuity-plane"), \
                patch.object(uc, "_install_origin", return_value="editable"):
            self.assertFalse(uc.reporting_enabled())
        with patch.object(uc, "_distribution", return_value="continuity-plane"), \
                patch.object(uc, "_install_origin", return_value="source"):
            self.assertFalse(uc.reporting_enabled())
        with patch.object(uc, "_distribution", return_value="continuity-plane"), \
                patch.object(uc, "_install_origin", return_value="index"):
            self.assertTrue(uc.reporting_enabled())

    def test_environment_switch_disables_everything(self):
        with patch.object(uc, "_distribution", return_value="continuity-plane"), \
                patch.object(uc, "_install_origin", return_value="index"):
            for value in ("0", "false", "off"):
                with patch.dict(os.environ, {"CONTINUITY_UPDATE_CHECK": value}):
                    self.assertFalse(uc.reporting_enabled())

    def test_install_origin_reads_direct_url(self):
        class Dist:
            def __init__(self, text):
                self.text = text

            def read_text(self, name):
                return self.text

        cases = {
            None: "index",
            json.dumps({"url": "file:///x", "dir_info": {"editable": True}}): "editable",
            json.dumps({"url": "file:///x", "dir_info": {}}): "source",
            json.dumps({"url": "https://x/y.whl", "archive_info": {}}): "source",
        }
        for text, expected in cases.items():
            with patch.object(uc, "_dist", return_value=Dist(text)):
                self.assertEqual(uc._install_origin(), expected)
        with patch.object(uc, "_dist", return_value=None):
            self.assertEqual(uc._install_origin(), "source")


class CacheAndRefreshTests(_Tmp):
    def setUp(self):
        super().setUp()
        patcher = patch.object(uc, "reporting_enabled", side_effect=lambda **k: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_default_fetch", "_default_spawn"):
            p = patch.object(uc, name, _forbid_network)
            p.start()
            self.addCleanup(p.stop)

    def test_refresh_stores_latest_and_only_requests_the_version_url(self):
        urls = []

        def fetch(url, timeout):
            urls.append(url)
            return _pypi("0.1.0a36", "0.1.0a37")

        uc.refresh(self.data, fetcher=fetch, current="0.1.0a36", now=1000.0)
        self.assertEqual(urls, ["https://pypi.org/pypi/continuity-plane/json"])
        state = json.loads((self.data / "update-check.json").read_text())
        self.assertEqual(state["latest"], "0.1.0a37")
        self.assertEqual(state["checked_at"], 1000.0)

    def test_refresh_failure_is_silent_and_keeps_old_latest(self):
        (self.data / "update-check.json").write_text(
            json.dumps({"latest": "0.1.0a37", "checked_at": 1.0})
        )

        def boom(url, timeout):
            raise OSError("offline")

        uc.refresh(self.data, fetcher=boom, current="0.1.0a36", now=500.0)
        state = json.loads((self.data / "update-check.json").read_text())
        self.assertEqual(state["latest"], "0.1.0a37")
        self.assertEqual(state["attempted_at"], 500.0)

    def test_refresh_never_runs_when_disabled(self):
        with patch.object(uc, "reporting_enabled", side_effect=lambda **k: False):
            uc.refresh(self.data, fetcher=_forbid_network, current="0.1.0a36")
        self.assertFalse((self.data / "update-check.json").exists())

    def test_spawns_at_most_once_per_day(self):
        spawned = []
        uc.maybe_refresh(self.data, now=10.0, spawner=lambda d: spawned.append(d))
        uc.maybe_refresh(self.data, now=10.0 + DAY - 1, spawner=lambda d: spawned.append(d))
        self.assertEqual(len(spawned), 1)
        uc.maybe_refresh(self.data, now=10.0 + DAY + 1, spawner=lambda d: spawned.append(d))
        self.assertEqual(len(spawned), 2)

    def test_fresh_success_blocks_spawn(self):
        (self.data / "update-check.json").write_text(
            json.dumps({"checked_at": 100.0, "latest": "0.1.0a36"})
        )
        spawned = []
        uc.maybe_refresh(self.data, now=100.0 + DAY - 5, spawner=lambda d: spawned.append(d))
        self.assertEqual(spawned, [])

    def test_spawn_failure_is_silent(self):
        def spawner(_data):
            raise OSError("no fork")

        uc.maybe_refresh(self.data, now=1.0, spawner=spawner)

    def test_available_reads_the_cache_only(self):
        self.assertIsNone(uc.available(self.data, current="0.1.0a36"))
        (self.data / "update-check.json").write_text(json.dumps({"latest": "0.1.0a37"}))
        self.assertEqual(
            uc.available(self.data, current="0.1.0a36"),
            {
                "current": "0.1.0a36",
                "latest": "0.1.0a37",
                "command": "continuity self-update",
            },
        )
        self.assertIsNone(uc.available(self.data, current="0.1.0a37"))
        self.assertIsNone(uc.available(self.data, current="0.1.0a38"))

    def test_corrupt_cache_is_ignored(self):
        (self.data / "update-check.json").write_text("{not json")
        self.assertIsNone(uc.available(self.data, current="0.1.0a36"))
        (self.data / "update-check.json").write_text(json.dumps({"latest": 5}))
        self.assertIsNone(uc.available(self.data, current="0.1.0a36"))


class SteadyStateCostTests(_Tmp):
    def test_no_install_metadata_is_read_when_nothing_is_due_or_pending(self):
        stamp = time.time()
        (self.data / "update-check.json").write_text(json.dumps({"latest": "0.1.0a36", "checked_at": stamp}))
        boom = patch.object(uc, "_install_origin", side_effect=NetworkTouched("metadata read"))
        with patch.object(uc, "_distribution", return_value="continuity-plane"), boom, \
                patch.object(uc, "_current", return_value="0.1.0a36"):
            uc.cli_notice(["task", "list"], io.StringIO(), data_root=self.data, now=stamp + 5)
            self.assertIsNone(uc.start_field(self.data))
            self.assertIsNone(uc.session_note({"hook_event_name": "SessionStart", "session_id": "s"}, self.data))

    def test_private_package_does_no_io_at_all(self):
        with patch.object(uc, "_distribution", return_value="a-private-build"), \
                patch.object(uc, "_read", side_effect=NetworkTouched("cache read")):
            uc.cli_notice(["task", "list"], io.StringIO(), data_root=self.data, now=1.0)
            self.assertIsNone(uc.start_field(self.data))


class CliNoticeTests(_Tmp):
    def setUp(self):
        super().setUp()
        for name, value in (
            ("reporting_enabled", lambda **k: True),
            ("_default_fetch", _forbid_network),
            ("_default_spawn", lambda data: None),
        ):
            p = patch.object(uc, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.data / "update-check.json").write_text(
            json.dumps({"latest": "0.1.0a99", "checked_at": 1000.0})
        )

    def _tty(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        return Tty()

    def test_notice_is_one_stderr_line_once_per_day(self):
        err = self._tty()
        uc.cli_notice(["task", "list"], err, data_root=self.data, now=1000.0)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("0.1.0a99", lines[0])
        self.assertIn("continuity self-update", lines[0])
        again = self._tty()
        uc.cli_notice(["task", "list"], again, data_root=self.data, now=1000.0 + DAY - 1)
        self.assertEqual(again.getvalue(), "")
        later = self._tty()
        uc.cli_notice(["task", "list"], later, data_root=self.data, now=1000.0 + DAY + 1)
        self.assertEqual(len(later.getvalue().splitlines()), 1)

    def test_no_notice_when_stderr_is_not_a_terminal(self):
        err = io.StringIO()
        uc.cli_notice(["task", "list"], err, data_root=self.data, now=1000.0)
        self.assertEqual(err.getvalue(), "")

    def test_no_notice_for_machine_commands(self):
        for words in (["self-update"], ["--version"], ["hook", "x"], ["mcp"]):
            err = self._tty()
            uc.cli_notice(words, err, data_root=self.data, now=1000.0)
            self.assertEqual(err.getvalue(), "", words)

    def test_notice_never_raises(self):
        class Broken:
            def isatty(self):
                raise RuntimeError

        uc.cli_notice(["x"], Broken(), data_root=self.data, now=1.0)

    def test_stdout_is_identical_with_and_without_pending_update(self):
        from continuity_plane import cli

        def run(latest):
            (self.data / "update-check.json").write_text(
                json.dumps({"latest": latest, "checked_at": time.time()})
            )
            out, err = io.StringIO(), self._tty()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    patch.object(uc, "_current", return_value="0.1.0a36"), \
                    patch.object(cli, "main", side_effect=lambda argv: print('{"a": 1}') or 0):
                code = entrypoint(["task", "list"])
            return code, out.getvalue(), err.getvalue()

        pending = run("0.1.0a99")
        none = run("0.1.0a36")
        self.assertEqual(pending[:2], none[:2])
        self.assertIn("0.1.0a99", pending[2])
        self.assertNotIn("0.1.0a99", pending[1])
        self.assertEqual(none[2], "")


class DetachedProcessTests(_Tmp):
    def test_refresh_runs_in_a_detached_isolated_interpreter(self):
        with patch("subprocess.Popen") as popen:
            uc._default_spawn(self.data)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:3], ["-I", "-m"])
        self.assertEqual(command[3], uc.__name__)
        self.assertEqual(command[4:], ["refresh"])
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["CONTINUITY_DATA_ROOT"], str(self.data))

    def test_module_entry_is_a_noop_when_disabled(self):
        with patch.dict(os.environ, {"CONTINUITY_UPDATE_CHECK": "0"}), \
                patch.object(uc, "_default_fetch", _forbid_network):
            self.assertEqual(uc.main(["refresh"]), 0)
        self.assertFalse((self.data / "update-check.json").exists())


class StartAndSessionTests(_Tmp):
    def setUp(self):
        super().setUp()
        for name, value in (
            ("reporting_enabled", lambda **k: True),
            ("_default_fetch", _forbid_network),
            ("_default_spawn", _forbid_network),
            ("_current", lambda: "0.1.0a36"),
        ):
            p = patch.object(uc, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.data / "update-check.json").write_text(
            json.dumps({"latest": "0.1.0a37", "checked_at": time.time()})
        )

    def test_continuity_start_adds_update_available_only_when_pending(self):
        from continuity_plane import agent_tools

        with patch.object(agent_tools, "_plan", return_value={}):
            result = agent_tools._start({}, "p", None)
        self.assertEqual(
            result["update_available"],
            {"current": "0.1.0a36", "latest": "0.1.0a37", "command": "continuity self-update"},
        )
        (self.data / "update-check.json").write_text(
            json.dumps({"latest": "0.1.0a36", "checked_at": time.time()})
        )
        with patch.object(agent_tools, "_plan", return_value={}):
            self.assertNotIn("update_available", agent_tools._start({}, "p", None))

    def test_session_note_once_per_version_per_session(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1"}
        first = uc.session_note(payload, self.data, max_bytes=1000)
        self.assertIn("0.1.0a37", first)
        self.assertIn("continuity self-update", first)
        self.assertIn("only if the user allows", first)
        self.assertIsNone(uc.session_note(payload, self.data, max_bytes=1000))
        other = {"hook_event_name": "SessionStart", "session_id": "s2"}
        self.assertIsNotNone(uc.session_note(other, self.data, max_bytes=1000))
        # A newer version in the same session is announced again.
        (self.data / "update-check.json").write_text(
            json.dumps({**json.loads((self.data / "update-check.json").read_text()), "latest": "0.1.0a38"})
        )
        again = uc.session_note(payload, self.data, max_bytes=1000)
        self.assertIn("0.1.0a38", again)

    def test_session_note_respects_budget_and_does_not_mark_when_dropped(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1"}
        self.assertIsNone(uc.session_note(payload, self.data, max_bytes=20))
        self.assertIsNotNone(uc.session_note(payload, self.data, max_bytes=1000))

    def test_session_note_only_at_session_start(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}
        self.assertIsNone(uc.session_note(payload, self.data, max_bytes=1000))

    def test_merge_session_notice_charges_the_hook_budget(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1"}
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "x" * 100}}
        uc.merge_session_notice(output, payload, 120, data_root=self.data)
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], "x" * 100)
        uc.merge_session_notice(output, payload, 4096, data_root=self.data)
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("x" * 100 + "\n"))
        self.assertLessEqual(len(text.encode()), 4096)

    def test_auto_update_applies_at_session_start_only_when_no_workers(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1"}
        calls = []
        with patch.dict(os.environ, {"CONTINUITY_AUTO_UPDATE": "1"}), \
                patch.object(uc, "running_workers", return_value=[{"job_id": "j"}]), \
                patch.object(uc, "spawn_apply", side_effect=lambda *a: calls.append(a)):
            note = uc.session_note(payload, self.data, max_bytes=1000)
        self.assertEqual(calls, [])
        self.assertIn("only if the user allows", note)
        with patch.dict(os.environ, {"CONTINUITY_AUTO_UPDATE": "1"}), \
                patch.object(uc, "running_workers", return_value=[]), \
                patch.object(uc, "spawn_apply", side_effect=lambda *a: calls.append(a)):
            uc.session_note({**payload, "session_id": "s9"}, self.data, max_bytes=1000)
        self.assertEqual(len(calls), 1)

    def test_no_auto_update_by_default(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1"}
        with patch.object(uc, "spawn_apply", side_effect=AssertionError("silent install")):
            uc.session_note(payload, self.data, max_bytes=1000)


class InstallMethodTests(unittest.TestCase):
    def test_detects_pipx_uv_and_venv(self):
        with patch.object(uc, "_install_origin", return_value="index"):
            pipx = uc.install_method(prefix="/h/.local/share/pipx/venvs/continuity-plane", base_prefix="/usr")
            uv = uc.install_method(prefix="/h/.local/share/uv/tools/continuity-plane", base_prefix="/usr")
            venv = uc.install_method(prefix="/w/.venv", base_prefix="/usr")
            system = uc.install_method(prefix="/usr", base_prefix="/usr")
        self.assertEqual((pipx, uv, venv, system), ("pipx", "uv", "venv", "system"))

    def test_editable_and_source_are_reported_as_such(self):
        for origin in ("editable", "source"):
            with patch.object(uc, "_install_origin", return_value=origin):
                self.assertEqual(
                    uc.install_method(prefix="/w/.venv", base_prefix="/usr"), origin
                )

    def test_upgrade_commands_include_pre_flags_for_prerelease_targets(self):
        pipx = uc.upgrade_command("pipx", "0.1.0a37", python="/p/python")
        self.assertEqual(pipx[:2], ["pipx", "upgrade"])
        self.assertIn("--pip-args=--pre", pipx)
        self.assertEqual(pipx[-1], "continuity-plane")
        uv = uc.upgrade_command("uv", "0.1.0a37", python="/p/python")
        self.assertEqual(uv[:3], ["uv", "tool", "upgrade"])
        self.assertIn("--prerelease=allow", uv)
        venv = uc.upgrade_command("venv", "0.1.0a37", python="/p/python")
        self.assertEqual(venv[:4], ["/p/python", "-m", "pip", "install"])
        self.assertIn("--pre", venv)
        self.assertIn("--upgrade", venv)
        final = uc.upgrade_command("venv", "0.2.0", python="/p/python")
        self.assertNotIn("--pre", final)
        self.assertIsNone(uc.upgrade_command("editable", "0.2.0", python="x"))


class SelfUpdateTests(_Tmp):
    def setUp(self):
        super().setUp()
        for name, value in (
            ("_default_fetch", _forbid_network),
            ("_default_spawn", _forbid_network),
            ("_current", lambda: "0.1.0a36"),
            ("_distribution", lambda: "continuity-plane"),
            ("reporting_enabled", lambda **k: True),
        ):
            p = patch.object(uc, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.ran = []

    def _run(self, **overrides):
        options = dict(
            data_root=self.data,
            fetcher=lambda url, timeout: _pypi("0.1.0a36", "0.1.0a37"),
            runner=lambda cmd: self.ran.append(cmd) or 0,
            workers=lambda data: [],
            method="pipx",
            force=False,
            dry_run=False,
        )
        options.update(overrides)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = uc.self_update(**options)
        return code, json.loads(out.getvalue())

    def test_upgrades_with_the_detected_method_and_reports_reconnect_steps(self):
        code, report = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "updated")
        self.assertEqual(self.ran[0][:2], ["pipx", "upgrade"])
        self.assertEqual(report["from"], "0.1.0a36")
        self.assertEqual(report["to"], "0.1.0a37")
        joined = " ".join(report["next"]).lower()
        self.assertIn("mcp", joined)
        self.assertIn("plugin", joined)

    def test_refuses_editable_and_source_installs_with_guidance(self):
        for method in ("editable", "source", "system"):
            self.ran.clear()
            code, report = self._run(method=method)
            self.assertEqual(code, 2, method)
            self.assertEqual(report["status"], "refused")
            self.assertTrue(report["hint"])
            self.assertEqual(self.ran, [])

    def test_defers_while_workers_run_unless_forced(self):
        busy = lambda data: [{"job_id": "j1", "project_id": "p"}]
        code, report = self._run(workers=busy)
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "deferred")
        self.assertEqual(report["running_workers"], [{"job_id": "j1", "project_id": "p"}])
        self.assertEqual(self.ran, [])
        code, report = self._run(workers=busy, force=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "updated")

    def test_unreadable_worker_state_defers(self):
        def broken(data):
            raise ValueError("unreadable")

        code, report = self._run(workers=broken)
        self.assertEqual(code, 3)
        self.assertEqual(self.ran, [])

    def test_up_to_date_does_nothing(self):
        code, report = self._run(fetcher=lambda u, t: _pypi("0.1.0a35", "0.1.0a36"))
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "up-to-date")
        self.assertEqual(self.ran, [])

    def test_dry_run_prints_the_command_without_running_it(self):
        code, report = self._run(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "dry-run")
        self.assertEqual(report["command"][:2], ["pipx", "upgrade"])
        self.assertEqual(self.ran, [])

    def test_failed_upgrade_is_reported_not_hidden(self):
        code, report = self._run(runner=lambda cmd: 1)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")

    def test_lookup_failure_still_reports(self):
        def offline(url, timeout):
            raise OSError("offline")

        code, report = self._run(fetcher=offline)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "lookup-failed")
        self.assertEqual(self.ran, [])

    def test_success_clears_the_pending_notice(self):
        (self.data / "update-check.json").write_text(json.dumps({"latest": "0.1.0a37"}))
        self._run()
        state = json.loads((self.data / "update-check.json").read_text())
        self.assertNotIn("latest", state)

    def test_disabled_check_skips_our_lookup(self):
        with patch.dict(os.environ, {"CONTINUITY_UPDATE_CHECK": "0"}):
            code, report = self._run(fetcher=_forbid_network)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "updated")
        self.assertNotIn("to", report)


class RunningWorkersTests(_Tmp):
    def _job(self, project, job_id, pid, host):
        base = self.data / "projects" / project
        (base / "worker-jobs" / job_id).mkdir(parents=True, exist_ok=True)
        (base / "project.json").write_text(json.dumps({"project_id": project, "control_root": "/x"}))
        index_path = base / "worker-jobs.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        index["t-" + job_id] = {"job_id": job_id, "job_status": "running"}
        index_path.write_text(json.dumps(index))
        (base / "worker-jobs" / job_id / "job.json").write_text(
            json.dumps({"runner_pid": pid, "runner_host": host})
        )

    def test_only_live_local_runners_count(self):
        import socket

        self._job("p", "live", os.getpid(), socket.gethostname())
        self._job("p", "dead", 2**22 + 7, socket.gethostname())
        self._job("p", "remote", os.getpid(), "some-other-host")
        found = uc.running_workers(self.data)
        self.assertEqual([row["job_id"] for row in found], ["live"])

    def test_no_projects_means_no_workers(self):
        self.assertEqual(uc.running_workers(self.data), [])


class VersionSkewTests(_Tmp):
    def test_reports_cli_mcp_and_plugin_versions_and_skew(self):
        with patch.object(uc, "_current", return_value="0.1.0a37"), \
                patch.object(uc, "_mcp_version", return_value="0.1.0a36"), \
                patch.object(uc, "_plugin_versions", return_value={"codex": "0.1.0-alpha.34"}):
            report = uc.version_report()
        self.assertEqual(report["cli"], "0.1.0a37")
        self.assertEqual(report["mcp"], "0.1.0a36")
        self.assertEqual(report["plugins"], {"codex": "0.1.0-alpha.34"})
        self.assertTrue(report["skew"])
        text = " ".join(report["skew"])
        self.assertIn("mcp", text)
        self.assertIn("plugin", text)

    def test_no_skew_when_everything_matches(self):
        with patch.object(uc, "_current", return_value="0.1.0a37"), \
                patch.object(uc, "_mcp_version", return_value="0.1.0a37"), \
                patch.object(uc, "_plugin_versions", return_value={"claude": "0.1.0-alpha.37"}):
            report = uc.version_report()
        self.assertEqual(report["skew"], [])

    def test_unknown_components_are_not_skew(self):
        with patch.object(uc, "_current", return_value="0.1.0a37"), \
                patch.object(uc, "_mcp_version", return_value=None), \
                patch.object(uc, "_plugin_versions", return_value={}):
            report = uc.version_report()
        self.assertEqual(report["skew"], [])
        self.assertIsNone(report["mcp"])

    def test_doctor_includes_versions(self):
        from continuity_plane import cli

        source = Path(cli.__file__).read_text()
        self.assertIn("version_report", source)


class LazyImportTests(unittest.TestCase):
    def test_cli_import_does_not_load_the_update_module(self):
        code = (
            "import sys, continuity_plane.cli as c;"
            "print('continuity_plane.update_check' in sys.modules)"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(done.stdout.strip(), "False", done.stderr)

    def test_self_update_command_is_registered(self):
        from continuity_plane.cli import build_parser

        args = build_parser().parse_args(["self-update", "--dry-run", "--force"])
        self.assertTrue(args.dry_run and args.force)


if __name__ == "__main__":
    unittest.main()
