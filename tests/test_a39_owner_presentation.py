"""Alpha39 owner-channel presentation: Bot API 9.4-10.4 features with fallbacks."""

from __future__ import annotations

import json
import re
import unittest
from datetime import UTC, datetime, timedelta

from continuity_plane import owner_channel as oc
from continuity_plane import owner_questions as oq
from continuity_plane import owner_sinks
from continuity_plane import owner_widgets
from continuity_plane.collaboration_inbox import acknowledge, list_messages
from continuity_plane.collaboration_registry import register_project

try:
    from tests import test_a38_owner_channel as base
except ImportError:  # run from the tests directory
    import test_a38_owner_channel as base

OWNER, FakeClient, message = base.OWNER, base.FakeClient, base.message


class ModernClient(FakeClient):
    """A client with the optional Bot API 10.x presentation calls."""

    def __init__(self, updates=()):
        super().__init__(updates)
        self.thread_id = None
        self.threads = []
        self.topics_made = []
        self.ephemeral = []
        self.deleted_ephemeral = []
        self.reactions = []
        self.pins = []
        self.unpins = []
        self.fail_ephemeral = False

    def send_ephemeral(self, chat_id, callback_id, text, keyboard=None, *, replace=True):
        if self.fail_ephemeral:
            raise oc.TelegramError("Bad Request: ephemeral messages are unavailable")
        self.ephemeral.append((callback_id, text, keyboard, replace))
        return {"message_id": 0, "ephemeral_message_id": 77}

    def delete_ephemeral(self, chat_id, receiver_id, ephemeral_id):
        self.deleted_ephemeral.append(ephemeral_id)
        return True

    def set_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((message_id, emoji))
        return True

    def pin(self, chat_id, message_id):
        self.pins.append(message_id)
        return True

    def unpin(self, chat_id, message_id):
        self.unpins.append(message_id)
        return True

    def send_message(self, chat_id, text, keyboard=None, rich=False):
        self.threads.append(self.thread_id)
        return super().send_message(chat_id, text, keyboard, rich)

    def create_topic(self, chat_id, name):
        self.topics_made.append(name)
        return {"message_thread_id": 500 + len(self.topics_made), "name": name}

    rich_error = None

    def send_rich(self, chat_id, html_text, keyboard=None, *, fallback=""):
        if self.rich_error:
            raise oc.TelegramError(self.rich_error)
        self.threads.append(self.thread_id)
        self.rich = getattr(self, "rich", []) + [(html_text, keyboard, fallback)]
        self.sent.append((chat_id, html_text, keyboard))
        return {"message_id": len(self.sent)}

    widget_error = None

    def send_widget(self, chat_id, html_text, chat_data, caption, *, height=None):
        if self.widget_error:
            raise oc.TelegramError(self.widget_error)
        self.widgets = getattr(self, "widgets", []) + [(html_text, chat_data, caption)]
        self.sent.append((chat_id, caption, None))
        return {"message_id": len(self.sent)}

    def edit_widget_data(self, chat_id, message_id, chat_data):
        self.widget_edits = getattr(self, "widget_edits", []) + [(message_id, chat_data)]
        return {}

    def answer_widget_query(self, query_id, data=None):
        self.widget_answers = getattr(self, "widget_answers", []) + [(query_id, data)]
        return True


