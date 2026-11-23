from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from calendar_reader import CalendarDay, Event
from chihaya import CANVAS, MainWindow


class OfficeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name) / "portraits"
        self.window = MainWindow(self.folder)
        self.addCleanup(self.window.quit_app)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()

    def test_launcher_routes_keyboard_focus_and_chat_continuity(self):
        win = self.window
        self.assertIs(win.tabs.currentWidget(), win.office)
        win.entry.setText("A saved draft")
        history = win.history.toPlainText()
        yesterday = QDate.currentDate().addDays(-1)
        win.calendar.day.setDate(yesterday)
        for key, target, focus, nav in (
            ("clock", win.today, win.calendar.day, "today"),
            ("calendar_status", win.today, win.calendar.connect_button, "today"),
            ("mail_status", win.today, win.mail.connect_button, "mail"),
            ("today_status", win.today, win.daily_refresh, "today"),
            ("board", win.today, win.calendar.day, "today"),
            ("board_hint", win.today, win.calendar.day, "today"),
            ("character", win.chat_page, win.entry, "chat"),
            ("chat_hint", win.chat_page, win.entry, "chat"),
            ("monitor", win.today, win.mail.connect_button, "mail"),
            ("mail_hint", win.today, win.mail.connect_button, "mail"),
            ("event", win.today, win.calendar.day, "today"),
            ("task", win.tasks, win.tasks.list, "tasks"),
            ("opening", win.today, win.daily_refresh, "today"),
            ("brief", win.today, win.daily_refresh, "today"),
            ("due", win.today, win.today.due_list, "today"),
            ("unread", win.today, win.mail.connect_button, "mail"),
            ("follow", win.today, win.daily_rows, "today"),
            ("resume", win.chat_page, win.entry, "chat"),
            ("schedule", win.today, win.calendar.day, "today"),
        ):
            with self.subTest(key=key):
                button = win.office.buttons[key]
                button.setFocus()
                QTest.keyClick(button, Qt.Key.Key_Return if key == "clock" else Qt.Key.Key_Space)
                self.app.processEvents()
                self.assertIs(win.tabs.currentWidget(), target)
                self.assertTrue(focus.hasFocus())
                self.assertEqual([route for route, item in win.nav_buttons.items() if item.isChecked()], [nav])
                QTest.keyClick(focus, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertIs(win.tabs.currentWidget(), win.office)
                self.assertTrue(button.hasFocus())
        self.assertNotEqual(win.calendar.day.date(), yesterday)
        for key, target in (("today", win.today), ("tasks", win.tasks), ("chat", win.chat_page),
                            ("mail", win.today), ("office", win.office)):
            QTest.mouseClick(win.nav_buttons[key], Qt.MouseButton.LeftButton)
            self.assertIs(win.tabs.currentWidget(), target)
            self.assertTrue(win.nav_buttons[key].isChecked())
        self.assertEqual(win.entry.text(), "A saved draft")
        self.assertEqual(win.history.toPlainText(), history)
        self.assertEqual(win.store.open_tasks(), [])

    def test_scene_resizes_and_shares_portraits_behind_desk(self):
        win = self.window
        self.folder.mkdir()
        for expression, color in (("neutral", "#df1234"), ("smile", "#30d12a"), ("focused", "#e6d015")):
            art = QPixmap(CANVAS)
            art.fill(QColor(color))
            self.assertTrue(art.save(str(self.folder / f"{expression}.png")))
        for size in ((1100, 680), (875, 595), (680, 450)):
            win.resize(*size)
            self.app.processEvents()
            office = win.office
            scale, x, y = office.scene_transform()
            for key, button in office.buttons.items():
                self.assertTrue(office.rect().contains(button.geometry()), key)
                self.assertEqual(office.childAt(button.geometry().center()), button, key)
                self.assertTrue(button.accessibleName(), key)
            for expression, color in (("neutral", "#df1234"), ("smile", "#30d12a"), ("focused", "#e6d015")):
                win.portrait.set_expression(expression)
                image = office.grab().toImage()
                dpr = image.devicePixelRatio()
                self.assertEqual(image.pixelColor(round((x + 570 * scale) * dpr),
                                                  round((y + 320 * scale) * dpr)), QColor(color))
                self.assertNotEqual(image.pixelColor(round((x + 570 * scale) * dpr),
                                                     round((y + 428 * scale) * dpr)), QColor(color))

    def test_calendar_and_due_cards_use_local_data_without_writes(self):
        win = self.window
        zone = win.calendar.zone
        now = datetime.now(zone).replace(hour=9, minute=0, second=0, microsecond=0)
        today = now.date()
        due = win.store.add("Due today", today.isoformat(), "30", "", today=today)
        win.store.add("Planned only", "", "20", today.isoformat(), today=today)
        with patch("chihaya.datetime") as clock:
            clock.now.return_value = now
            win.update_office()
            self.assertIn("尚未連線", win.office.buttons["event"].text())
            self.assertIn("本機待辦", win.office.buttons["task"].text())
            self.assertIn("未連線", win.office.buttons["mail_status"].text())
            win.office.buttons["due"].click()
            self.assertEqual(win.today.due_list.count(), 1)
            self.assertEqual(win.today.due_list.item(0).data(Qt.ItemDataRole.UserRole), due)
            win.calendar.attempted = True
            win.calendar.reader.token.touch()
            win.briefing_last_attempt = now.timestamp()
            meeting = Event("Fixture", "Upcoming event", now + timedelta(hours=1),
                            now + timedelta(hours=2), False, True)
            for snapshot, failure, expected in (
                (None, "", "尚未載入"),
                (None, "failed", "讀取失敗"),
                (CalendarDay(today, zone, (), (), ("Fixture",), now), "", "只取得部分行程"),
                (CalendarDay(today, zone, (), (), (), now - timedelta(days=1)), "", "資料已過期"),
                (CalendarDay(today, zone, (), (), (), now, ("different",)), "", "資料已過期"),
                (CalendarDay(today, zone, (), (), (), now), "", "今日無行程"),
                (CalendarDay(today, zone, (), (meeting,), (), now), "", "Upcoming event"),
            ):
                win.briefing_snapshot, win.briefing_status = snapshot, failure
                win.update_office()
                self.assertIn(expected, win.office.buttons["event"].text())
            win.today.end.setText("10:00")
            win.update_briefing()
            self.assertIn("09:00–10:00", win.office.buttons["opening"].text())
            self.assertEqual(win.office_task_id, due)
            win.navigate("office")
            before = win.store.open_tasks()
            win.office.buttons["task"].click()
            self.assertEqual(win.tasks.selected_id(), due)
            self.assertEqual(win.store.open_tasks(), before)
            win.tasks.complete.click()
            self.assertIn(" · 0", win.office.buttons["due"].text())
            win.navigate("due")
            self.assertEqual(win.today.due_list.count(), 0)
            win.today.start.setText("10:00")
            win.today.end.setText("11:00")
            win.update_briefing()
            self.assertIn("無空檔", win.office.buttons["opening"].text())
