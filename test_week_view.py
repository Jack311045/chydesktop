import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollBar

from calendar_reader import Event
from week_view import EVENT_PALETTE, WEEKDAY_LABELS, WeekGrid, calendar_color

APP = QApplication.instance() or QApplication([])

ZONE = ZoneInfo("America/New_York")
SUNDAY = date(2026, 11, 22)
WEEK = [SUNDAY + timedelta(days=i) for i in range(7)]
NOW = datetime(2026, 11, 24, 10, 30, tzinfo=ZONE)
TODAY_RGB = 0xFF5AA9E6
NOW_RGB = 0xFFEA5B4A
LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier


def at(offset: int, hour: int, minute: int = 0, base: date = SUNDAY) -> datetime:
    return datetime.combine(base + timedelta(days=offset), time(hour, minute), ZONE)


def timed(calendar, title, start, end, busy=True) -> Event:
    return Event(calendar, title, start, end, False, busy)


def plan(title, start, end) -> Event:
    return Event("本机待办", title, start, end, False, True, local=True)


STANDUP = timed("工作", "站会", at(1, 9), at(1, 10))
REVIEW = timed("工作", "评审", at(1, 9, 30), at(1, 10, 30))
CLIENT = timed("个人", "客户会议", at(2, 14), at(2, 15, 15))
NIGHT = timed("值班", "夜班", at(3, 23), at(4, 1))
REMIND = timed("个人", "提醒", at(5, 12), at(5, 12))
BLANK = timed("个人", "", at(6, 8), at(6, 9), busy=False)
HOLIDAY = Event("节日", "感恩节假期", at(4, 0), at(7, 0), True, True)
EVENTS = [STANDUP, REVIEW, CLIENT, NIGHT, REMIND, BLANK, HOLIDAY]
AFTERCLASS = timed("课程", "课后自习", at(2, 15, 30), at(2, 16, 30))
GYM = timed("健身", "健身", at(2, 16, 30), at(2, 17, 30))


def pixel_set(grid: WeekGrid) -> set[int]:
    image = grid.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    return set(memoryview(image.constBits()).cast("B").cast("I"))