class HelperTest(unittest.TestCase):
    def test_clip_html_never_leaves_a_tag_open(self):
        text = "<b>标题</b>\n<blockquote expandable>" + "很长的一行 &amp; 内容\n" * 400 + "</blockquote>"
        clipped = oc._clip_html(text)
        self.assertLessEqual(len(clipped), oc.MAX_TEXT_CHARS)
        self.assertTrue(clipped.endswith("</blockquote>"))
        self.assertNotRegex(clipped, r"&[#\w]*…")
        self.assertEqual(clipped.count("<blockquote"), clipped.count("</blockquote>"))

    def test_safe_lines_never_cuts_an_entity(self):
        text = "\n".join(["a & b < c"] * 50)
        value = oc._safe_lines(text, 100)
        self.assertLessEqual(len(value), 100)
        self.assertFalse(re.search(r"&[a-z]*$", value))

    def test_time_tags_render_locally_and_keep_a_fallback(self):
        moment = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
        tag = oc._when(moment)
        self.assertIn(f'unix="{int(moment.timestamp())}"', tag)
        self.assertIn('format="dt"', tag)
        plain = owner_sinks.to_plain_text(f"有效至 {tag}")
        self.assertEqual(plain, "有效至 " + moment.astimezone().strftime("%m-%d %H:%M"))
        relative = oc._ago_html((datetime.now(UTC) - timedelta(minutes=10)).isoformat())
        self.assertIn('format="r"', relative)
        self.assertIn("10 分钟前", relative)

    def test_fold_collapses_only_long_text(self):
        self.assertEqual(oc._fold(["一", "二"]), ["一", "二"])
        folded = oc._fold([f"第 {i} 行" for i in range(8)])
        self.assertEqual(folded[:3], ["第 0 行", "第 1 行", "第 2 行"])
        self.assertTrue(folded[3].startswith("<blockquote expandable>"))

    def test_disabled_buttons_carry_no_callback(self):
        self.assertEqual(oc._disabled("✅ 已批准"), {"text": "✅ 已批准", "disabled": {}})
        self.assertEqual(oc._button("批准", "p|yes", "success")["style"], "success")
        self.assertNotIn("style", oc._button("看看", "v|status"))

    def test_widget_html_fits_the_platform_limits(self):
        raw, compressed = owner_widgets.html_size()
        self.assertLessEqual(raw, owner_widgets.MAX_HTML_BYTES)
        self.assertLessEqual(compressed, owner_widgets.MAX_COMPRESSED_BYTES)
        self.assertNotIn("\x00", owner_widgets.PANEL_HTML)
        self.assertNotRegex(owner_widgets.PANEL_HTML, r"(src|href)=")
        self.assertNotIn("innerHTML", owner_widgets.PANEL_HTML, "task titles must stay text")

    def test_state_is_trimmed_to_chat_data(self):
        state = {"t": 1, "u": "b", "w": 0, "p": [["项目" * 5, 1, 2, 3, 4, [["a", "很长的标题" * 4, "k" * 12]] * 40]]}
        data = owner_widgets.fit(state)
        self.assertLessEqual(len(data.encode("utf-8")), owner_widgets.MAX_DATA_BYTES)
        self.assertEqual(json.loads(data)["p"][0][1:5], [1, 2, 3, 4])
        self.assertEqual(owner_widgets.digest_view('{"t":1,"w":2}'), owner_widgets.digest_view('{"t":9,"w":2}'))

    def test_client_sends_into_the_current_topic(self):
        client = oc.TelegramClient("123:abc")
        bodies = []
        client._exchange = lambda method, body, headers, timeout: bodies.append((method, body)) or {}
        client.thread_id = 42
        client.send_message(1, "hi")
        client.send_ephemeral(1, "cb", "only you")
        client.answer_callback("cb")
        self.assertIn(b"message_thread_id=42", bodies[0][1])
        self.assertNotIn(b"message_thread_id", bodies[1][1], "ephemeral replies follow the callback")
        self.assertNotIn(b"message_thread_id", bodies[2][1])

    def test_widget_updates_are_requested(self):
        client = oc.TelegramClient("123:abc")
        calls = []
        client._call = lambda method, **fields: calls.append((method, fields)) or []
        client.get_updates(None, 1)
        self.assertIn("widget_query", calls[0][1]["allowed_updates"])

    def test_webhooks_get_the_classic_text_of_a_rich_message(self):
        sent = []
        primary = type("P", (), {"send_rich": lambda self, *a, **k: {"message_id": 1}})()
        sinks = owner_sinks.Sinks([], factory=lambda url: None)
        sinks.targets = [("hook", lambda text, image=None: sent.append(text) or True)]
        client = owner_sinks.MirroredClient(primary, sinks, interactive=True)
        client.send_rich(1, "<h2>标题</h2>", None, fallback="<b>阶段完成</b> 摘要")
        self.assertEqual(sent, ["阶段完成 摘要"])

    def test_webhook_only_client_accepts_the_optional_calls(self):
        client = owner_sinks.NullClient(sleep=lambda seconds: None)
        self.assertIsNone(client.set_reaction(1, 2, "👀"))
        self.assertIsNone(client.pin(1, 2))


