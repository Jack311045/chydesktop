import os
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from calendar_reader import Calendar, CalendarDay, Event
from chihaya import MainWindow

ZONE = ZoneInfo("America/New_York")
SUNDAY = date(2026, 11, 22)
CALENDARS = (Calendar("primary", "Main calendar"), Calendar("b", "Spring course"))


def event(day_offset, hour, title="Class", calendar="Main calendar"):
    day = SUNDAY + timedelta(days=day_offset)
    start = datetime(day.year, day.month, day.day, hour, tzinfo=ZONE)
    return Event(calendar, title, start, start + timedelta(hours=1), False, True)


def week_for(first):
    events = [event(1, 14, "Monday class"), event(2, 9, "Tuesday lab", "Spring course")]
    result = []
    for offset in range(7):
        day = first + timedelta(days=offset)
        result.append(CalendarDay(day, ZONE, CALENDARS, tuple(e for e in events if e.start.date() == day),
                                  (), datetime.now(ZONE), ("primary",)))
    return result


class CalendarPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, condition):
        for _ in range(250):
            if condition():
                return
            QTest.qWait(10)
        self.fail("Calendar worker did not finish")

    def make_window(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        window = MainWindow(Path(directory.name) / "portraits")
        self.addCleanup(window.quit_app)
        window.show()
        window.calendar.reader.token.write_text("{}")
        window.calendar.attempted = True
        return window

    def test_week_view_loads_sunday_first_range_navigates_and_shows_details(self):
        window = self.make_window()
        calendar = window.calendar
        calls = []

        def read_days(days, zone, **kwargs):
            calls.append(list(days))
            return week_for(days[0])

        with patch.object(calendar.reader, "read_days", side_effect=read_days), patch.object(
            calendar.reader, "selected", return_value=("primary",)
        ):
            # Moving the date refreshes when it differs from today, so do it where the reader is mocked.
            calendar.day.setDate(QDate(2026, 11, 23))
            self.wait_for(lambda: calendar.worker is None)
            calendar.refresh()
            self.wait_for(lambda: calendar.worker is None)
            self.assertEqual(calls[-1][0], SUNDAY)
            self.assertEqual(len(calls[-1]), 7)
            self.assertEqual(calendar.snapshot.day, date(2026, 11, 23))
            self.assertEqual(calendar.title.text(), "2026年11月")
            self.assertEqual(sorted(item.title for item, _ in calendar.grid.event_rects()),
                             ["Monday class", "Tuesday lab"])
            self.assertEqual(calendar.grid.message(), "")

            count = len(calls)
            calendar.day.setDate(QDate(2026, 11, 25))
            self.assertEqual(len(calls), count)
            self.assertEqual(calendar.snapshot.day, date(2026, 11, 25))

            calendar.next.click()
            self.wait_for(lambda: calendar.worker is None)
            self.assertEqual(calls[-1][0], SUNDAY + timedelta(days=7))
            self.assertEqual(calendar.day.date().toPython(), date(2026, 12, 2))
            self.assertEqual(calendar.grid.message(), "这段时间没有行程")

            calendar.previous.click()
            self.wait_for(lambda: calendar.worker is None)
            self.assertEqual(calls[-1][0], SUNDAY)
            self.assertEqual(len(calendar.week_events()), 2)

            calendar.day.setDate(QDate(2026, 11, 23))
            target = calendar.week_events()[0]
            calendar.grid.select_event(target)
            self.assertFalse(calendar.detail.isHidden())
            text = calendar.detail_text.text()
            self.assertLess(text.index("名称"), text.index("类别"))
            self.assertLess(text.index("类别"), text.index("预估时长"))
            self.assertLess(text.index("预估时长"), text.index("已安排时段"))
            self.assertLess(text.index("已安排时段"), text.index("截止日期"))
            self.assertLess(text.index("截止日期"), text.index("完成状态"))
            self.assertIn("不适用", text)
            calendar.set_view_mode("day")
            self.assertEqual(calendar.range_days(), [date(2026, 11, 23)])

    def test_calendar_choices_stay_in_sync_between_sidebar_and_settings(self):
        window = self.make_window()
        calendar = window.calendar
        snapshot = week_for(SUNDAY)[1]
        calendar.populate_calendars(snapshot, ("primary",))
        saved = []
        with patch.object(calendar.reader, "save_selection", side_effect=saved.append), patch.object(
            calendar, "refresh"
        ) as refresh:
            calendar.side_calendars.item(1).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(saved, [("primary", "b")])
        self.assertEqual(calendar.calendars.item(1).checkState(), Qt.CheckState.Checked)
        refresh.assert_called_once()
        self.assertEqual(calendar.side_calendars.item(0).icon().isNull(), False)

    def test_disconnected_calendar_explains_where_to_connect_and_panel_toggles(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        window = MainWindow(Path(directory.name) / "portraits")
        self.addCleanup(window.quit_app)
        window.show()
        window.navigate("today")
        self.assertIn("设置", window.calendar.grid.message())
        self.assertTrue(window.today.scroll.isHidden())
        window.calendar.panel_button.click()
        self.assertTrue(window.today.scroll.isVisible())
        window.navigate("calendar")
        self.assertTrue(window.today.scroll.isVisible())
        window.calendar.panel_button.click()
        self.assertTrue(window.today.scroll.isHidden())


if __name__ == "__main__":
    unittest.main()