class WeekGridCase(unittest.TestCase):
    def setUp(self):
        self.grid = WeekGrid()
        self.grid.resize(1000, 1300)
        self.grid.show()
        self.grid.set_range(WEEK, EVENTS, ZONE, now=NOW)
        self.selected, self.activated, self.days = [], [], []
        self.grid.event_selected.connect(self.selected.append)
        self.grid.event_activated.connect(self.activated.append)
        self.grid.day_selected.connect(self.days.append)

    def tearDown(self):
        self.grid.close()
        self.grid.deleteLater()

    def column_of(self, rect):
        for index, column in enumerate(self.grid.day_column_rects()):
            if column.left() <= rect.center().x() <= column.right():
                return index
        self.fail(f"rect {rect} is outside every day column")

    def rect_of(self, event, column=None):
        for ev, rect in self.grid.event_rects():
            if ev == event and (column is None or self.column_of(rect) == column):
                return rect
        self.fail(f"no rect for {event.title!r} in column {column}")

    def click(self, point, button=LEFT):
        QTest.mouseClick(self.grid, button, NO_MOD, QPoint(round(point.x()), round(point.y())))

    def key(self, key):
        QTest.keyClick(self.grid, key)

    def show(self, *events, days=WEEK):
        self.grid.set_range(days, list(events), ZONE, now=NOW)
        self.grid.scroll_to_hour(8)

    def facts(self, event):
        return next(row for row in self.grid.describe_layout() if row["event"] == event)

    # (1)
    def test_week_layout_rects_inside_day_columns_in_chronological_order(self):
        rects = self.grid.event_rects()
        self.assertEqual(len(rects), 7)
        columns = self.grid.day_column_rects()
        self.assertEqual(len(columns), 7)
        per_column: dict[int, list[tuple[float, float]]] = {}
        for event, rect in rects:
            index = self.column_of(rect)
            column = columns[index]
            self.assertGreaterEqual(rect.left(), column.left())
            self.assertLessEqual(rect.right(), column.right())
            start = event.start
            minutes = start.hour * 60 + start.minute if start.date() == WEEK[index] else 0
            per_column.setdefault(index, []).append((minutes, rect.top()))
        for index, items in per_column.items():
            ordered = [top for _, top in sorted(items)]
            self.assertEqual(ordered, sorted(ordered), f"column {index}")
        hour = (self.rect_of(CLIENT).top() - self.rect_of(STANDUP).top()) / 5
        self.assertAlmostEqual(hour, 44, delta=12)
        self.assertGreaterEqual(self.rect_of(REMIND).height(), 18)
        self.assertEqual(self.grid.accessibleName(), "行程时间表")
        self.assertEqual(self.grid.accessibleDescription(), "共 7 个行程")
        self.assertEqual(WEEKDAY_LABELS[SUNDAY.weekday()], "周日")
        self.assertEqual(self.grid.gutter_caption(), "GMT-5")

    # (2)
    def test_overlapping_events_get_disjoint_x_ranges(self):
        first, second, alone = self.rect_of(STANDUP), self.rect_of(REVIEW), self.rect_of(CLIENT)
        self.assertTrue(first.right() <= second.left() or second.right() <= first.left())
        self.assertAlmostEqual(first.width(), second.width(), delta=1.5)
        self.assertGreater(alone.width(), first.width() * 1.8)
        self.grid.set_range(WEEK[1:2], [STANDUP, REVIEW, timed("个人", "三重", at(1, 9, 15), at(1, 9, 45))], ZONE, now=NOW)
        timed_rects = [r for _, r in self.grid.event_rects()]
        self.assertEqual(len(timed_rects), 3)
        for i, a in enumerate(timed_rects):
            for b in timed_rects[i + 1:]:
                self.assertTrue(a.right() <= b.left() + 0.01 or b.right() <= a.left() + 0.01)
        self.assertAlmostEqual(timed_rects[0].width(), timed_rects[2].width(), delta=1.5)
        self.assertLess(timed_rects[0].width(), self.grid.day_column_rects()[0].width() * 0.4)

    def test_back_to_back_events_share_a_full_width_column(self):
        a, b = timed("工作", "甲", at(2, 8), at(2, 9)), timed("工作", "乙", at(2, 9), at(2, 10))
        self.grid.set_range(WEEK, [a, b], ZONE, now=NOW)
        self.assertAlmostEqual(self.rect_of(a).width(), self.rect_of(b).width(), delta=0.01)
        self.assertLessEqual(self.rect_of(a).bottom(), self.rect_of(b).top())

    # (3)
    def test_midnight_crossing_and_multi_day_all_day_events(self):
        night = [(self.column_of(r), r) for e, r in self.grid.event_rects() if e == NIGHT]
        self.assertEqual([c for c, _ in night], [3, 4])
        evening, morning = night[0][1], night[1][1]
        self.assertGreater(evening.height(), 30)
        self.assertGreater(morning.top(), evening.top() - 24 * 44)
        self.assertLess(morning.top(), evening.top())
        self.assertAlmostEqual(morning.height(), 44 - 1, delta=14)
        chips = [(self.column_of(r), e) for e, r in self.grid.all_day_rects()]
        self.assertEqual(sorted(c for c, e in chips), [4, 5, 6])
        self.assertTrue(all(e == HOLIDAY for _, e in chips))
        self.assertEqual(self.grid.all_day_overflow(), {})

    def test_all_day_strip_shows_three_chips_then_overflow_count(self):
        before = self.grid.day_column_rects()[0].top()
        extras = [Event("节日", f"全天{i}", at(2, 0), at(3, 0), True, True) for i in range(5)]
        self.grid.set_range(WEEK, EVENTS + extras, ZONE, now=NOW)
        in_column = [r for e, r in self.grid.all_day_rects() if self.column_of(r) == 2]
        self.assertEqual(len(in_column), 3)
        self.assertEqual(self.grid.all_day_overflow(), {WEEK[2]: 2})
        self.assertGreater(self.grid.day_column_rects()[0].top(), before)
        tooltip = self.grid.tooltip_at(QPointF(self.grid.day_column_rects()[2].center().x(), self.grid.day_column_rects()[2].top() - 6))
        self.assertIn("还有 2 个全天行程", tooltip)

    # (4)
    def test_click_selects_events_and_emits_signals(self):
        center = self.rect_of(REVIEW).center()
        self.assertEqual(self.grid.event_at(center), REVIEW)
        self.assertEqual(self.grid.event_at(QPoint(round(center.x()), round(center.y()))), REVIEW)
        self.click(center)
        self.assertEqual(self.grid.selected_event, REVIEW)
        self.assertEqual(self.selected, [REVIEW])
        self.click(center)
        self.assertEqual(self.selected, [REVIEW])
        empty = self.grid.day_column_rects()[0].center()
        self.assertIsNone(self.grid.event_at(empty))
        self.click(empty)
        self.assertIsNone(self.grid.selected_event)
        self.assertEqual(self.selected, [REVIEW, None])
        chip = next(r for e, r in self.grid.all_day_rects() if e == HOLIDAY)
        self.click(chip.center())
        self.assertEqual(self.grid.selected_event, HOLIDAY)

    def test_double_click_activates_and_header_click_selects_day(self):
        center = self.rect_of(CLIENT).center()
        QTest.mouseDClick(self.grid, LEFT, NO_MOD, QPoint(round(center.x()), round(center.y())))
        self.assertEqual(self.activated, [CLIENT])
        self.assertEqual(self.grid.selected_event, CLIENT)
        column = self.grid.day_column_rects()[3]
        self.click(QPointF(column.center().x(), 20))
        self.assertEqual(self.days, [WEEK[3]])

    def test_tooltip_describes_event(self):
        text = self.grid.tooltip_at(self.rect_of(CLIENT).center())
        self.assertIn("客户会议", text)
        self.assertIn("14:00–15:15", text)
        self.assertIn("个人", text)
        self.grid.scroll_to_hour(6)
        self.assertIn("（无标题）", self.grid.tooltip_at(self.rect_of(BLANK).center()))
        self.assertEqual(self.grid.tooltip_at(self.grid.day_column_rects()[0].center()), "")

    def test_keyboard_navigation(self):
        self.key(Qt.Key.Key_Home)
        self.assertEqual(self.grid.selected_event, STANDUP)
        self.key(Qt.Key.Key_Down)
        self.assertEqual(self.grid.selected_event, REVIEW)
        self.key(Qt.Key.Key_Right)
        self.assertEqual(self.grid.selected_event, CLIENT)
        self.key(Qt.Key.Key_Left)
        self.assertEqual(self.grid.selected_event, REVIEW)
        self.key(Qt.Key.Key_Up)
        self.assertEqual(self.grid.selected_event, STANDUP)
        self.key(Qt.Key.Key_Up)
        self.assertEqual(self.grid.selected_event, STANDUP)
        self.key(Qt.Key.Key_Left)
        self.assertEqual(self.grid.selected_event, STANDUP)
        self.key(Qt.Key.Key_End)
        self.assertEqual(self.grid.selected_event, BLANK)
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Space)
        self.assertEqual(self.activated, [BLANK, BLANK])
        self.key(Qt.Key.Key_Escape)
        self.assertIsNone(self.grid.selected_event)
        self.assertEqual(self.selected[-1], None)
        self.key(Qt.Key.Key_Return)
        self.assertEqual(len(self.activated), 2)

    def test_keyboard_visits_each_event_once_in_start_order(self):
        seen = []
        for _ in range(10):
            self.key(Qt.Key.Key_Down)
            seen.append(self.grid.selected_event)
        self.assertEqual(seen, [STANDUP, REVIEW, CLIENT, NIGHT, HOLIDAY, REMIND, BLANK, BLANK, BLANK, BLANK])
        for _ in range(6):
            self.key(Qt.Key.Key_Up)
        self.assertEqual(self.grid.selected_event, STANDUP)

    def test_selection_survives_refresh_only_if_event_still_present(self):
        self.grid.select_event(REVIEW)
        self.assertEqual(self.selected, [REVIEW])
        self.grid.set_range(WEEK, EVENTS, ZONE, now=NOW)
        self.assertEqual(self.grid.selected_event, REVIEW)
        self.assertEqual(self.selected, [REVIEW])
        self.grid.set_range(WEEK, [STANDUP], ZONE, now=NOW)
        self.assertIsNone(self.grid.selected_event)
        self.assertEqual(self.selected, [REVIEW, None])

    # local plans, lightened and masked events
    def test_overlapping_plan_is_a_right_aligned_lane_over_full_width_events(self):
        slot = plan("写报告", at(2, 16), at(2, 16, 45))
        self.show(AFTERCLASS, GYM, slot, days=WEEK[1:4])
        column = self.grid.day_column_rects()[1]
        lane, gym, after = self.rect_of(slot), self.rect_of(GYM), self.rect_of(AFTERCLASS)
        self.assertTrue(self.facts(slot)["overlay"])
        self.assertAlmostEqual(lane.width(), 0.38 * column.width(), delta=1.5)
        self.assertAlmostEqual(lane.right(), gym.right(), delta=0.01)
        self.assertGreaterEqual(lane.left(), column.left())
        self.assertLessEqual(lane.right(), column.right())
        self.assertTrue(after.top() < lane.top() < gym.top())
        for below in (gym, after):
            self.assertAlmostEqual(below.width(), column.width() - 4, delta=0.01)
        self.assertEqual(self.grid.event_rects()[-1], (slot, lane))
        self.show(AFTERCLASS, GYM, slot)
        self.assertAlmostEqual(self.rect_of(slot).width(), 56, delta=0.01)

    def test_overlapping_plans_split_the_lane_without_touching_the_events(self):
        first, second = plan("甲", at(2, 16), at(2, 16, 45)), plan("乙", at(2, 16, 30), at(2, 17, 15))
        self.show(AFTERCLASS, GYM, first, second, days=WEEK[1:4])
        column = self.grid.day_column_rects()[1]
        a, b, gym = self.rect_of(first), self.rect_of(second), self.rect_of(GYM)
        self.assertLessEqual(a.right(), b.left())
        self.assertAlmostEqual(a.width(), b.width(), delta=1.5)
        self.assertAlmostEqual(a.united(b).width(), 0.38 * column.width(), delta=1.5)
        self.assertAlmostEqual(b.right(), gym.right(), delta=0.01)
        self.assertAlmostEqual(gym.width(), column.width() - 4, delta=0.01)

    def test_plan_without_a_real_event_is_full_width_and_leaves_event_packing_alone(self):
        self.show(STANDUP, REVIEW)
        before = [self.rect_of(STANDUP), self.rect_of(REVIEW)]
        free, other = plan("整理", at(1, 11), at(1, 12)), plan("复习", at(1, 11, 30), at(1, 12, 30))
        self.show(STANDUP, REVIEW, free, other)
        self.assertEqual([self.rect_of(STANDUP), self.rect_of(REVIEW)], before)
        self.assertFalse(self.facts(free)["overlay"] or self.facts(other)["overlay"])
        a, b = self.rect_of(free), self.rect_of(other)
        column = self.grid.day_column_rects()[1]
        self.assertLessEqual(a.right(), b.left())
        self.assertAlmostEqual(a.width(), b.width(), delta=1.5)
        self.assertAlmostEqual(a.united(b).width(), column.width() - 4, delta=0.01)
        self.show(STANDUP, REVIEW, free)
        self.assertAlmostEqual(self.rect_of(free).width(), column.width() - 4, delta=0.01)

    def test_overlay_detection_uses_the_minimum_block_height(self):
        brief = timed("个人", "短会", at(2, 16), at(2, 16, 5))
        near, far = plan("紧接", at(2, 16, 10), at(2, 16, 40)), plan("稍后", at(2, 17), at(2, 17, 30))
        self.show(brief, near, far)
        self.assertTrue(self.facts(near)["overlay"] and self.facts(brief)["dim"])
        self.assertFalse(self.facts(far)["overlay"])

    def test_events_under_plans_are_dimmed_and_masked_events_stay_usable(self):
        cancelled = Event("课程", "已取消的课", at(2, 9), at(2, 10, 15), False, True, masked=True)
        lonely = Event("课程", "没人覆盖", at(3, 12), at(3, 13), False, True, masked=True)
        over_masked = plan("补课", at(2, 9, 30), at(2, 10))
        slot = plan("写报告", at(2, 16), at(2, 16, 45))
        self.show(AFTERCLASS, GYM, CLIENT, cancelled, lonely, over_masked, slot)
        dimmed = {row["event"].title: row["dim"] for row in self.grid.describe_layout()}
        self.assertEqual(dimmed, {"课后自习": True, "健身": True, "客户会议": False, "已取消的课": True,
                                  "没人覆盖": False, "补课": False, "写报告": False})
        self.assertTrue(self.facts(lonely)["masked"] and not self.facts(over_masked)["masked"])
        self.assertEqual(self.grid.accessibleDescription(), "共 5 个行程")
        center = self.rect_of(lonely).center()
        self.assertEqual(self.grid.event_at(center), lonely)
        self.click(center)
        self.assertEqual(self.grid.selected_event, lonely)
        text = self.grid.tooltip_at(center)
        self.assertIn("没人覆盖", text)
        self.assertIn("日历：课程", text)
        self.assertIn("已在本机取消（Google 日历没有改动）", text)
        self.assertNotIn("已在本机取消", self.grid.tooltip_at(self.rect_of(CLIENT).center()))
        note = self.grid.tooltip_at(self.rect_of(slot).center())
        self.assertIn("写报告", note)
        self.assertIn("本机安排（不会写入 Google 日历）", note)
        self.assertNotIn("日历：", note)
        self.grid.grab()

    def test_overlay_wins_hit_testing_and_all_kinds_are_navigable(self):
        slot = plan("写报告", at(2, 16), at(2, 16, 45))
        self.show(AFTERCLASS, GYM, slot)
        lane = self.rect_of(slot)
        self.assertEqual(self.grid.event_at(lane.center()), slot)
        self.assertEqual(self.grid.event_at(QPointF(lane.left() - 8, lane.center().y())), AFTERCLASS)
        self.assertEqual(self.grid.event_at(QPointF(lane.center().x(), lane.bottom() + 3)), GYM)
        self.click(lane.center())
        self.assertEqual(self.grid.selected_event, slot)
        self.show(AFTERCLASS, GYM, slot)
        self.assertEqual(self.grid.selected_event, slot)
        self.key(Qt.Key.Key_Home)
        seen = [self.grid.selected_event]
        for _ in range(2):
            self.key(Qt.Key.Key_Down)
            seen.append(self.grid.selected_event)
        self.assertEqual(seen, [AFTERCLASS, slot, GYM])
        self.grid.select_event(slot)
        self.assertEqual(self.grid.selected_event, slot)
        self.grid.clearFocus()
        self.grid.grab()

    # (5)
    def test_message_overlay_toggles_and_is_plain_text(self):
        plain = self.grid.grab().toImage()
        self.grid.set_message("未连接")
        self.assertEqual(self.grid.message(), "未连接")
        self.assertNotEqual(self.grid.grab().toImage(), plain)
        self.grid.set_message("<b>载入失败</b>")
        self.assertEqual(self.grid.message(), "<b>载入失败</b>")
        self.grid.grab()
        self.grid.set_message("")
        self.assertEqual(self.grid.grab().toImage(), plain)

    def test_empty_ranges_and_degenerate_events_do_not_crash(self):
        self.grid.set_range([], [], ZONE, now=NOW)
        self.assertEqual(self.grid.event_rects(), [])
        self.assertIsNone(self.grid.event_at(QPoint(300, 300)))
        self.grid.grab()
        self.key(Qt.Key.Key_Down)
        self.key(Qt.Key.Key_Home)
        self.grid.set_range(WEEK, [], ZONE, now=NOW)
        self.assertEqual(self.grid.event_rects(), [])
        self.assertEqual(self.grid.accessibleDescription(), "共 0 个行程")
        long_title = timed("工作", "很长的标题" * 200, at(1, 9), at(1, 9, 5))
        reversed_event = timed("工作", "倒置", at(1, 12), at(1, 11))
        self.grid.set_range(WEEK, [long_title, reversed_event, BLANK], ZONE, now=None)
        self.assertEqual(len(self.grid.event_rects()), 3)
        self.assertGreaterEqual(self.rect_of(long_title).height(), 18)
        self.grid.set_message("没有行程")
        self.grid.grab()

    def test_dst_days_stay_within_24_hours(self):
        spring = date(2026, 3, 8)
        crossing = timed("工作", "跨越", datetime(2026, 3, 8, 1, 30, tzinfo=ZONE), datetime(2026, 3, 8, 3, 30, tzinfo=ZONE))
        self.grid.set_range([spring], [crossing], ZONE, now=datetime(2026, 3, 8, 12, tzinfo=ZONE))
        self.grid.scroll_to_hour(0)
        rect = self.rect_of(crossing)
        self.assertEqual(len(self.grid.event_rects()), 1)
        self.assertLessEqual(rect.bottom(), self.grid._y_of(24 * 60) + 1)
        fall = date(2026, 11, 1)
        late = timed("工作", "深夜", datetime(2026, 11, 1, 23, tzinfo=ZONE), datetime(2026, 11, 2, 1, tzinfo=ZONE))
        fold = timed("工作", "重复小时", datetime(2026, 11, 1, 1, 30, tzinfo=ZONE), datetime(2026, 11, 1, 1, 15, tzinfo=ZONE, fold=1))
        self.grid.set_range([fall, fall + timedelta(days=1)], [late, fold], ZONE, now=datetime(2026, 11, 1, 12, tzinfo=ZONE))
        self.grid.scroll_to_hour(0)
        rects = self.grid.event_rects()
        self.assertEqual(len(rects), 3)
        self.assertTrue(all(r.bottom() <= self.grid._y_of(24 * 60) + 1 for _, r in rects))
        self.assertGreaterEqual(self.rect_of(fold).height(), 18)
        self.grid.grab()

    # (6)
    def test_day_view_fills_the_width(self):
        week_width = self.grid.day_column_rects()[0].width()
        self.grid.set_range(WEEK[1:2], [STANDUP, CLIENT, HOLIDAY], ZONE, now=NOW)
        columns = self.grid.day_column_rects()
        self.assertEqual(len(columns), 1)
        self.assertGreater(columns[0].width(), week_width * 6)
        self.assertGreater(columns[0].right(), self.grid.width() - 20)
        rects = self.grid.event_rects()
        self.assertEqual(len(rects), 1)
        self.assertGreater(rects[0][1].width(), columns[0].width() - 12)
        self.assertEqual(self.grid.all_day_rects(), [])

    # (7)
    def test_render_contains_today_circle_and_now_line(self):
        self.grid.clearFocus()
        pixels = pixel_set(self.grid)
        self.assertGreater(len(pixels), 8)
        self.assertIn(TODAY_RGB, pixels)
        self.assertIn(NOW_RGB, pixels)
        self.assertIn(0xFF000000 | int(calendar_color("工作").lstrip("#"), 16), pixels)

    def test_render_without_today_has_no_today_markers(self):
        self.grid.clearFocus()
        next_week = [SUNDAY + timedelta(days=7 + i) for i in range(7)]
        self.grid.set_range(next_week, [], ZONE, now=NOW)
        pixels = pixel_set(self.grid)
        self.assertNotIn(TODAY_RGB, pixels)
        self.assertNotIn(NOW_RGB, pixels)

    def test_focus_ring_is_drawn_when_focused(self):
        self.grid.activateWindow()
        QTest.qWaitForWindowActive(self.grid)
        self.grid.setFocus()
        self.assertTrue(self.grid.hasFocus())
        next_week = [SUNDAY + timedelta(days=7 + i) for i in range(7)]
        self.grid.set_range(next_week, [], ZONE, now=NOW)
        self.assertIn(TODAY_RGB, pixel_set(self.grid))

    def test_scrolling_and_auto_scroll(self):
        grid = WeekGrid()
        self.addCleanup(grid.deleteLater)
        grid.resize(800, 420)
        grid.show()
        grid.set_range(WEEK, [], ZONE, now=datetime(2026, 11, 1, 3, tzinfo=ZONE))
        self.assertAlmostEqual(grid.scroll_hour(), 7.0, delta=0.05)
        self.assertTrue(grid.findChild(QScrollBar).isVisible())
        late = timed("工作", "晚会", at(1, 15), at(1, 16))
        grid.set_range(WEEK, [late], ZONE, now=datetime(2026, 11, 1, 3, tzinfo=ZONE))
        low, high = grid.visible_hours()
        self.assertTrue(low <= 15 <= high)
        self.assertAlmostEqual(grid.scroll_hour(), 14.5, delta=0.05)
        grid.scroll_to_hour(0)
        self.assertEqual(grid.scroll_hour(), 0)
        angle = QPoint(0, -120)
        wheel = QWheelEvent(QPointF(300, 300), QPointF(300, 300), QPoint(), angle, Qt.MouseButton.NoButton, NO_MOD, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(grid, wheel)
        self.assertGreater(grid.scroll_hour(), 0.5)
        grid.scroll_to_hour(24)
        self.assertEqual(grid.visible_hours()[1], 24.0)
        grid.resize(800, 1400)
        self.assertFalse(grid.findChild(QScrollBar).isVisible())
        self.assertEqual(grid.visible_hours(), (0.0, 24.0))
        grid.set_range(WEEK, [], ZONE, now=NOW)
        self.assertEqual(grid.scroll_hour(), 0)

    def test_auto_scroll_waits_for_the_real_size(self):
        grid = WeekGrid()
        self.addCleanup(grid.deleteLater)
        grid.set_range(WEEK, [], ZONE, now=NOW.replace(hour=16, minute=20))
        grid.resize(800, 420)
        self.assertGreater(grid.scroll_hour(), 12.0)
        grid.resize(900, 1000)
        grid.show()
        self.assertLess(grid.scroll_hour(), 8.0)

    def test_todays_range_scrolls_to_current_hour(self):
        grid = WeekGrid()
        self.addCleanup(grid.deleteLater)
        grid.resize(800, 420)
        grid.show()
        grid.set_range(WEEK, [], ZONE, now=NOW)
        self.assertAlmostEqual(grid.scroll_hour(), 7.0, delta=0.05)
        late = WeekGrid()
        self.addCleanup(late.deleteLater)
        late.resize(800, 420)
        late.show()
        late.set_range(WEEK, [], ZONE, now=NOW.replace(hour=20, minute=30))
        low, high = late.visible_hours()
        self.assertTrue(low <= 20.5 <= high)


class HelperCase(unittest.TestCase):
    def test_calendar_color_is_stable_and_overridable(self):
        first = calendar_color("工作")
        self.assertEqual(first, calendar_color("工作"))
        self.assertIn(first, EVENT_PALETTE)
        self.assertEqual(calendar_color("工作", {"工作": "#123456"}), "#123456")
        self.assertEqual(calendar_color("工作", {"工作": "not-a-color"}), first)
        self.assertEqual(calendar_color("工作", {"其他": "#123456"}), first)
        self.assertGreater(len({calendar_color(f"日历{i}") for i in range(40)}), 4)

    def test_gutter_caption_follows_zone_and_now(self):
        grid = WeekGrid()
        self.addCleanup(grid.deleteLater)
        grid.set_range(WEEK[:1], [], ZONE, now=datetime(2026, 7, 1, 12, tzinfo=ZONE))
        self.assertEqual(grid.gutter_caption(), "GMT-4")
        grid.set_range(WEEK[:1], [], ZoneInfo("Asia/Kolkata"), now=datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Asia/Kolkata")))
        self.assertEqual(grid.gutter_caption(), "GMT+5:30")


if __name__ == "__main__":
    unittest.main()