class PresentationTest(base.OwnerChannelTest):
    def setUp(self):
        super().setUp()
        self.client = ModernClient()
        self.channel.client = self.client

    def press_ephemeral(self, data, update_id=60):
        """A press on an ephemeral message: message_id 0, no chat (Bot API 10.4)."""
        self.channel.handle_update(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": "e",
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {"message_id": 0, "ephemeral_message_id": 77, "text": "confirm"},
                },
            }
        )

    def test_approval_buttons_are_coloured(self):
        self.card(kind="approval", risk="low", text="合并")
        last_row = self.client.sent[-1][2][-1]
        self.assertEqual([b.get("style") for b in last_row], ["success", "danger"])

    def test_decided_approval_keeps_a_greyed_record_of_items(self):
        message_id = self.card(kind="approval", risk="low", text="x", items=["提交", "推送"])
        self.press("p|t|1", message_id)
        self.press("p|yes", message_id, update_id=51)
        rows = self.client.buttons[-1][1]
        self.assertEqual(rows[0][0], {"text": "✅ 部分批准", "disabled": {}})
        self.assertEqual([row[0]["text"] for row in rows[1:]], ["☑ 提交", "☐ 推送"])
        self.assertTrue(all("callback_data" not in row[0] for row in rows))

    def test_medium_risk_confirms_in_an_ephemeral_message(self):
        message_id = self.card(kind="approval", risk="medium", text="重试", items=["提交"])
        buttons_before = len(self.client.buttons)
        self.press("p|yes", message_id)
        self.assertEqual(len(self.client.buttons), buttons_before, "the card itself is not edited")
        callback_id, text, keyboard, replace = self.client.ephemeral[-1]
        self.assertEqual(callback_id, "c")
        self.assertTrue(replace)
        self.assertIn("提交", text)
        self.assertEqual(keyboard[0][0]["callback_data"], f"p|confirm|{message_id}")
        self.assertEqual(self.inbox_of("controller-a"), [])
        self.press_ephemeral(f"p|confirm|{message_id}")
        self.assertIn("approved", self.inbox_of("controller-a")[0]["body"])
        self.assertEqual(self.client.deleted_ephemeral, [77])
        self.assertEqual(self.client.buttons[-1][0], message_id)
        self.assertEqual(self.client.buttons[-1][1][0][0]["text"], "✅ 已批准")

    def test_ephemeral_cancel_and_vanishing_change_nothing(self):
        message_id = self.card(kind="approval", risk="medium", text="重试")
        self.press("p|yes", message_id)
        self.press_ephemeral(f"p|cancel|{message_id}")
        self.assertEqual(self.inbox_of("controller-a"), [])
        self.assertEqual(self.client.answers[-1], "已取消")
        # The confirmation vanished unseen: approving again simply asks again.
        self.press("p|yes", message_id, update_id=61)
        self.assertEqual(len(self.client.ephemeral), 2)
        self.assertEqual(self.inbox_of("controller-a"), [])

    def test_ephemeral_press_from_someone_else_is_ignored(self):
        message_id = self.card(kind="approval", risk="medium", text="重试")
        self.press("p|yes", message_id)
        self.channel.handle_update(
            {
                "update_id": 70,
                "callback_query": {
                    "id": "e",
                    "from": {"id": OWNER + 1},
                    "data": f"p|confirm|{message_id}",
                    "message": {"message_id": 0, "ephemeral_message_id": 77},
                },
            }
        )
        self.assertEqual(self.inbox_of("controller-a"), [])

    def test_ephemeral_failure_falls_back_to_editing_the_card(self):
        self.client.fail_ephemeral = True
        message_id = self.card(kind="approval", risk="medium", text="重试")
        self.press("p|yes", message_id)
        self.assertEqual(self.client.buttons[-1][1][0][0]["callback_data"], "p|confirm")
        self.press("p|confirm", message_id, update_id=51)
        self.assertIn("approved", self.inbox_of("controller-a")[0]["body"])

    def test_reply_gets_reactions_instead_of_a_confirmation_message(self):
        message_id = self.card(text="做完了吗")
        sent_before = len(self.client.sent)
        update = message("做完了", update_id=80)
        update["message"]["message_id"] = 555
        update["message"]["reply_to_message"] = {"message_id": message_id, "text": "做完了吗"}
        self.channel.handle_update(update)
        self.assertEqual(len(self.client.sent), sent_before)
        self.assertEqual(self.client.reactions, [(555, oc.REACTION_FORWARDED)])
        self.assertEqual(self.channel.check_receipts(), 0)
        item = self.inbox_of("controller-a")[0]
        acknowledge(self.root, project_id="demo", recipient="controller-a", message_id=item["message_id"])
        self.assertEqual(self.channel.check_receipts(), 1)
        self.assertEqual(self.client.reactions[-1], (555, oc.REACTION_READ))
        self.assertEqual(self.channel.receipts, {})

    def test_old_receipts_expire_quietly(self):
        self.channel.receipts["9"] = {
            "project": "demo",
            "recipient": "controller-a",
            "inbox_id": "tg-1",
            "at": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
        }
        self.assertEqual(self.channel.check_receipts(), 0)
        self.assertEqual(self.client.reactions, [])

    def test_answered_question_is_acknowledged_with_a_reaction(self):
        oq.ask(self.root, project_id="demo", asker="controller-a", question="用哪个方案？")
        self.channel.deliver_questions()
        message_id = len(self.client.sent)
        sent_before = len(self.client.sent)
        update = message("用 B", update_id=81)
        update["message"]["message_id"] = 556
        update["message"]["reply_to_message"] = {"message_id": message_id, "text": "q"}
        self.channel.handle_update(update)
        self.assertEqual(len(self.client.sent), sent_before)
        self.assertEqual(self.client.reactions, [(556, oc.REACTION_READ)])

    def test_status_panel_pins_and_refreshes_only_on_change(self):
        self.channel.handle_update(message("/status"))
        status_id = len(self.client.sent)
        self.assertIn("v|pin", json.dumps(self.client.sent[-1][2]))
        self.press("v|pin", status_id)
        self.assertEqual(self.client.pins, [status_id])
        self.assertIn("自动刷新", self.client.edits[-1][1])
        self.assertIn("v|unpin", json.dumps(self.client.edits[-1][2]))
        start = datetime.now(UTC)
        self.assertTrue(self.channel.refresh_panel(start))
        edits = len(self.client.edits)
        self.assertFalse(self.channel.refresh_panel(start + timedelta(minutes=1)), "too soon")
        self.assertFalse(self.channel.refresh_panel(start + timedelta(minutes=6)), "unchanged")
        self.assertEqual(len(self.client.edits), edits)
        oc.add_task(
            self.root,
            project_id="demo",
            task_id="card-two",
            lane_id="main",
            mode="patch",
            priority="p1",
            title="New",
            objective="Changes the counts",
            next_action="Go",
            exit_criteria=["Done"],
            assignee="claude-opus",
        )
        self.assertTrue(self.channel.refresh_panel(start + timedelta(minutes=12)))

    def test_pinning_another_message_unpins_the_old_panel(self):
        self.channel.handle_update(message("/status"))
        first = len(self.client.sent)
        self.press("v|pin", first)
        self.channel.handle_update(message("/status", update_id=2))
        second = len(self.client.sent)
        self.press("v|pin", second, update_id=52)
        self.assertEqual(self.client.unpins, [first])
        self.press("v|unpin", second, update_id=53)
        self.assertEqual(self.channel.panel, {})

    def test_deleted_panel_is_forgotten(self):
        self.channel.panel.update({"message_id": 42, "digest": "", "checked_at": ""})

        def gone(*args, **kwargs):
            raise oc.TelegramError("Bad Request: message to edit not found")

        self.client.edit_message = gone
        self.assertFalse(self.channel.refresh_panel())
        self.assertEqual(self.channel.panel, {})

    def test_stage_report_folds_the_task_picture(self):
        summary = "\n".join(f"结论 {i}" for i in range(10))
        self.channel.send_stage_report("阶段一", summary=summary, project="demo")
        text = self.client.sent[-1][1]
        self.assertIn("结论 0\n结论 1\n结论 2\n<blockquote expandable>结论 3", text)
        self.assertIn("<b>📋 项目任务</b>\n<blockquote expandable>", text)
        self.assertEqual(text.count("<blockquote"), text.count("</blockquote>"))

    def test_widget_panel_state_lists_what_needs_a_glance(self):
        data = json.loads(self.channel.panel_state())
        name, active, queued, blocked, review, rows = data["p"][0]
        self.assertEqual((name, blocked), ("demo", 1))
        self.assertEqual(rows[0][0], "b")
        self.assertNotIn("<", rows[0][1][:1])
        self.assertEqual(rows[0][2], oc._card_key("demo", "card-one"))
        self.assertEqual(data["u"], "demo_bot")

    def test_panel_command_sends_and_pins_a_widget(self):
        self.channel.handle_update(message("/panel"))
        html_text, data, caption = self.client.widgets[-1]
        self.assertIs(html_text, owner_widgets.PANEL_HTML)
        self.assertIn("demo", caption)
        widget_id = len(self.client.sent)
        self.assertEqual(self.client.pins, [widget_id])
        self.assertEqual(self.channel.widget["message_id"], widget_id)
        start = datetime.now(UTC) + timedelta(minutes=6)
        self.assertFalse(self.channel.refresh_widget(start), "unchanged state is not re-sent")
        oc.update_task(self.root, project_id="demo", task_id="card-one", status="active")
        self.assertTrue(self.channel.refresh_widget(start + timedelta(minutes=6)))
        self.assertEqual(self.client.widget_edits[-1][0], widget_id)
        self.assertEqual(json.loads(self.client.widget_edits[-1][1])["p"][0][1], 1)

    def test_widget_refresh_query_answers_only_the_owner(self):
        self.channel.handle_update(message("/panel"))
        widget_id = len(self.client.sent)
        query = {"id": "w1", "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                 "message_id": widget_id, "data": "refresh"}
        self.channel.handle_update({"update_id": 90, "widget_query": query})
        self.assertEqual(self.client.widget_answers[-1][0], "w1")
        self.assertEqual(json.loads(self.client.widget_answers[-1][1])["p"][0][0], "demo")
        stranger = dict(query, id="w2", **{"from": {"id": OWNER + 1}})
        self.channel.handle_update({"update_id": 91, "widget_query": stranger})
        self.assertEqual(self.client.widget_answers[-1], ("w2", None))

    def test_widget_failure_falls_back_to_the_text_overview(self):
        self.client.widget_error = "Bad Request: widgets are unavailable"
        self.channel.handle_update(message("/panel"))
        self.assertIn("暂时用不了小组件", self.client.sent[-1][1])
        self.assertEqual(self.channel.widget, {})

    def test_status_offers_the_widget(self):
        self.channel.handle_update(message("/status"))
        self.assertIn("v|widget", json.dumps(self.client.sent[-1][2]))

    def test_stage_report_is_a_rich_message_with_a_table(self):
        self.channel.rich_ok = True
        summary = "\n".join(f"结论 {i}" for i in range(6))
        self.channel.send_stage_report(
            "阶段一", summary=summary, project="demo",
            metrics=[{"label": "token", "before": "100", "after": "80"}], recommendations=["下一步"],
        )
        document, keyboard, fallback = self.client.rich[-1]
        self.assertIn("<h2>阶段一</h2>", document)
        self.assertIn("<table bordered striped>", document)
        self.assertIn("-20.0%", document)
        self.assertIn("<details><summary>展开详细结论（3 段）</summary>", document)
        self.assertIn("<details><summary>📋 项目任务（1）</summary>", document)
        self.assertIn("<ol><li>下一步</li></ol>", document)
        self.assertIn("🏁 阶段完成", fallback)
        self.assertEqual(keyboard[0][0]["callback_data"], "a|demo|0")

    def test_refused_rich_message_falls_back_once(self):
        self.channel.rich_ok = True
        self.client.rich_error = "Bad Request: rich messages are unavailable"
        self.channel.send_stage_report("阶段一", summary="摘要", project="demo")
        self.assertFalse(self.channel.rich_ok)
        self.assertIn("🏁 阶段完成", self.client.sent[-1][1])

    def test_transient_rich_failure_is_retried_later(self):
        self.channel.rich_ok = True
        self.client.rich_error = "sendRichMessage failed: URLError"
        with self.assertRaises(oc.TelegramError):
            self.channel.send_stage_report("阶段一", summary="摘要", project="demo")
        self.assertTrue(self.channel.rich_ok)

    def test_completion_digest_is_a_rich_summary(self):
        self.channel.rich_ok = True
        oc.update_task(self.root, project_id="demo", task_id="card-one", status="completed",
                       result="118 个测试通过 & 已合并")
        self.channel._send_digest("demo", ["card-one"])
        document, keyboard, fallback = self.client.rich[-1]
        self.assertIn("<h3>1 项有了结果</h3>", document)
        self.assertIn("<table compact bordered><tr><th>完成</th><th>待审</th></tr>", document)
        self.assertIn("118 个测试通过 &amp; 已合并", document)
        self.assertIn('format="r"', document)
        self.assertIn("✅ 任务完成总结", fallback)
        self.assertEqual(document.count("<li>"), document.count("</li>"))

    def test_report_command_is_a_rich_table(self):
        self.channel.rich_ok = True
        self.channel.handle_update(message("/report"))
        document = self.client.rich[-1][0]
        self.assertIn("<th>项目</th><th>完成</th>", document)
        self.assertIn("<td>demo</td>", document)
        self.assertIn("<details open><summary>demo</summary><h4>⛔ 新阻塞</h4>", document)

    def test_timer_report_uses_the_rich_rendering(self):
        self.channel.rich_ok = True
        self.channel.add_timer("2h")
        self.channel.run_timers(datetime.now(UTC) + timedelta(hours=3))
        self.assertIn("🕘 进度报告", self.client.rich[-1][0])

    def test_alerts_are_rich_and_point_at_blocked_tasks(self):
        self.channel.rich_ok = True
        self.channel._send_alerts("demo", {"card-one": "阻塞"}, datetime.now(UTC))
        document, keyboard, _ = self.client.rich[-1]
        self.assertIn("<h3>1 项需要看一眼</h3>", document)
        self.assertEqual(keyboard[0][0]["style"], "danger")

    def test_refused_rich_digest_still_arrives(self):
        self.channel.rich_ok = True
        self.client.rich_error = "Bad Request: unsupported"
        self.channel._send_digest("demo", ["card-one"])
        self.assertIn("✅ 任务完成总结", self.client.sent[-1][1])

    def two_projects(self):
        register_project(self.root, project_id="other", control_root=self.root)
        self.channel.projects = ["demo", "other"]

    def test_topics_route_cards_and_replies_per_project(self):
        self.two_projects()
        self.assertTrue(self.channel.setup_topics({"has_topics_enabled": True}))
        self.assertEqual(self.client.topics_made, ["demo", "other"])
        self.card(text="hello")
        self.assertEqual(self.client.threads[-1], self.channel.topics["demo"])
        update = message("在 other 话题里说的话", update_id=95)
        update["message"]["message_thread_id"] = self.channel.topics["other"]
        update["message"]["reply_to_message"] = {"message_id": 1, "forum_topic_created": {"name": "other"}}
        self.channel.handle_update(update)
        inbox = list_messages(self.root, project_id="other", recipient="claude-opus", limit=5)
        self.assertEqual(inbox[0]["body"], "在 other 话题里说的话")
        self.assertIsNone(self.client.thread_id, "the thread is reset after handling")

    def test_commands_answer_in_the_topic_they_came_from(self):
        self.two_projects()
        self.channel.setup_topics({"has_topics_enabled": True})
        update = message("/status", update_id=96)
        update["message"]["message_thread_id"] = self.channel.topics["other"]
        self.channel.handle_update(update)
        self.assertEqual(self.client.threads[-1], self.channel.topics["other"])

    def test_without_topic_mode_everything_stays_in_one_chat(self):
        self.two_projects()
        self.assertFalse(self.channel.setup_topics({"has_topics_enabled": False}))
        self.card(text="hello")
        self.assertIsNone(self.client.threads[-1])
        self.channel.handle_update(message("没说是哪个项目", update_id=97))
        self.assertIn("发给哪个项目", self.client.sent[-1][1])

    def test_topic_creation_failure_disables_topic_mode(self):
        self.two_projects()

        def refuse(chat_id, name):
            raise oc.TelegramError("Bad Request: topics are disabled")

        self.client.create_topic = refuse
        self.assertFalse(self.channel.setup_topics({"has_topics_enabled": True}))
        self.assertFalse(self.channel.topic_mode)

    def test_serve_persists_receipts_and_panel(self):
        ticks = iter([False, True])
        oc.serve(self.channel, should_stop=lambda: next(ticks), sleep=lambda seconds: None)
        state = oc._read_state(self.root)
        self.assertEqual(state["receipts"], {})
        self.assertEqual(state["panel"], {})
        self.assertEqual(state["widget"], {})
        self.assertEqual(state["topics"], {})
        self.assertIs(self.channel.panel, self.channel.panel)

# Reuse the a38 fixtures, not its tests: those pin the legacy client's behaviour.
for _name in dir(base.OwnerChannelTest):
    if _name.startswith("test_") and _name not in PresentationTest.__dict__:
        setattr(PresentationTest, _name, None)


if __name__ == "__main__":
    unittest.main()
