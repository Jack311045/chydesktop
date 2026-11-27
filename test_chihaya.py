import os
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import json
from threading import Event
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx2
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from chihaya import CANVAS, MainWindow, Portrait
from calendar_reader import CalendarDay, CalendarError, Event as CalendarEvent
from claude_reply import ClaudeError, request_reply
from dialogue import Reply, respond
from desktop_today import briefing_text, collect_reminders
from task_chat import Operation, TaskAction, TaskReply, apply_task_reply
from tasks import Task, TaskError, TaskStore, local_date, plan_today


class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_tray_preserves_chat_and_quit_stops_timer(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.skipTest("No Windows system tray")
        with TemporaryDirectory() as directory:
            folder = Path(directory) / "portraits"
            window = MainWindow(folder)
            try:
                window.show()
                window.entry.setText("早上好")
                window.send()
                window.today.close_to_tray.setChecked(True)
                self.assertFalse(window.close())
                self.assertFalse(window.isVisible())
                self.assertTrue(window.timer.isActive())
                window.open_window()
                self.assertIn("早上好", window.history.toPlainText())
            finally:
                window.quit_app()
            self.assertFalse(window.timer.isActive())
            self.assertFalse(window.isVisible())
            reopened = MainWindow(folder)
            try:
                self.assertTrue(reopened.today.close_to_tray.isChecked())
            finally:
                reopened.quit_app()

    def test_prescribed_conversation_without_personal_art(self) -> None:
        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory) / "portraits")
            window.show()
            try:
                self.assertEqual(window.mode.currentText(), "离线")
                initial = window.history.toPlainText()
                neutral_render = window.portrait.grab().toImage()
                window.entry.setText("   ")
                QTest.keyClick(window.entry, Qt.Key.Key_Return)
                self.assertEqual(window.history.toPlainText(), initial)

                for message, expression in (
                    ("早上好", "smile"),
                    ("我今天要做什么", "focused"),
                ):
                    window.entry.setText(message)
                    if expression == "smile":
                        QTest.keyClick(window.entry, Qt.Key.Key_Return)
                    else:
                        QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
                    history = window.history.toPlainText()
                    self.assertIn(message, history)
                    self.assertIn(respond(message).reply, history)
                    self.assertEqual(window.portrait.expression, expression)
                    self.assertIsNone(window.portrait.artwork)
                    self.assertNotEqual(window.portrait.grab().toImage(), neutral_render)
                    self.assertEqual(window.entry.text(), "")
                    print(f"{message} -> {respond(message).reply} [{expression}]")
            finally:
                window.close()

    def test_artwork_fallback_and_shared_canvas_at_multiple_sizes(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            portrait = Portrait(folder)
            portrait.set_expression("focused")
            self.assertIsNone(portrait.artwork)

            for name, color in (("neutral", "blue"), ("smile", "green")):
                artwork = QPixmap(CANVAS)
                artwork.fill(QColor(color))
                self.assertTrue(artwork.save(str(folder / f"{name}.png")))
            invalid = QPixmap(32, 32)
            invalid.fill(QColor("red"))
            self.assertTrue(invalid.save(str(folder / "focused.png")))

            for size in ((260, 380), (340, 300)):
                portrait.resize(*size)
                portrait.set_expression("focused")
                fallback = portrait.artwork
                self.assertIsNotNone(fallback)
                assert fallback is not None
                self.assertEqual(fallback.toImage().pixelColor(0, 0), QColor("blue"))
                portrait.set_expression("smile")
                active = portrait.artwork
                self.assertIsNotNone(active)
                assert active is not None
                self.assertEqual(active.toImage().pixelColor(0, 0), QColor("green"))
                bounds = portrait.canvas_rect()
                portrait.set_expression("neutral")
                self.assertEqual(portrait.canvas_rect(), bounds)
                self.assertAlmostEqual(bounds.width() / bounds.height(), 640 / 855)


class DesktopTodayTests(unittest.TestCase):
    def test_briefing_and_reminders_use_local_task_and_calendar_facts(self) -> None:
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 23, 9, tzinfo=zone)
        day = now.date()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite3"
            store = TaskStore(path)
            task_id = store.add("Review CV", "2026-11-23 10:00", "30", "2026-11-23", today=day)
            store.add("Overdue", "2026-11-22", "", "", today=day)
            tasks = store.open_tasks()
            event = CalendarEvent("Local", "Standup", now + timedelta(minutes=30),
                                  now + timedelta(hours=1), False, True)
            snapshot = CalendarDay(day, zone, (), (event,), (), now)
            args = time(8), time(18), None
            result = briefing_text(now, tasks, snapshot, "Unavailable", *args)
            for text in ("2026-11-23", "Standup", "Overdue:", "Due today / Planned today:", "09:00–09:30 Review CV"):
                self.assertIn(text, result)
            self.assertIn("No calendar events today.", briefing_text(
                now, tasks, CalendarDay(day, zone, (), (), (), now), "Unavailable", *args))
            failed = briefing_text(now, tasks, None, "Unavailable", *args)
            self.assertIn("Unavailable", failed)
            self.assertIn("Review CV", failed)
            self.assertNotIn("Next opening", failed)
            self.assertEqual(collect_reminders(store, now - timedelta(minutes=1)), 0)
            self.assertEqual(collect_reminders(store, now), 1)
            reopened = TaskStore(path)
            self.assertEqual(collect_reminders(reopened, now), 0)
            self.assertEqual(collect_reminders(reopened, now + timedelta(hours=1)), 1)
            reopened.edit(task_id, "2026-11-23 11:00", "30", "2026-11-23", today=day)
            self.assertEqual(collect_reminders(reopened, now + timedelta(hours=2)), 1)
            self.assertEqual(len(reopened.recent_reminders()), 3)


class ClaudeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.config = self.folder / ".env"
        self.config.write_text("ANTHROPIC_API_KEY=fake-test-key\nANTHROPIC_MODEL=local-model\n")
        (self.folder / "persona.md").write_text("Local persona for testing.")

    def test_sdk_sends_bounded_history_and_persona_once(self) -> None:
        requests = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append((str(request.url), json.loads(request.content), request.headers))
            return httpx2.Response(200, json={
                "id": "msg_fake", "type": "message", "role": "assistant", "model": "override",
                "content": [{"type": "text", "text": json.dumps({
                    "reply": "Next step.", "expression": "unknown",
                    "action": {"mode": "none", "operations": []},
                })}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 25, "output_tokens": 12},
            })

        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "override"}), patch(
            "claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                transport=httpx2.MockTransport(handle), **kwargs)
        ):
            result = request_reply(self.folder, "Now?", [(f"Question {n}", f"Answer {n}") for n in range(8)])
        self.assertEqual(result, Reply("Next step.", "neutral"))
        self.assertEqual(len(requests), 1)
        endpoint, payload, headers = requests[0]
        self.assertEqual(endpoint, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers.get("authorization"), "Bearer fake-test-key")
        self.assertEqual(payload["model"], "override")
        self.assertTrue(payload["system"].startswith("Local persona for testing.\n"))
        self.assertEqual(payload["max_tokens"], 3072)
        self.assertEqual(payload["messages"][0]["content"], "Question 2")
        self.assertEqual(len(payload["messages"]), 13)
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": "Now?"})
        self.assertEqual(payload["output_config"]["format"]["type"], "json_schema")

    def test_sdk_errors_are_sanitized_and_not_retried(self) -> None:
        for code, reason, expected in (
            (None, httpx2.ReadTimeout, "超时"),
            (None, httpx2.ConnectError, "无法连接到 Claude"),
            (401, None, "身份验证"),
            (400, None, "额度不足"),
            (429, None, "速率或用量上限"),
        ):
            with self.subTest(code=code, reason=reason):
                calls = []

                def handle(request: httpx2.Request) -> httpx2.Response:
                    calls.append(request)
                    if reason is not None:
                        raise reason("fake-test-key", request=request)
                    assert code is not None
                    return httpx2.Response(code, json={"error": {
                        "message": "credit balance fake-test-key",
                    }}, headers={"x-should-retry": "true"})

                with patch("claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                    transport=httpx2.MockTransport(handle), **kwargs
                )):
                    with self.assertRaisesRegex(ClaudeError, expected) as failure:
                        request_reply(self.folder, "Test", [])
                self.assertNotIn("fake-test-key", str(failure.exception))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].extensions["timeout"]["read"], 20.0)

    def test_calendar_chat_sends_computed_openings_for_the_requested_date(self) -> None:
        window = MainWindow(self.folder / "portraits")
        zone = window.calendar.zone
        day = datetime.now(zone).date() + timedelta(days=1)
        busy = datetime(day.year, day.month, day.day, 15, tzinfo=zone)
        snapshot = CalendarDay(day, zone, (), (
            CalendarEvent("Private calendar", "Private appointment", busy,
                          busy + timedelta(minutes=30), False, True),
        ), (), datetime.now(zone))
        payloads = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            payloads.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "msg_fake", "type": "message", "role": "assistant", "model": "local-model",
                "content": [{"type": "text", "text": '{"reply":"這是你明天的空檔。","expression":"focused"}'}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 25, "output_tokens": 12},
            })

        window.show()
        window.mode.setCurrentText("Claude")
        window.today.start.setText("14:00")
        window.today.end.setText("16:00")
        try:
            with patch.object(window.calendar.reader, "read", return_value=snapshot) as read, patch(
                "claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                    transport=httpx2.MockTransport(handle), **kwargs)
            ):
                window.entry.setText("明天我什麼時候有空")
                window.send()
                self.wait_for_request(window)
            read.assert_called_once_with(day, zone, reuse=True, selected=("primary",))
            payload, = payloads
            self.assertEqual(payload["messages"], [{"role": "user", "content": "明天我什麼時候有空"}])
            self.assertIn(str(day), payload["system"])
            self.assertIn("14:00", payload["system"])
            self.assertIn("15:30", payload["system"])
            self.assertNotIn("08:00", payload["system"])
            self.assertNotIn("Private", payload["system"])
            self.assertIn("15:30", window.history.toPlainText())
        finally:
            window.close()

    def test_calendar_follow_up_survives_history_trim_and_keeps_entries_local(self) -> None:
        window = MainWindow(self.folder / "portraits")
        zone = ZoneInfo("America/New_York")
        day = date(2026, 11, 23)
        now = datetime(2026, 11, 22, 12, tzinfo=zone)
        window.calendar.zone = zone
        window.mode.setCurrentText("Claude")
        base = datetime.combine(day, time(15, 30), zone)
        events = (
            CalendarEvent("Private", "RDS review", base, base + timedelta(hours=1), False, True),
            CalendarEvent("Private", "Engineering standup", base + timedelta(minutes=30),
                          base + timedelta(minutes=75), False, True),
        )
        payloads = []

        def read(day_requested, zone_requested, **kwargs):
            return CalendarDay(day_requested, zone_requested, (), events if day_requested == day else (), (), now)

        def handle(request):
            payloads.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "msg_fake", "type": "message", "role": "assistant", "model": "local-model",
                "content": [{"type": "text", "text": json.dumps({
                    "reply": "I checked.", "expression": "focused",
                    "action": {"mode": "none", "operations": []},
                })}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 25, "output_tokens": 12},
            })

        def send(message):
            window.entry.setText(message)
            window.send()
            self.wait_for_request(window)

        try:
            with patch("chihaya.datetime", wraps=datetime) as clock, patch(
                "calendar_chat.datetime", wraps=datetime
            ) as calendar_clock, patch.object(window.calendar.reader, "read", side_effect=read) as reader, patch(
                "claude_reply.DefaultHttpxClient", side_effect=lambda **kwargs: httpx2.Client(
                    transport=httpx2.MockTransport(handle), **kwargs)
            ):
                clock.now.return_value = now
                calendar_clock.now.return_value = now
                send("11/23 15:15 到 16:45，我什麼時候有空？")
                self.assertIn("15:15 EST–15:30 EST", payloads[-1]["system"])
                self.assertIn("15:30–16:45", payloads[-1]["system"])
                window.calendar.day.setDate(QDate(2026, 11, 25))
                for n in range(7):
                    send(f"Ordinary message {n}")
                send("我其他時間都在做什麼？")
                reader.assert_called_with(day, zone, reuse=True, selected=("primary",))
                self.assertEqual(payloads[-1]["messages"][0]["content"], "Ordinary message 1")
                self.assertEqual(payloads[-1]["messages"][1]["role"], "assistant")
                self.assertIn("15:15–16:45", payloads[-1]["system"])
                self.assertIn("15:30–16:30  RDS review", window.history.toPlainText())
                self.assertIn("16:00–16:45  Engineering standup", window.history.toPlainText())
                send("那下午四點呢？")
                self.assertIn("16:00: 忙碌", payloads[-1]["system"])
                send("2026-11-24 我什麼時候有空？")
                reader.assert_called_with(date(2026, 11, 24), zone, reuse=True, selected=("primary",))
                self.assertNotIn("RDS review", str(payloads))
                self.assertNotIn("Engineering standup", str(payloads))
                window.clear_conversation()
                send("其他時間呢？")
                self.assertIn("你想查哪一天或哪个时段？", window.history.toPlainText())
                self.assertIsNone(window.calendar_context)
        finally:
            window.close()

    def test_pending_task_confirmation_consumes_one_validated_action(self) -> None:
        window = MainWindow(self.folder / "portraits")
        window.mode.setCurrentText("Claude")
        store = window.store
        assert store is not None

        def send(message):
            window.entry.setText(message)
            window.send()
            self.wait_for_request(window)

        def propose(title, minutes="30"):
            action = TaskAction("propose", (Operation("add", title=title, minutes=minutes),))
            with patch("chihaya.request_reply", return_value=TaskReply("Shall I add it?", "focused", action)):
                send("Propose a task")

        try:
            send("yes please")
            propose("Send two job applications")
            self.assertEqual(store.open_tasks(), [])
            send("yes please")
            send("yes please")
            propose("複習")
            send("好")
            propose("Rejected")
            send("no thanks")
            send("好")
            propose("Replaced")
            propose("Replacement")
            send("好")
            propose("Invalid", "0")
            self.assertIsNone(window.pending_action)
            propose("Write failure")
            with patch.object(store, "add", side_effect=TaskError("Could not save tasks.")):
                send("yes please")
            self.assertIn("Could not save tasks.", window.history.toPlainText())
            send("yes please")
            propose("Cleared")
            window.clear_conversation()
            send("好")
            self.assertEqual([(task.title, task.minutes) for task in store.open_tasks()],
                             [("Send two job applications", 30), ("複習", 30), ("Replacement", 30)])
        finally:
            window.close()

    def test_claude_is_default_when_configured_and_missing_key_is_explained(self) -> None:
        window = MainWindow(self.folder / "portraits")
        try:
            self.assertEqual(window.mode.currentText(), "Claude")
            self.assertIn("Claude 模式", window.status.text())
            self.assertNotIn("fake-test-key", window.status.text())
        finally:
            window.quit_app()
        self.config.unlink()
        offline = MainWindow(self.folder / "portraits")
        try:
            self.assertEqual(offline.mode.currentText(), "离线")
            self.assertIn(r"%LOCALAPPDATA%\Chihaya\.env", offline.status.text())
            offline.show()
            offline.show_conversation(True)
            self.assertTrue(offline.status.isVisible())
        finally:
            offline.quit_app()

    def test_switch_background_wait_and_manual_retry(self) -> None:
        window = MainWindow(self.folder / "portraits")
        window.show()
        release = Event()
        try:
            self.config.unlink()
            window.mode.setCurrentIndex(0)
            window.mode.setCurrentText("Claude")
            self.assertEqual(window.mode.currentText(), "离线")
            self.assertIn(r"%LOCALAPPDATA%\Chihaya\.env", window.status.text())
            self.config.write_text("ANTHROPIC_API_KEY=fake-test-key\n")
            window.mode.setCurrentText("Claude")
            window.entry.setText("Plan tomorrow")
            calls = []

            def fake_reply(folder, message, history, **kwargs):
                calls.append((message, history))
                if len(calls) == 1:
                    release.wait(3)
                    raise ClaudeError("Claude timed out.")
                return Reply("Which deadline comes first?", "focused")

            with patch("chihaya.request_reply", side_effect=fake_reply):
                window.send()
                self.assertFalse(window.send_button.isEnabled())
                self.assertIn("正在等待 Claude", window.status.text())
                self.assertFalse(window.close())
                window.send()
                release.set()
                self.wait_for_request(window)
                self.assertEqual(len(calls), 1)
                self.assertIn("timed out", window.status.text())
                self.assertTrue(window.send_button.isEnabled())
                self.assertEqual(window.entry.text(), "Plan tomorrow")
                window.send()
                self.wait_for_request(window)
                self.assertEqual(calls, [("Plan tomorrow", ()), ("Plan tomorrow", ())])
                self.assertIn("Which deadline comes first?", window.history.toPlainText())
                self.assertEqual(window.portrait.expression, "focused")
                window.mode.setCurrentIndex(0)
                window.entry.setText("早上好")
                window.send()
                self.assertEqual(window.portrait.expression, "smile")
                self.assertEqual(len(calls), 2)
        finally:
            release.set()
            self.wait_for_request(window)
            window.close()

    def wait_for_request(self, window: MainWindow) -> None:
        for _ in range(200):
            if window.request is None:
                return
            QTest.qWait(20)
        self.fail("Claude worker did not finish")

    def test_task_actions_save_clarify_and_report_failed_write(self) -> None:
        window = MainWindow(self.folder / "portraits")
        window.show()
        window.mode.setCurrentText("Claude")
        store = window.store
        assert store is not None

        def send(message, mode, *operations, reply="好的。"):
            action = TaskAction(mode, tuple(operations))
            with patch("chihaya.request_reply", return_value=TaskReply(reply, "focused", action)) as claude:
                window.entry.setText(message)
                window.send()
                self.wait_for_request(window)
            return claude

        claim = "Unverified model claim."
        try:
            send("帮我加一项：这周五前投两个岗位，预计 30 分钟。", "execute",
                 Operation("add", title="投两个岗位", date=local_date("这周五", date.today()).isoformat(), minutes="30"))
            saved, = store.open_tasks()
            self.assertEqual((saved.title, saved.minutes, saved.deadline),
                             ("投两个岗位", 30, local_date("这周五", date.today()).isoformat()))
            self.assertEqual(len(window.tasks.visible_tasks()), 1)
            send("我今天只有一小时，先做哪项？", "none", Operation("plan", minutes="60"))
            self.assertIn("可安排 30 / 60 分钟", window.history.toPlainText())
            store.add("投两个岗位（备选）", "", "", "", today=date.today())
            send("Complete 投两个", "execute", Operation("complete", target="投两个"), reply=claim)
            self.assertIn("请用 #编号 指定", window.history.toPlainText())
            send("Move #1", "execute", Operation("reschedule", target="#1", date="tomorrow"), reply=claim)
            self.assertIn("planned day or the deadline", window.history.toPlainText())
            send("I might add an interview task", "none")
            self.assertEqual(len(store.open_tasks()), 2)
            with store.transaction("test") as db:
                db.execute("CREATE TRIGGER fail_task BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            previous_rows = store.open_tasks()
            send("Add a resume review", "execute", Operation("add", title="resume review"), reply=claim)
            self.assertIn("保存失败", window.history.toPlainText())
            self.assertNotIn(claim, window.history.toPlainText())
            self.assertEqual(store.open_tasks(), previous_rows)
            self.assertTrue(window.send_button.isEnabled())
        finally:
            window.close()

    def test_chat_saves_a_list_of_plans_in_any_wording_with_context_and_undo(self) -> None:
        window = MainWindow(self.folder / "portraits")
        window.show()
        window.mode.setCurrentText("Claude")
        store = window.store
        assert store is not None
        store.add("复习 CSC 小考", "", "", "", today=date.today(), category="CSC")
        plans = tuple(Operation("add", title=title, category="Project ABC") for title in ("写简历", "投三个岗位", "跑步"))

        def send(message, mode, *operations, reply="好的。"):
            with patch("chihaya.request_reply", return_value=TaskReply(reply, "focused", TaskAction(mode, tuple(operations)))) as claude:
                window.entry.setText(message)
                window.send()
                self.wait_for_request(window)
            return claude

        try:
            claude = send("我今天剩下要写简历、投三个岗位、再跑步。", "propose", *plans, reply="要把这三件事放进待办吗？")
            context = claude.call_args.kwargs["task_context"]
            self.assertIn("复习 CSC 小考", context)
            self.assertEqual(len(store.open_tasks()), 1)
            self.assertIn("写简历", window.history.toPlainText())
            claude = send("你帮我把这三件事放入代办吧", "approve_pending")
            self.assertIn("pending_proposal", claude.call_args.kwargs["task_context"])
            self.assertNotIn("没有修改任务", window.history.toPlainText())
            self.assertEqual([task.title for task in store.open_tasks()], ["复习 CSC 小考", "写简历", "投三个岗位", "跑步"])
            self.assertEqual(store.categories(), ["CSC", "Project ABC"])
            self.assertIn("已新增", window.history.toPlainText())
            send("算了，刚才那些撤销", "execute", Operation("undo"))
            self.assertEqual([task.title for task in store.open_tasks()], ["复习 CSC 小考"])
            send("新开一个项目 Thesis，作为一个类别", "execute", Operation("create_category", category="Thesis"))
            self.assertIn("Thesis", store.categories())
            send("把复习放进 Thesis", "execute", Operation("set_category", target="#1", category="thesis"))
            self.assertEqual(store.open_tasks()[0].category, "Thesis")
            send("删掉 Thesis 类别", "execute", Operation("delete_category", category="Thesis"))
            self.assertIn("Thesis", store.categories())
            self.assertIsNotNone(window.pending_action)
            send("好", "none")
            self.assertNotIn("Thesis", store.categories())
            self.assertIsNone(store.open_tasks()[0].category)
        finally:
            window.close()


class LocalTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_calendar_refresh_failure_preserves_manual_planning_and_chat(self) -> None:
        with TemporaryDirectory() as directory:
            window = MainWindow(Path(directory) / "portraits")
            calendar = window.calendar
            window.show()

            def wait_until(condition):
                for _ in range(100):
                    if condition():
                        return
                    QTest.qWait(20)
                self.fail("Calendar worker did not finish")

            try:
                window.tabs.setCurrentWidget(window.today)
                with patch.object(calendar.reader, "read", side_effect=CalendarError("Retry after timeout.")):
                    calendar.reader.token.touch()
                    calendar.set_view_mode("day")
                    wait_until(lambda: calendar.worker is None)
                    calendar.refresh_button.click()
                    wait_until(lambda: calendar.worker is None)
                self.assertIsNone(calendar.snapshot)
                self.assertIn("timeout", calendar.status.text())
                self.assertEqual(calendar.refresh_button.text(), "重试")
                window.today.budget.setText("60")
                window.today.suggest.click()
                self.assertIn("建议使用 0 / 60 分钟", window.today.output.toPlainText())
                zone = ZoneInfo("UTC")
                empty = CalendarDay(datetime.now(zone).date(), zone, (), (), (), datetime.now(zone))
                with patch.object(calendar.reader, "read", return_value=empty):
                    calendar.refresh_button.click()
                    wait_until(lambda: calendar.worker is None)
                    self.assertIn("已载入行程", calendar.status.text())
                    self.assertEqual(calendar.grid.message(), "这段时间没有行程")
                    window.tabs.setCurrentIndex(0)
                    window.entry.setText("我今天下午还有哪些空档？")
                    with patch("chihaya.request_reply") as claude:
                        window.send()
                        wait_until(lambda: window.request is None)
                        claude.assert_not_called()
                    self.assertIn("可用空档", window.history.toPlainText())
            finally:
                window.close()

    def test_offline_task_and_plan_controls_persist(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory) / "portraits"
            window = MainWindow(folder)
            window.show()
            today = date.today()
            friday = local_date("Friday", today)
            later = f"{friday + timedelta(days=3)} 15:45"
            try:
                window.tabs.setCurrentWidget(window.tasks)
                tasks = window.tasks
                for title, due, minutes in (("筛选岗位", "Friday", "45"), ("投递两个岗位", later, "30")):
                    editor = tasks.open_editor()
                    editor.title.setText(title)
                    editor.deadline.setText(due)
                    editor.minutes.setText(minutes)
                    editor.accept()
                self.assertEqual(len(tasks.visible_tasks()), 2)
                editor = tasks.open_editor()
                editor.title.setText("Invalid estimate")
                editor.minutes.setText("0")
                editor.accept()
                self.assertIn("正整数", editor.status.text())
                self.assertEqual(len(tasks.visible_tasks()), 2)
                editor.reject()
                window.tabs.setCurrentWidget(window.today)
                window.today.budget.setText("60")
                window.today.suggest.click()
                plan = window.today.output.toPlainText()
                self.assertIn("建议使用 45 / 60 分钟", plan)
                self.assertIn("建议顺序\n筛选岗位", plan)
                self.assertIn("排不下\n投递两个岗位", plan)
                window.tabs.setCurrentWidget(tasks)
                first = tasks.visible_tasks()[0]
                tasks.select_task(first.id)
                self.assertEqual(tasks.editor.deadline.text(), friday.isoformat())
                tasks.complete.click()
                second = tasks.visible_tasks()[0]
                tasks.select_task(second.id)
                tasks.editor.day.setText("tomorrow")
                tasks.editor.accept()
            finally:
                window.close()
            reopened = MainWindow(folder)
            try:
                shown = reopened.tasks.visible_tasks()
                self.assertEqual(len(shown), 1)
                self.assertEqual(shown[0].deadline, later)
                self.assertEqual(shown[0].planned_day, str(today + timedelta(days=1)))
                with closing(sqlite3.connect(folder.parent / "tasks.sqlite3")) as db:
                    self.assertEqual(db.execute("SELECT status, deadline FROM tasks WHERE id = 1").fetchone(),
                                     ("complete", friday.isoformat()))
            finally:
                reopened.close()

    def test_plan_priority_and_action_target_safety(self) -> None:
        now = datetime(2026, 11, 20, 12)
        tasks = [
            Task(1, "unplanned", None, 10, None),
            Task(2, "today", None, 10, "2026-11-20"),
            Task(3, "due tonight", "2026-11-20", 10, "2026-11-21"),
            Task(4, "overdue", "2026-11-20 09:30", 10, None),
            Task(5, "tomorrow", None, 10, "2026-11-21"),
            Task(6, "needs estimate", None, None, None),
        ]
        plan = plan_today(tasks, 30, now)
        self.assertEqual([task.id for task in plan.fitted], [4, 3, 2])
        self.assertEqual([task.id for task in plan.unscheduled], [1])
        self.assertEqual([task.id for task in plan.unestimated], [6])
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            store.add("Review", "Friday 15:45", "20", "", today=now.date())
            reply = TaskReply("Guess", "focused", TaskAction("execute", (Operation("complete", target="#10"),)))
            outcome = apply_task_reply(reply, "Complete #10", store, now)
            self.assertFalse(outcome.changed)
            self.assertNotIn("Guess", outcome.reply.reply)
            move = TaskReply("Guess", "focused", TaskAction("execute", (Operation(
                "reschedule", target="#1", field="planned_day", date="tomorrow",
            ),)))
            outcome = apply_task_reply(move, "Move the planned day for #1 to tomorrow", store, now)
            self.assertTrue(outcome.changed)
            moved, = store.open_tasks()
            self.assertEqual((moved.deadline, moved.planned_day), ("2026-11-20 15:45", "2026-11-21"))
