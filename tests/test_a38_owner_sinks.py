"""Webhook sinks, Telegram binding and the login service for the owner channel."""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from continuity_plane import owner_channel as oc
from continuity_plane import owner_service as svc
from continuity_plane import owner_sinks as sinks
from continuity_plane.collaboration_registry import register_project

try:
    from tests.test_a38_owner_channel import OWNER, STRANGER, FakeClient, message
except ImportError:  # unittest discover -s tests imports modules top-level
    from test_a38_owner_channel import OWNER, STRANGER, FakeClient, message


class Recorder:
    """Stands in for Apprise: records what each sink URL would receive."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, url):
        def send(body, image=None):
            if url in self.fail:
                raise RuntimeError("down")
            self.calls.append((url, body, image))
            return True

        return send


def private_file(directory, name, text):
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


class UrlAndTextTests(unittest.TestCase):
    def test_native_feishu_and_dingtalk_addresses_become_apprise_urls(self):
        self.assertEqual(
            sinks.normalize_url("https://open.feishu.cn/open-apis/bot/v2/hook/ab-12"),
            "feishu://ab-12",
        )
        self.assertEqual(
            sinks.normalize_url("https://oapi.dingtalk.com/robot/send?access_token=abc123"),
            "dingtalk://abc123",
        )
        self.assertEqual(
            sinks.normalize_url("https://oapi.dingtalk.com/robot/send?access_token=abc&secret=S%2B1/x"),
            "dingtalk://S%2B1%2Fx@abc",
        )
        other = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
        self.assertEqual(sinks.normalize_url(other), other)

    def test_telegram_html_becomes_plain_text(self):
        text = '<b>[demo]</b> <a href="https://t.me/bot?start=c_1">卡片 &lt;一&gt;</a> 完成'
        self.assertEqual(sinks.to_plain_text(text), "[demo] 卡片 <一> 完成")

    def test_button_labels_are_listed(self):
        keyboard = [[{"text": "批准", "callback_data": "p|y"}], [{"text": "拒绝", "callback_data": "p|n"}]]
        self.assertIn("在 Telegram 里点选：批准 · 拒绝", sinks.keyboard_hint(keyboard, True))
        self.assertIn("可选：批准 · 拒绝", sinks.keyboard_hint(keyboard, False))
        self.assertEqual(sinks.keyboard_hint(None, True), "")


class SecretFileTests(unittest.TestCase):
    def test_group_readable_secret_is_refused_on_posix_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = private_file(root, "url", "feishu://x")
            path.chmod(0o640)
            with mock.patch.object(sinks, "_WINDOWS", False):
                with self.assertRaises(sinks.SinkError):
                    sinks.read_secret(path)
            # Windows reports 0o666 for every writable file; the profile ACL protects it.
            with mock.patch.object(sinks, "_WINDOWS", True):
                self.assertEqual(sinks.read_secret(path), "feishu://x")


class MirrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        register_project(self.root, project_id="demo", control_root=self.root)
        self.recorder = Recorder(fail={"slack://down"})
        good = private_file(self.root, "feishu.url", "feishu://good")
        bad = private_file(self.root, "slack.url", "slack://down")
        self.logs = []
        self.sinks = sinks.Sinks(
            [{"name": "feishu", "url_file": str(good)}, {"name": "slack", "url_file": str(bad)}],
            factory=self.recorder,
            log=self.logs.append,
        )
        self.primary = FakeClient()
        self.client = sinks.MirroredClient(self.primary, self.sinks, interactive=True)
        self.channel = oc.OwnerChannel(
            self.client,
            {"owner_id": OWNER, "projects": ["demo"], "recipient": "claude-opus"},
            self.root,
        )

    def test_proactive_message_reaches_telegram_and_sinks(self):
        self.channel.notify("构建完成", options=["好", "再看看"])
        self.assertEqual(len(self.primary.sent), 1)
        url, body, image = self.recorder.calls[0]
        self.assertEqual(url, "feishu://good")
        self.assertTrue(body.startswith("[demo] 构建完成"))
        self.assertIn("好 · 再看看", body)
        self.assertIsNone(image)
        # The failing sink is logged by name only and does not stop delivery.
        self.assertEqual(self.logs[0], "sink slack failed: RuntimeError")

    def test_replies_to_owner_commands_stay_on_telegram(self):
        with sinks.quiet(self.client):
            self.channel.handle_update(message("/status"))
        self.assertTrue(self.primary.sent)
        self.assertEqual(self.recorder.calls, [])

    def test_photo_is_forwarded_with_its_caption(self):
        self.client.send_photo(OWNER, "/evidence/shot.png", "截图 1/2")
        self.assertEqual(self.recorder.calls[0][1:], ("截图 1/2", "/evidence/shot.png"))

    def test_only_rich_messages_lose_their_markup(self):
        self.client.send_message(OWNER, "a <b>x</b> &amp;", rich=True)
        self.client.send_message(OWNER, "plain <tag> &amp;")
        self.assertEqual([call[1] for call in self.recorder.calls], ["a x &", "plain <tag> &amp;"])

    def test_edits_are_not_mirrored(self):
        self.client.edit_message(OWNER, 1, "updated")
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(self.primary.edits[0][1], "updated")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.url = private_file(self.root, "feishu.url", "https://open.feishu.cn/open-apis/bot/v2/hook/t")
        patcher = mock.patch.object(sinks, "_apprise_notifier", Recorder())
        self.recorder = patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, **telegram):
        return oc.configure(self.root, projects=["demo"], recipient="controller", **telegram)

    def test_telegram_needs_both_owner_and_token_or_neither(self):
        with self.assertRaises(oc.OwnerChannelError):
            self.configure(owner_id=5, token_file=None)
        self.configure(owner_id=None, token_file=None)
        with self.assertRaises(oc.OwnerChannelError):
            oc.build_channel(self.root)

    def test_webhook_only_channel_delivers_through_sinks(self):
        self.configure(owner_id=None, token_file=None)
        oc.add_sink(self.root, name="feishu", url_file=str(self.url))
        channel = oc.build_channel(self.root)
        self.assertIsInstance(channel.client, sinks.MirroredClient)
        self.assertIsInstance(channel.client.primary, sinks.NullClient)
        channel.notify("夜间任务失败", options=["重试"])
        body = self.recorder.calls[-1][1]
        self.assertIn("夜间任务失败", body)
        self.assertIn("可选：重试", body)
        status = oc.channel_status(self.root)
        self.assertEqual((status["telegram"], status["sinks"], status["serving"]), (False, ["feishu"], False))

    def test_webhook_only_questions_and_approvals_name_another_path(self):
        from continuity_plane import owner_questions as oq
        from continuity_plane.collaboration_inbox import list_messages, send_message
        from continuity_plane.collaboration_registry import register_project

        register_project(self.root, project_id="demo", control_root=self.root)
        self.configure(owner_id=None, token_file=None)
        oc.add_sink(self.root, name="feishu", url_file=str(self.url))
        channel = oc.build_channel(self.root)
        item = oq.ask(self.root, project_id="demo", asker="agent", question="今晚部署？", options=["可以"])
        channel.deliver_questions()
        body = self.recorder.calls[-1][1]
        self.assertIn(f"continuity notify answer --project demo --question-id {item['question_id']}", body)
        card = {"owner_card": 1, "kind": "approval", "title": "发布", "text": "发布 a38", "risk": "low"}
        send_message(self.root, project_id="demo", sender="agent", recipient="owner", body=__import__("json").dumps(card))
        channel.deliver_pending()
        self.assertIn("不能在这里批准", self.recorder.calls[-1][1])
        self.assertNotIn("在 Telegram 里点选", self.recorder.calls[-1][1])
        told = list_messages(self.root, project_id="demo", recipient="agent", limit=10)
        self.assertTrue(any("无法在通道内批准" in m["body"] for m in told["messages"] if isinstance(told, dict)) if isinstance(told, dict) else any("无法在通道内批准" in m["body"] for m in told))

    def test_reconfigure_keeps_sinks_and_remove_drops_them(self):
        self.configure(owner_id=None, token_file=None)
        oc.add_sink(self.root, name="feishu", url_file=str(self.url))
        self.configure(owner_id=None, token_file=None)
        self.assertEqual(oc.load_config(self.root)["sinks"][0]["name"], "feishu")
        self.assertEqual(oc.test_sink(self.root), {"feishu": True})
        self.assertEqual(oc.remove_sink(self.root, name="feishu"), {"sinks": []})
        with self.assertRaises(oc.OwnerChannelError):
            oc.remove_sink(self.root, name="feishu")

    def test_sink_names_are_bounded(self):
        self.configure(owner_id=None, token_file=None)
        with self.assertRaises(sinks.SinkError):
            oc.add_sink(self.root, name="Feishu Bot", url_file=str(self.url))

    def test_config_stores_the_url_file_not_the_url(self):
        self.configure(owner_id=None, token_file=None)
        oc.add_sink(self.root, name="feishu", url_file=str(self.url))
        self.assertNotIn("open.feishu.cn", oc.config_path(self.root).read_text(encoding="utf-8"))

    def test_missing_extra_names_the_install_command(self):
        with mock.patch.dict("sys.modules", {"apprise": None}):
            with self.assertRaises(sinks.SinkError) as raised:
                ORIGINAL_NOTIFIER("feishu://x")
        self.assertIn("continuity-plane[webhooks]", str(raised.exception))


ORIGINAL_NOTIFIER = sinks._apprise_notifier


class BindTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        oc.configure(self.root, owner_id=None, token_file=None, projects=["demo"], recipient="c")
        self.token = private_file(self.root, "token", "123:abc")
        self.shown = []

    def bind(self, updates, confirm=lambda account: True):
        client = FakeClient(updates)
        ticks = iter(range(0, 10_000, 100))
        result = oc.bind(
            self.root,
            client,
            token_file=str(self.token),
            confirm=confirm,
            show=self.shown.append,
            code="c0de",
            clock=lambda: next(ticks),
        )
        return result, client

    def test_only_a_private_start_with_the_code_binds(self):
        updates = [
            message("/start bind-c0de", sender=STRANGER, chat=-9, kind="group", update_id=4),
            message("/start bind-wrong", sender=STRANGER, chat=STRANGER, update_id=5),
            message("/start bind-c0de", update_id=6),
        ]
        updates[2]["message"]["from"].update(username="owner", first_name="Sky")
        result, client = self.bind(updates)
        self.assertEqual(result, {"bound": True, "owner_id": OWNER, "bot": "demo_bot"})
        self.assertIn("https://t.me/demo_bot?start=bind-c0de", self.shown[0])
        config = oc.load_config(self.root)
        self.assertEqual((config["owner_id"], config["bot_username"]), (OWNER, "demo_bot"))
        self.assertEqual(client.sent[-1][0], OWNER)
        self.assertEqual(oc._read_state(self.root)["offset"], 7)
        self.assertTrue(oc.channel_status(self.root)["telegram"])

    def test_terminal_refusal_saves_nothing(self):
        result, client = self.bind([message("/start bind-c0de")], confirm=lambda account: False)
        self.assertEqual(result, {"bound": False})
        self.assertIsNone(oc.load_config(self.root)["owner_id"])
        self.assertIn("取消", client.sent[-1][1])

    def test_code_expires(self):
        with self.assertRaises(oc.OwnerChannelError):
            self.bind([])

    def test_offset_from_another_bot_is_not_reused_and_state_survives(self):
        oc._atomic_write(oc.state_path(self.root), {
            "schema_version": oc.STATE_SCHEMA_VERSION, "offset": 500, "bot_id": 1, "cards": {"7": {"project": "demo"}},
        })
        seen = []

        class OtherBot(FakeClient):
            def get_me(self):
                return {"username": "new_bot", "id": 2}

            def get_updates(self, offset, timeout):
                seen.append(offset)
                return super().get_updates(offset, timeout)

        client = OtherBot([message("/start bind-c0de", update_id=3)])
        ticks = iter(range(0, 10_000, 100))
        oc.bind(self.root, client, token_file=str(self.token), confirm=lambda a: True,
                show=self.shown.append, code="c0de", clock=lambda: next(ticks))
        self.assertIsNone(seen[0])
        state = oc._read_state(self.root)
        self.assertEqual((state["offset"], state["bot_id"], state["cards"]), (4, 2, {"7": {"project": "demo"}}))

    def test_reconfigure_without_telegram_flags_keeps_the_binding(self):
        self.bind([message("/start bind-c0de")])
        oc.configure(self.root, owner_id=None, token_file=None, projects=["demo", "other"], recipient="c")
        config = oc.load_config(self.root)
        self.assertEqual((config["owner_id"], config["projects"], config["bot_username"]), (OWNER, ["demo", "other"], "demo_bot"))

    def test_unbind_keeps_other_settings(self):
        self.bind([message("/start bind-c0de")])
        self.assertEqual(oc.unbind(self.root), {"telegram": False, "sinks": []})
        self.assertEqual(oc.load_config(self.root)["projects"], ["demo"])


class ServiceTests(unittest.TestCase):
    def test_serve_command_names_its_data_root_and_log(self):
        command = svc.serve_command("/data root")
        root = Path("/data root").resolve()
        self.assertEqual(command[1:5], ["-m", "continuity_plane.cli", "notify", "serve"])
        self.assertEqual(command[5:7], ["--data-root", str(root)])
        self.assertEqual(command[7:], ["--log-file", str(root / "owner-channel" / "serve.log")])
        # Without an explicit root the service still pins the one in effect now.
        with mock.patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": "/elsewhere"}):
            self.assertEqual(svc.serve_command(None)[6], str(Path("/elsewhere").resolve()))

    def test_systemd_unit_quotes_paths_with_spaces(self):
        unit = svc.systemd_unit(["/usr/bin/python3", "-m", "pkg.cli", "--data-root", "/a b/100%"])
        self.assertIn('ExecStart=/usr/bin/python3 -m pkg.cli --data-root "/a b/100%%"', unit)
        self.assertIn("Restart=on-failure", unit)

    def test_second_poller_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with svc.single_instance(root):
                with self.assertRaises(svc.ServiceError):
                    with svc.single_instance(root):
                        pass
            with svc.single_instance(root):
                pass

    def test_log_rotates_once(self):
        with tempfile.TemporaryDirectory() as root:
            log = svc.LogFile(Path(root) / "serve.log")
            with mock.patch.object(svc, "MAX_LOG_BYTES", 10):
                log("first line that is long")
                log("second")
            self.assertIn("second", (Path(root) / "serve.log").read_text())
            self.assertIn("first", (Path(root) / "serve.log.1").read_text())

    def test_windows_wake_command_keeps_backslashes(self):
        fake_os = types.SimpleNamespace(name="nt")
        with mock.patch.object(oc, "os", fake_os):
            words = oc._split_command(r'"C:\Program Files\t3\t3.exe" thread send C:\x -')
        self.assertEqual(words, [r"C:\Program Files\t3\t3.exe", "thread", "send", r"C:\x", "-"])

    @unittest.skipIf(os.name == "nt", "POSIX quoting")
    def test_posix_wake_command_uses_shell_quoting(self):
        self.assertEqual(oc._split_command("ssh host 't3 send x -'"), ["ssh", "host", "t3 send x -"])


if __name__ == "__main__":
    unittest.main()
