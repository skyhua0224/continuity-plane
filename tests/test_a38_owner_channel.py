"""Owner channel: identity gate, views, buttons, replies and token handling."""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from continuity_plane import owner_channel as oc
from continuity_plane import owner_questions as oq
from continuity_plane.lifecycle import handle_event
from continuity_plane.collaboration_inbox import list_messages, send_message
from continuity_plane.collaboration_registry import (
    add_task,
    list_changes,
    list_tasks,
    register_project,
    update_task,
)

OWNER = 1001
STRANGER = 2002


class FakeClient:
    def __init__(self, updates=()):
        self.updates = list(updates)
        self.sent = []
        self.edits = []
        self.buttons = []
        self.photos = []
        self.answers = []
        self.commands = None

    def get_updates(self, offset, timeout):
        batch, self.updates = self.updates, []
        return batch

    def get_me(self):
        return {"username": "demo_bot"}

    def send_message(self, chat_id, text, keyboard=None, rich=False):
        self.sent.append((chat_id, text, keyboard))
        return {"message_id": len(self.sent)}

    def edit_message(self, chat_id, message_id, text, keyboard=None, rich=False):
        self.edits.append((message_id, text, keyboard))
        return {}

    def edit_buttons(self, chat_id, message_id, keyboard):
        self.buttons.append((message_id, keyboard))
        return {}

    def send_photo(self, chat_id, path, caption=""):
        self.photos.append((path, caption))
        return {"message_id": 900 + len(self.photos)}

    def answer_callback(self, callback_id, text=""):
        self.answers.append(text)

    def set_commands(self, commands):
        self.commands = commands


def message(text, *, sender=OWNER, chat=OWNER, kind="private", update_id=1, reply=None):
    body = {
        "message_id": 10,
        "from": {"id": sender},
        "chat": {"id": chat, "type": kind},
        "text": text,
    }
    if reply:
        body["reply_to_message"] = {"text": reply}
    return {"update_id": update_id, "message": body}


class OwnerChannelTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        register_project(self.root, project_id="demo", control_root=self.root)
        add_task(
            self.root,
            project_id="demo",
            task_id="card-one",
            lane_id="main",
            mode="patch",
            priority="p1",
            title="Blocked <card>",
            objective="Exercise the channel views in /srv/someone/private/repo",
            next_action="Wait",
            exit_criteria=["Views render"],
            assignee="claude-opus",
        )
        update_task(
            self.root,
            project_id="demo",
            task_id="card-one",
            status="blocked",
            blocked_reason="Waiting on a fixture",
        )
        self.client = FakeClient()
        self.channel = oc.OwnerChannel(
            self.client,
            {"owner_id": OWNER, "projects": ["demo"], "recipient": "claude-opus"},
            self.root,
        )
        self.channel.bot_username = "demo_bot"

    def inbox(self):
        return list_messages(
            self.root, project_id="demo", recipient="claude-opus", limit=10
        )

    def test_other_users_and_chats_are_ignored(self):
        for update in (
            message("hi", sender=STRANGER, chat=STRANGER),
            message("/status", sender=STRANGER, chat=STRANGER),
            message("hi", chat=-5, kind="group"),
            message("hi", sender=OWNER, chat=STRANGER),
        ):
            self.channel.handle_update(update)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.inbox(), [])

    def test_stranger_cannot_press_a_button(self):
        self.channel.handle_update(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "c",
                    "from": {"id": STRANGER},
                    "data": "a|demo|0",
                    "message": {"message_id": 9, "chat": {"id": STRANGER, "type": "private"}},
                },
            }
        )
        self.assertEqual(self.client.edits, [])
        self.assertEqual(self.inbox(), [])

    def test_status_and_list_views(self):
        self.channel.handle_update(message("/status"))
        text = self.client.sent[-1][1]
        self.assertIn("阻塞 1", text)
        self.channel.handle_update(message("/list blocked", update_id=2))
        listing = self.client.sent[-1][1]
        self.assertIn("⛔ 阻塞", listing)
        self.assertIn("Blocked &lt;card&gt;", listing)
        self.assertIn("https://t.me/demo_bot?start=c_", listing)
        self.assertNotIn("card-one", listing)

    def test_link_opens_card_with_recent_activity_and_no_paths(self):
        key = oc._card_key("demo", "card-one")
        self.channel.handle_update(message(f"/start c_{key}"))
        card = self.client.sent[-1][1]
        self.assertIn("Waiting on a fixture", card)
        self.assertIn("状态变为阻塞", card)
        self.assertIn("‹路径›", card)
        self.assertNotIn("/srv/someone", card)

    def test_old_review_cards_stay_out_of_the_default_list(self):
        update_task(self.root, project_id="demo", task_id="card-one", status="ready-for-review")
        task = list_tasks(self.root, project_id="demo")[0]
        task["updated_at"] = "2020-01-01T00:00:00+00:00"
        with mock.patch.object(oc, "list_tasks", return_value=[task]):
            text, keyboard = self.channel.render_list()
            self.assertIn("0 项", text)
            self.assertIn("旧卡（1）", json.dumps(keyboard, ensure_ascii=False))
            self.assertIn("Blocked", self.channel.render_list("old")[0])

    def test_owner_action_is_a_card_and_done_closes_it(self):
        task = self.channel.request_action(
            "Lock the vault", detail="The session key was exposed", steps=["bw lock", "Confirm it"]
        )
        self.assertEqual(task["assignee"], "owner")
        sent = self.client.sent[-1]
        self.assertIn("需要你处理", sent[1])
        self.assertIn("bw lock", sent[1])
        self.assertIn("Lock the vault", self.channel.render_waiting()[0])
        data = sent[2][0][0]["callback_data"]
        press = {
            "update_id": 21,
            "callback_query": {
                "id": "c",
                "from": {"id": OWNER},
                "data": data,
                "message": {"message_id": 5, "chat": {"id": OWNER, "type": "private"}},
            },
        }
        self.channel.handle_update(press)
        self.assertIn("已完成", self.client.buttons[-1][1][0][0]["text"])
        self.assertIn("没有等你处理", self.channel.render_waiting()[0])
        self.channel.handle_update(press)
        self.assertEqual(self.client.answers[-1], "已处理过")

    def test_later_resends_the_card_when_due(self):
        self.channel.request_action("Check it", detail="Please check")
        data = self.client.sent[-1][2][0][1]["callback_data"]
        self.channel.handle_update(
            {
                "update_id": 22,
                "callback_query": {
                    "id": "c",
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {"message_id": 5, "chat": {"id": OWNER, "type": "private"}},
                },
            }
        )
        self.assertEqual(self.channel.resend_due(), 0)
        key = data.split("|")[2]
        self.channel.reminders[key] = "2020-01-01T00:00:00+00:00"
        before = len(self.client.sent)
        self.assertEqual(self.channel.resend_due(), 1)
        self.assertEqual(len(self.client.sent), before + 1)

    def test_stage_report_carries_summary_advice_and_live_cards(self):
        self.channel.request_action("Check it", detail="Please check")
        self.channel.send_stage_report(
            "Channel trial", summary="It works", recommendations=["Do A", "Do B"]
        )
        text, keyboard = self.client.sent[-1][1], self.client.sent[-1][2]
        for expected in ("阶段完成", "It works", "1. Do A", "等你处理（1）", "阻塞（1）"):
            self.assertIn(expected, text)
        self.assertEqual(keyboard[0][1]["text"], "做 2")

    def test_view_button_edits_in_place(self):
        self.channel.handle_update(
            {
                "update_id": 4,
                "callback_query": {
                    "id": "c",
                    "from": {"id": OWNER},
                    "data": "v|list|blocked|0",
                    "message": {"message_id": 9, "chat": {"id": OWNER, "type": "private"}},
                },
            }
        )
        self.assertEqual(self.client.sent, [])
        self.assertIn("Blocked", self.client.edits[-1][1])

    def test_reply_is_stored_once_with_verbatim_text(self):
        update = message("这个阴影太夸张", update_id=7, reply="[demo] 看一下")
        self.channel.handle_update(update)
        self.channel.handle_update(update)
        items = self.inbox()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["body"].startswith("这个阴影太夸张"))
        self.assertIn("追问：[demo] 看一下", items[0]["body"])
        self.assertEqual(items[0]["sender"], oc.CHANNEL_SENDER)

    def test_option_button_records_answer_and_removes_buttons(self):
        self.channel.notify("能关吗？", options=["可以", "不能"])
        keyboard = self.client.sent[-1][2]
        callback = {
            "update_id": 8,
            "callback_query": {
                "id": "c",
                "from": {"id": OWNER},
                "data": keyboard[1][0]["callback_data"],
                "message": {
                    "message_id": 1,
                    "text": "[demo] 能关吗？",
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            },
        }
        self.channel.handle_update(callback)
        self.assertIn("按钮回答：不能", self.inbox()[0]["body"])
        self.assertEqual(self.client.buttons[-1][1][0][0]["text"], "✅ 已回答：不能")

    def test_stale_button_is_not_recorded(self):
        self.channel.handle_update(
            {
                "update_id": 9,
                "callback_query": {
                    "id": "c",
                    "from": {"id": OWNER},
                    "data": "a|demo|0",
                    "message": {
                        "message_id": 1,
                        "text": "[demo] 已回答",
                        "chat": {"id": OWNER, "type": "private"},
                    },
                },
            }
        )
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.client.answers, ["已处理过"])

    def test_pending_owner_messages_are_delivered_once(self):
        send_message(
            self.root, project_id="demo", sender="claude-opus", recipient="owner", body="做完了"
        )
        self.assertEqual(self.channel.deliver_pending(), 1)
        self.assertEqual(self.channel.deliver_pending(), 0)
        self.assertIn("做完了", self.client.sent[-1][1])

    def test_secret_shaped_text_is_not_sent(self):
        with self.assertRaises(oc.OwnerChannelError):
            self.channel.notify("password=hunter2hunter2")
        self.assertEqual(self.client.sent, [])

    def test_serve_persists_offset_and_poll_time(self):
        self.client.updates = [message("/help", update_id=41)]
        ticks = iter([False, True])
        oc.serve(self.channel, should_stop=lambda: next(ticks))
        state = json.loads(oc.state_path(self.root).read_text())
        self.assertEqual(state["offset"], 42)
        self.assertIsNotNone(state["last_poll_at"])
        self.assertEqual(self.client.commands, oc.COMMANDS)


    def test_serve_survives_a_network_failure_at_startup(self):
        real = self.client.set_commands
        calls = []

        def flaky(commands):
            calls.append(commands)
            if len(calls) == 1:
                raise oc.TelegramError("setMyCommands failed: URLError")
            real(commands)

        self.client.set_commands = flaky
        self.channel.bot_username = None
        ticks = iter([False, False, True])
        oc.serve(self.channel, should_stop=lambda: next(ticks), sleep=lambda seconds: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.channel.bot_username, "demo_bot")


    def press(self, data, message_id, update_id=50):
        self.channel.handle_update(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": "c",
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "text": "card",
                        "chat": {"id": OWNER, "type": "private"},
                    },
                },
            }
        )

    def card(self, sender="controller-a", **fields):
        send_message(
            self.root,
            project_id="demo",
            sender=sender,
            recipient="owner",
            body=json.dumps({"owner_card": 1, "title": "T", **fields}, ensure_ascii=False),
        )
        self.channel.deliver_pending()
        return len(self.client.sent)

    def inbox_of(self, recipient):
        return list_messages(self.root, project_id="demo", recipient=recipient, limit=10)

    def test_custom_buttons_answer_the_sending_controller(self):
        message_id = self.card(
            text="选一个",
            buttons=[{"label": "方案 A", "value": "plan-a"}, {"label": "文档", "url": "https://example.com"}],
        )
        keyboard = self.client.sent[-1][2]
        self.assertEqual(keyboard[1][0]["url"], "https://example.com")
        self.press("b|0", message_id)
        self.assertIn("plan-a", self.inbox_of("controller-a")[0]["body"])
        self.press("b|0", message_id, update_id=51)
        self.assertEqual(len(self.inbox_of("controller-a")), 1)
        self.assertEqual(self.client.answers[-1], "已处理过")

    def test_insecure_button_link_does_not_block_the_inbox(self):
        self.card(buttons=[{"label": "x", "url": "http://example.com"}])
        self.assertIn("无法显示", self.client.sent[-1][1])
        self.assertEqual(self.channel.deliver_pending(), 0)

    def test_reply_goes_back_to_the_sender_of_that_message(self):
        message_id = self.card(text="做完了")
        update = message("为什么这样做", update_id=60)
        update["message"]["reply_to_message"] = {"message_id": message_id, "text": "做完了"}
        self.channel.handle_update(update)
        self.assertIn("为什么这样做", self.inbox_of("controller-a")[0]["body"])
        self.assertEqual(self.inbox(), [])

    def test_low_risk_approval_is_one_press_and_recorded(self):
        message_id = self.card(kind="approval", risk="low", text="合并到特性分支")
        self.press("p|yes", message_id)
        result = self.inbox_of("controller-a")[0]["body"]
        self.assertIn("approved", result)
        changes = list_changes(self.root, project_id="demo", since=0, max_results=50)["changes"]
        self.assertEqual(
            [c["decision"] for c in changes if c["event"] == "owner.approval"], ["approved"]
        )
        self.press("p|yes", message_id, update_id=51)
        self.assertEqual(len(self.inbox_of("controller-a")), 1)

    def test_medium_risk_approval_needs_confirmation(self):
        message_id = self.card(kind="approval", risk="medium", text="重试")
        self.press("p|yes", message_id)
        self.assertEqual(self.inbox_of("controller-a"), [])
        self.assertEqual(self.client.buttons[-1][1][0][0]["callback_data"], "p|confirm")
        self.press("p|confirm", message_id, update_id=51)
        self.assertIn("approved", self.inbox_of("controller-a")[0]["body"])

    def test_high_risk_approval_cannot_be_approved_here(self):
        message_id = self.card(kind="approval", risk="high", text="部署")
        self.assertIsNone(self.client.sent[-1][2])
        self.press("p|confirm", message_id)
        self.assertEqual(self.inbox_of("controller-a"), [])

    def test_expired_approval_is_refused(self):
        message_id = self.card(kind="approval", risk="low", text="x", expires_hours=-1)
        self.press("p|yes", message_id)
        self.assertIn("expired", self.inbox_of("controller-a")[0]["body"])

    def test_timers_report_and_collapse_missed_runs(self):
        self.channel.handle_update(message("/every 2h"))
        self.assertIn("每 2 小时", self.client.sent[-1][1])
        timer = self.channel.timers[0]
        self.assertEqual(self.channel.run_timers(), 0)
        timer["next_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(self.channel.run_timers(), 1)
        self.assertIn("进度报告", self.client.sent[-1][1])
        self.assertEqual(self.channel.run_timers(), 0)

    def test_once_timer_runs_once_and_bad_schedules_are_refused(self):
        self.channel.handle_update(message("/in 30m"))
        self.channel.timers[0]["next_at"] = "2020-01-01T00:00:00+00:00"
        self.channel.run_timers()
        self.assertEqual(self.channel.timers, [])
        self.channel.handle_update(message("/every 1m", update_id=2))
        self.assertIn("时间格式", self.client.sent[-1][1])

    def test_timer_buttons_add_and_cancel(self):
        self.press("t|add|09:00", 3)
        self.assertEqual(self.channel.timers[0]["value"], "09:00")
        self.press(f"t|del|{self.channel.timers[0]['id']}", 3)
        self.assertEqual(self.channel.timers, [])

    def test_bare_text_asks_which_project_when_several(self):
        register_project(self.root, project_id="other", control_root=self.root)
        self.channel.projects.append("other")
        self.channel.routes = {"other": "controller-b"}
        self.channel.handle_update(message("看看这个", update_id=70))
        self.assertIn("发给哪个项目", self.client.sent[-1][1])
        self.channel.handle_update(
            {
                "update_id": 71,
                "callback_query": {
                    "id": "c",
                    "from": {"id": OWNER},
                    "data": "s|other",
                    "message": {
                        "message_id": 4,
                        "text": "发给哪个项目？\n\n看看这个",
                        "chat": {"id": OWNER, "type": "private"},
                    },
                },
            }
        )
        items = list_messages(self.root, project_id="other", recipient="controller-b", limit=5)
        self.assertEqual(items[0]["body"], "看看这个")

    def test_waiting_lists_owner_decisions_from_the_handoff(self):
        with mock.patch.object(
            oc,
            "_declared_decisions",
            return_value=["Owner: hero band R1 vs R2", "Fix the docs"],
        ):
            text = self.channel.render_waiting()[0]
        self.assertIn("hero band R1 vs R2", text)
        self.assertNotIn("Fix the docs", text)

    def test_card_keeps_times_when_the_journal_window_has_no_events(self):
        with mock.patch.object(
            oc, "list_changes", return_value={"changes": [{"event": "attempt.heartbeat"}]}
        ):
            text = self.channel.render_card(oc._card_key("demo", "card-one"))[0]
        self.assertIn("创建", text)


    def start_watching(self):
        self.channel.watch_events()
        self.client.sent.clear()

    def test_watch_starts_from_now_and_pushes_a_failure_once(self):
        self.start_watching()
        self.assertEqual(self.client.sent, [])
        record = oc._record_change
        record(self.root, "demo", "job.lost", "card-one", reason="process-lost")
        record(self.root, "demo", "job.interrupted", "card-one", reason="timeout")
        self.channel.watch_events()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("被中断", self.client.sent[0][1])
        record(self.root, "demo", "job.lost", "card-one", reason="process-lost")
        self.channel.watch_events()
        self.assertEqual(len(self.client.sent), 1)

    def test_heartbeats_are_not_news(self):
        self.start_watching()
        oc._record_change(self.root, "demo", "attempt.heartbeat", "card-one")
        self.assertEqual(self.channel.watch_events(), 0)
        self.assertEqual(self.client.sent, [])

    def test_finished_cards_fold_into_one_digest_outside_quiet_hours(self):
        self.start_watching()
        update_task(
            self.root,
            project_id="demo",
            task_id="card-one",
            status="ready-for-review",
            verification="All checks passed",
        )
        now = datetime.now(oc.UTC)
        with mock.patch.object(oc, "_is_quiet", return_value=False):
            self.channel.watch_events(now)
            self.assertEqual(self.client.sent, [])
            self.channel.watch_events(now + timedelta(hours=2))
        self.assertIn("任务完成总结", self.client.sent[0][1])
        self.assertIn("All checks passed", self.client.sent[0][1])
        self.assertEqual(self.channel.finished["demo"], [])

    def test_quiet_hours_hold_the_digest(self):
        self.start_watching()
        update_task(self.root, project_id="demo", task_id="card-one", status="ready-for-review")
        with mock.patch.object(oc, "_is_quiet", return_value=True):
            self.channel.watch_events()
            self.channel.digest_at["demo"] = "2020-01-01T00:00:00+00:00"
            self.channel.watch_events()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.channel.finished["demo"], ["card-one"])

    def test_stage_report_shows_measured_data(self):
        self.channel.send_stage_report(
            "T",
            summary="line one\nline two",
            metrics=[oc._parse_metric("恢复注入=3974 B->1150 B"), oc._parse_metric("测试=31")],
        )
        text = self.client.sent[-1][1]
        self.assertIn("3974 B → 1150 B（-71.1%）", text)
        self.assertIn("测试：31", text)
        self.assertIn("line one\nline two", text)

    def test_only_images_under_an_evidence_root_are_sent(self):
        allowed = self.root / "evidence"
        allowed.mkdir()
        good = allowed / "shot.png"
        good.write_bytes(b"png")
        text = allowed / "notes.txt"
        text.write_text("private")
        outside = self.root / "outside.png"
        outside.write_bytes(b"png")
        link = allowed / "link.png"
        link.symlink_to(outside)
        self.channel.evidence_roots = [allowed.resolve()]
        sent = self.channel.send_evidence("demo", [str(good), str(text), str(outside), str(link)], "T")
        self.assertEqual(sent, 1)
        self.assertEqual(self.client.photos[0][0], str(good.resolve()))
        self.assertIn("3 张截图没有发送", self.client.sent[-1][1])

    def test_screenshots_are_off_without_a_root(self):
        path = self.root / "shot.png"
        path.write_bytes(b"png")
        self.assertEqual(self.channel.send_evidence("demo", [str(path)], "T"), 0)

    def test_default_timers_are_seeded_once(self):
        ticks = iter([False, True])
        oc.serve(self.channel, default_timers=["09:00"], should_stop=lambda: next(ticks))
        self.assertEqual(len(self.channel.timers), 1)
        state = json.loads(oc.state_path(self.root).read_text())
        state["timers"] = []
        oc.state_path(self.root).write_text(json.dumps(state))
        ticks = iter([False, True])
        oc.serve(self.channel, default_timers=["09:00"], should_stop=lambda: next(ticks))
        self.assertEqual(self.channel.timers, [])

    def test_a_registry_error_does_not_stop_the_service(self):
        self.client.get_updates = mock.Mock(
            side_effect=[oc.CollaborationRegistryError("busy"), []]
        )
        ticks = iter([False, False, True])
        oc.serve(self.channel, should_stop=lambda: next(ticks), sleep=lambda seconds: None)
        self.assertEqual(self.client.get_updates.call_count, 2)


    def ask(self, **fields):
        item = oq.ask(
            self.root,
            project_id="demo",
            asker="controller-a",
            question=fields.pop("question", "能关虚拟机吗？"),
            **fields,
        )
        self.channel.deliver_questions()
        return item, len(self.client.sent)

    def test_question_button_answers_once_and_reaches_the_asker(self):
        item, message_id = self.ask(options=["可以", "不能"])
        self.assertIn("需要你回答", self.client.sent[-1][1])
        self.assertEqual(self.channel.deliver_questions(), 0)
        self.press("q|1", message_id)
        answer = self.inbox_of("controller-a")[0]
        self.assertIn("不能", answer["body"])
        self.assertEqual(answer["sender"], "owner")
        self.press("q|0", message_id, update_id=51)
        self.assertEqual(len(self.inbox_of("controller-a")), 1)
        closed = oq.list_questions(self.root, project_id="demo", status="answered")
        self.assertEqual(closed[0]["answer"], "不能")

    def test_reply_is_a_free_text_answer(self):
        _, message_id = self.ask()
        update = message("等我编译完再关", update_id=61)
        update["message"]["reply_to_message"] = {"message_id": message_id, "text": "q"}
        self.channel.handle_update(update)
        self.assertIn("等我编译完再关", self.inbox_of("controller-a")[0]["body"])
        self.assertEqual(oq.list_questions(self.root, project_id="demo"), [])

    def test_first_answer_wins_across_surfaces(self):
        item, message_id = self.ask(options=["可以"])
        first = oq.answer(
            self.root, project_id="demo", question_id=item["question_id"], text="电脑上答的", via="cli"
        )
        self.assertTrue(first["accepted"])
        self.press("q|0", message_id)
        self.assertEqual(self.client.answers[-1], "已处理过")
        self.assertEqual(len(self.inbox_of("controller-a")), 1)

    def test_expired_question_tells_the_asker_and_refuses_answers(self):
        item, message_id = self.ask(expires_hours=0.0000001)
        time.sleep(0.01)  # the 0.36 ms lifetime must have passed
        self.assertEqual(oq.list_questions(self.root, project_id="demo"), [])
        self.assertIn("已过期", self.inbox_of("controller-a")[0]["body"])
        self.press("q|0", message_id)
        result = oq.answer(
            self.root, project_id="demo", question_id=item["question_id"], text="x", via="cli"
        )
        self.assertFalse(result["accepted"])

    def test_repeated_question_is_not_filed_twice(self):
        first, _ = self.ask()
        second, sent = self.ask()
        self.assertEqual(first["question_id"], second["question_id"])
        self.assertEqual(sent, 1)

    def test_open_question_shows_in_waiting_and_report_and_can_be_resent(self):
        item, _ = self.ask()
        text, keyboard = self.channel.render_waiting()
        self.assertIn("能关虚拟机吗", text)
        self.assertIn("能关虚拟机吗", self.channel.render_report()[0])
        self.press(keyboard[0][0]["callback_data"], 2)
        self.assertEqual(self.channel.deliver_questions(), 1)

    def test_question_card_from_an_older_cli(self):
        self.card(kind="question", title="选哪个", options=["A"])
        self.channel.deliver_questions()
        self.assertEqual(oq.list_questions(self.root, project_id="demo")[0]["asker"], "controller-a")
        self.assertIn("需要你回答", self.client.sent[-1][1])

    def test_resume_request_is_recorded_on_answer(self):
        item, _ = self.ask(resume_on_answer=True)
        oq.answer(self.root, project_id="demo", question_id=item["question_id"], text="好", via="cli")
        changes = list_changes(self.root, project_id="demo", since=0, max_results=50)["changes"]
        answered = [c for c in changes if c["event"] == "owner.question.answered"]
        self.assertTrue(answered[0]["resume_requested"])

    def test_secret_shaped_question_is_refused(self):
        with self.assertRaises(oc.CollaborationRegistryError):
            oq.ask(self.root, project_id="demo", asker="a", question="password=hunter2hunter2")

    def test_session_stop_is_recorded_and_pushed_once(self):
        self.start_watching()
        payload = {
            "hook_event_name": "StopFailure",
            "session_id": "s1",
            "cwd": str(self.root),
            "error": "rate_limit",
            "error_details": "429 Too Many Requests",
        }
        for _ in range(2):
            handle_event(payload, host="claude-remote", data_root=self.root)
        self.channel.watch_events()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("撞到限额", self.client.sent[0][1])
        handle_event(payload, host="claude-remote", data_root=self.root)
        self.channel.watch_events()
        self.assertEqual(len(self.client.sent), 1)

    def test_only_quota_notifications_are_recorded(self):
        self.start_watching()
        for kind in ("idle_prompt", "quota_auto_resume_disabled"):
            handle_event(
                {
                    "hook_event_name": "Notification",
                    "session_id": "s2",
                    "cwd": str(self.root),
                    "notification_type": kind,
                },
                host="claude",
                data_root=self.root,
            )
        self.channel.watch_events()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("已放弃等待", self.client.sent[0][1])


    def test_partial_approval_reports_each_item(self):
        message_id = self.card(kind="approval", risk="low", text="x", items=["提交", "推送", "发版"])
        keyboard = self.client.sent[-1][2]
        self.assertEqual(keyboard[1][0]["text"], "☑ 推送")
        self.press("p|t|1", message_id)
        self.assertEqual(self.client.buttons[-1][1][1][0]["text"], "☐ 推送")
        self.press("p|t|2", message_id, update_id=51)
        self.press("p|yes", message_id, update_id=52)
        body = self.inbox_of("controller-a")[0]["body"]
        self.assertIn("partial", body)
        self.assertIn("批准：提交", body)
        self.assertIn("未批准：推送；发版", body)
        changes = list_changes(self.root, project_id="demo", since=0, max_results=50)["changes"]
        record = [c for c in changes if c["event"] == "owner.approval"][0]
        self.assertEqual(record["approved_items"], [0])

    def test_approval_with_nothing_selected_is_not_approved(self):
        message_id = self.card(kind="approval", risk="low", text="x", items=["提交"])
        self.press("p|t|0", message_id)
        self.press("p|yes", message_id, update_id=51)
        self.assertEqual(self.inbox_of("controller-a"), [])
        self.assertIn("至少选一项", self.client.answers[-1])

    def test_all_items_selected_is_a_full_approval(self):
        message_id = self.card(kind="approval", risk="low", text="x", items=["提交", "推送"])
        self.press("p|yes", message_id)
        self.assertIn("审批结果：approved", self.inbox_of("controller-a")[0]["body"])

    def test_reject_approves_no_items(self):
        message_id = self.card(kind="approval", risk="low", text="x", items=["提交"])
        self.press("p|no", message_id)
        self.assertIn("批准：无", self.inbox_of("controller-a")[0]["body"])

    def test_wake_runs_the_command_without_a_shell_and_sends_stdin(self):
        target = self.root / "woken"
        self.channel.wake_commands = {
            "demo": [sys.executable, "-c", f"import sys;open({str(target)!r},'wb').write(sys.stdin.buffer.read())"]
        }
        self.assertTrue(self.channel.wake("demo", "$(touch /tmp/never) 你好"))
        self.assertEqual(target.read_text(encoding="utf-8"), "$(touch /tmp/never) 你好")

    def test_failed_wake_is_reported_and_unconfigured_projects_have_no_button(self):
        self.channel.handle_update(message("hi", update_id=80))
        self.assertIsNone(self.client.sent[-1][2])
        self.channel.wake_commands = {"demo": [sys.executable, "-c", "raise SystemExit(3)"]}
        self.channel.handle_update(message("hello", update_id=81))
        self.assertEqual(self.client.sent[-1][2][0][0]["callback_data"], "k|go")
        self.press("k|go", len(self.client.sent))
        self.assertIn("叫醒失败", self.client.buttons[-1][1][0][0]["text"])
        self.press("k|go", len(self.client.sent), update_id=51)
        self.assertEqual(self.client.answers[-1], "已处理过")

    def test_answer_wakes_the_asker_only_when_requested(self):
        self.start_watching()
        calls = []
        self.channel.wake = lambda project, text: calls.append(text) or True
        plain, _ = self.ask(question="普通问题")
        waiting, _ = self.ask(question="卡住的问题", resume_on_answer=True)
        for item in (plain, waiting):
            oq.answer(self.root, project_id="demo", question_id=item["question_id"], text="好", via="cli")
        self.channel.watch_events()
        self.assertEqual(len(calls), 1)
        self.assertIn(waiting["question_id"], calls[0])


class TokenTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX mode bits; see test_a38_owner_sinks")
    def test_group_readable_token_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "token"
            path.write_text("123:abc\n")
            path.chmod(0o640)
            with self.assertRaises(oc.OwnerChannelError):
                oc.read_token(str(path))
            path.chmod(0o600)
            self.assertEqual(oc.read_token(str(path)), "123:abc")

    def test_http_errors_do_not_expose_the_token(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"not json")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = oc.TelegramClient(
            "SECRET-TOKEN", base_url=f"http://127.0.0.1:{server.server_port}"
        )
        with self.assertRaises(oc.TelegramError) as caught:
            client.get_me()
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
