from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QDate, QPoint, Qt
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
            ("calendar_status", win.settings_page, win.calendar.connect_button, "settings"),
            ("mail_status", win.settings_page, win.mail.connect_button, "settings"),
            ("today_status", win.today, win.brief_links, "today"),
            ("board", win.today, win.calendar.day, "today"),
            ("character", win.office, win.entry, "office"),
            ("monitor", win.mail_page, win.mail.filter, "mail"),
            ("event", win.today, win.calendar.day, "today"),
            ("task", win.tasks, win.tasks.new_button, "tasks"),
            ("opening", win.today, win.brief_links, "today"),
            ("brief", win.today, win.brief_links, "today"),
            ("due", win.today, win.today.due_list, "today"),
            ("unread", win.mail_page, win.mail.filter, "mail"),
            ("follow", win.mail_page, win.daily_rows, "mail"),
            ("resume", win.office, win.entry, "office"),
            ("schedule", win.today, win.calendar.day, "today"),
        ):
            with self.subTest(key=key):
                button = win.office.buttons[key]
                button.setFocus()
                QTest.keyClick(button, Qt.Key.Key_Return if key == "clock" else Qt.Key.Key_Space)
                self.app.processEvents()
                self.assertIs(win.tabs.currentWidget(), target)
                self.assertTrue(focus.hasFocus())
                if target is win.office:
                    self.assertTrue(win.conversation.isVisible())
                self.assertEqual([route for route, item in win.nav_buttons.items() if item.property("current")], [nav])
                QTest.keyClick(focus, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertIs(win.tabs.currentWidget(), win.office)
                self.assertTrue(button.hasFocus())
        self.assertNotEqual(win.calendar.day.date(), yesterday)
        self.assertEqual(win.tabs.count(), 5)
        for key, target in (("today", win.today), ("tasks", win.tasks), ("settings", win.settings_page),
                            ("mail", win.mail_page), ("office", win.office)):
            QTest.mouseClick(win.nav_buttons[key], Qt.MouseButton.LeftButton)
            self.assertIs(win.tabs.currentWidget(), target)
            self.assertTrue(win.nav_buttons[key].property("current"))
            self.assertEqual(win.nav_buttons[key].accessibleDescription(), "当前页面")
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

    def test_labels_are_gone_and_character_hotspot_follows_her_silhouette(self):
        win = self.window
        win.resize(1100, 680)
        self.app.processEvents()
        office = win.office
        for removed in ("board_hint", "chat_hint"):
            self.assertNotIn(removed, office.buttons)
        for key in ("monitor", "board", "character"):
            self.assertEqual(office.buttons[key].text(), "")
            self.assertEqual(office.buttons[key].toolTip(), "")
            self.assertTrue(office.buttons[key].accessibleName())
        win.update_office()
        self.assertNotIn("千早桌面助手", office.buttons["clock"].text())
        self.assertRegex(office.buttons["clock"].text(), r"^\d{4}-\d{2}-\d{2}  周.  \d{2}:\d{2}$")
        button = office.buttons["character"]
        mask = button.mask()
        self.assertFalse(office.silhouette.isEmpty())
        self.assertFalse(mask.isEmpty())
        self.assertTrue(mask.contains(button.rect().center()))
        self.assertFalse(mask.contains(QPoint(0, 0)))
        self.assertIsNot(office.childAt(button.geometry().topLeft() + QPoint(1, 1)), button)
        plain = office.grab().toImage()
        with patch.object(type(button), "active", lambda self: True):
            office.update()
            outlined = office.grab().toImage()
        self.assertNotEqual(plain, outlined)
        left = office.grab().toImage()
        self.assertEqual(plain, left)

    def test_screenshot_task_requires_separate_confirmation(self):
        win = self.window
        win.stage_screenshot_task("Review application")
        self.assertIn("Review application", win.history.toPlainText())
        self.assertEqual(win.store.open_tasks(), [])
        win.entry.setText("确认")
        win.send()
        self.assertEqual([task.title for task in win.store.open_tasks()], ["Review application"])

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
            self.assertIn("尚未连接", win.office.buttons["event"].text())
            self.assertIn("本机待办", win.office.buttons["task"].text())
            self.assertIn("未连接", win.office.buttons["mail_status"].text())
            self.assertIn("未连接", win.office.buttons["monitor"].accessibleName())
            self.assertEqual(win.office.buttons["monitor"].text(), "")
            win.office.buttons["due"].click()
            self.assertEqual(win.today.due_list.count(), 1)
            self.assertEqual(win.today.due_list.item(0).data(Qt.ItemDataRole.UserRole), due)
            win.calendar.attempted = True
            win.calendar.reader.token.touch()
            win.briefing_last_attempt = now.timestamp()
            meeting = Event("Fixture", "Upcoming event", now + timedelta(hours=1),
                            now + timedelta(hours=2), False, True)
            for snapshot, failure, expected in (
                (None, "", "尚未载入"),
                (None, "failed", "读取失败"),
                (CalendarDay(today, zone, (), (), ("Fixture",), now), "", "只取得部分行程"),
                (CalendarDay(today, zone, (), (), (), now - timedelta(days=1)), "", "数据已过期"),
                (CalendarDay(today, zone, (), (), (), now, ("different",)), "", "数据已过期"),
                (CalendarDay(today, zone, (), (), (), now), "", "今日无行程"),
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
            self.assertIn("无空档", win.office.buttons["opening"].text())
