from dataclasses import replace
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from availability import has_slot, openings, place_tasks, slot_intervals
from calendar_chat import parse_calendar_query
from calendar_masks import listing, lookup_days, mask_event, plan_events
from calendar_reader import Calendar, CalendarDay, CalendarError, CalendarReader, Event, apply_masks, event_key
from chihaya import MainWindow
from task_chat import Operation, TaskAction, TaskReply, apply_task_reply, approve
from tasks import TaskStore

ZONE = ZoneInfo("America/New_York")
DAY = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, 7, 0, tzinfo=ZONE)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), ZONE)


def event(title: str, start: datetime, end: datetime, *, calendar: str = "Courses", ident: str = "",
          located: bool = False, busy: bool = True) -> Event:
    return Event(calendar, title, start, end, False, busy, ident or title, located)


def snapshot(*events: Event, day: date = DAY) -> CalendarDay:
    return CalendarDay(day, ZONE, (Calendar("primary", "Courses"),), tuple(events), (), NOW, ("primary",))


CLASS = event("Numerical Analysis", at(12, 30), at(13, 45), located=True)
GRIND = event("afternoon grind", at(15, 30), at(16, 30))
MORNING = event("morning grind", at(8), at(9))


class MaskDataTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / "tasks.sqlite3")
        self.directory = Path(directory.name)

    def test_key_is_stable_across_refreshes_and_changes_when_google_moves_the_event(self) -> None:
        again = event("Numerical Analysis", at(12, 30), at(13, 45), located=True)
        self.assertEqual(event_key(CLASS), event_key(again))
        self.assertNotEqual(event_key(CLASS), event_key(replace(CLASS, start=at(12, 45))))
        self.assertNotEqual(event_key(CLASS), event_key(replace(CLASS, id="other")))

    def test_apply_masks_splits_is_repeatable_and_can_be_undone_by_removing_the_key(self) -> None:
        day = snapshot(CLASS, GRIND)
        keys = {event_key(CLASS)}
        masked = apply_masks(day, keys)
        self.assertEqual([item.title for item in masked.events], ["afternoon grind"])
        self.assertEqual([(item.title, item.masked) for item in masked.masked], [("Numerical Analysis", True)])
        self.assertIs(apply_masks(masked, keys), masked)
        restored = apply_masks(masked, set())
        self.assertEqual([item.title for item in restored.events], ["Numerical Analysis", "afternoon grind"])
        self.assertEqual(restored.masked, ())
        self.assertFalse(any(item.masked for item in restored.events))

    def test_store_masks_are_listed_pruned_and_removable(self) -> None:
        stamp = datetime(2026, 11, 26, 9)
        mask_event(self.store, CLASS, reason="teacher is away")
        self.assertEqual(self.store.event_mask_keys(), {event_key(CLASS)})
        item, = self.store.event_masks()
        self.assertEqual((item.title, item.reason), ("Numerical Analysis", "teacher is away"))
        old = event("Old class", at(9, day=DAY - timedelta(days=60)), at(10, day=DAY - timedelta(days=60)))
        self.store.mask_event(event_key(old), old.calendar, old.title, "2026-09-27T14:00:00+00:00",
                              "2026-09-27T15:00:00+00:00", now=stamp)
        self.store.mask_event("later", "c", "t", "2026-11-26T14:00:00+00:00", "2026-11-26T15:00:00+00:00", now=stamp)
        self.assertNotIn(event_key(old), self.store.event_mask_keys())
        self.assertTrue(self.store.unmask_event(event_key(CLASS)))
        self.assertFalse(self.store.unmask_event(event_key(CLASS)))

    def test_reader_applies_masks_to_cached_fresh_and_reconnected_reads(self) -> None:
        reader = CalendarReader(self.directory)
        reader.token.write_text("{}")
        reader.mask_keys = self.store.event_mask_keys
        raw = replace(snapshot(CLASS, GRIND), refreshed=datetime.now(ZONE))
        with patch.object(reader, "_read", return_value=[raw]) as google:
            first = reader.read(DAY, ZONE, selected=("primary",))
            self.assertEqual(len(first.events), 2)
            mask_event(self.store, CLASS)
            cached = reader.read(DAY, ZONE, reuse=True, selected=("primary",))
            refreshed = reader.read(DAY, ZONE, selected=("primary",))
            reconnected = reader.read(DAY, ZONE, connect=True, selected=("primary",))
        self.assertEqual(google.call_count, 3)
        for result in (cached, refreshed, reconnected):
            self.assertEqual([item.title for item in result.events], ["afternoon grind"])
            self.assertEqual([item.title for item in result.masked], ["Numerical Analysis"])

    def test_unreadable_mask_store_does_not_stop_the_calendar(self) -> None:
        reader = CalendarReader(self.directory)
        reader.mask_keys = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        reader.token.write_text("{}")
        with patch.object(reader, "_read", return_value=[snapshot(CLASS)]):
            self.assertEqual(len(reader.read(DAY, ZONE, selected=("primary",)).events), 1)

    def test_listing_gives_the_model_only_what_it_needs_and_flags_cleared_events(self) -> None:
        cleared = apply_masks(snapshot(CLASS, GRIND, MORNING), {event_key(CLASS)})
        payload, index = listing([cleared], ZONE)
        rows = payload["days"][DAY.isoformat()]
        self.assertEqual([row["title"] for row in rows],
                         ["morning grind", "Numerical Analysis", "afternoon grind"])
        self.assertEqual(rows[1], {"id": "e2", "title": "Numerical Analysis", "calendar": "Courses",
                                   "time": "12:30-13:45", "has_place": True, "cleared_locally": True})
        self.assertEqual(set(rows[0]), {"id", "title", "calendar", "time", "has_place"})
        self.assertEqual(index["e2"].title, "Numerical Analysis")

    def test_listing_is_capped_and_lookup_days_are_validated(self) -> None:
        many = snapshot(*(event(f"e{n}", at(0) + timedelta(minutes=n), at(0) + timedelta(minutes=n + 1))
                          for n in range(80)))
        payload, index = listing([many], ZONE)
        self.assertEqual(len(index), 60)
        self.assertIn("note", payload)
        days = lookup_days(["2026-11-27", "nonsense", "2026-11-27", "2027-05-01", "2026-11-26T09:00",
                            "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01"], DAY)
        self.assertEqual(days, [date(2026, 11, 26), date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 29)])


class PlanAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / "tasks.sqlite3")

    def test_a_cleared_event_frees_its_time_and_a_saved_plan_takes_it_again(self) -> None:
        busy = snapshot(CLASS)
        blocked = openings(busy, time(12), time(15), at(7))
        self.assertEqual([(a.hour, a.minute, b.hour, b.minute) for a, b in
                          ((x.astimezone(ZONE), y.astimezone(ZONE)) for x, y in blocked)],
                         [(12, 0, 12, 30), (13, 45, 15, 0)])
        cleared = apply_masks(busy, {event_key(CLASS)})
        self.assertEqual(len(openings(cleared, time(12), time(15), at(7))), 1)
        self.store.add("Coffee chat", "", "60", DAY.isoformat(), today=DAY, start="13:00", end="14:00")
        slots = slot_intervals(self.store.open_tasks(), DAY, ZONE)
        free = openings(cleared, time(12), time(15), at(7), slots)
        self.assertEqual([(a.astimezone(ZONE).strftime("%H:%M"), b.astimezone(ZONE).strftime("%H:%M"))
                          for a, b in free], [("12:00", "13:00"), ("14:00", "15:00")])

    def test_planner_does_not_suggest_a_task_that_already_has_a_slot(self) -> None:
        self.store.add("Coffee chat", "", "60", DAY.isoformat(), today=DAY, start="13:00", end="14:00")
        self.store.add("Reading", "", "30", "", today=DAY)
        tasks = self.store.open_tasks()
        self.assertTrue(has_slot(tasks[0], DAY))
        day = snapshot()
        schedule = place_tasks(day, openings(day, time(8), time(18), at(7)), tasks, at(7), None)
        self.assertEqual([task.title for task, *_ in schedule.placed], ["Reading"])

    def test_plan_events_only_include_open_slotted_tasks_in_range(self) -> None:
        self.store.add("Coffee chat", "", "60", DAY.isoformat(), today=DAY, start="09:00", end="10:00")
        self.store.add("No slot", "", "", DAY.isoformat(), today=DAY)
        self.store.add("Other day", "", "", (DAY + timedelta(days=3)).isoformat(), today=DAY,
                       start="09:00", end="10:00")
        done = self.store.add("Done", "", "", DAY.isoformat(), today=DAY, start="11:00", end="12:00")
        self.store.complete(done)
        plans = plan_events(self.store.list_tasks(), [DAY, DAY + timedelta(days=1)], ZONE)
        self.assertEqual([(item.title, item.local, f"{item.start:%H:%M}-{item.end:%H:%M}") for item in plans],
                         [("Coffee chat", True, "09:00-10:00")])


class ChatMaskTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / "tasks.sqlite3")
        self.events = {"e1": CLASS, "e2": GRIND}

    def reply(self, mode: str, *operations: Operation) -> TaskReply:
        return TaskReply("好的。", "focused", TaskAction(mode, tuple(operations)))

    def test_clearing_events_is_local_validated_per_event_and_undoable(self) -> None:
        outcome = apply_task_reply(
            self.reply("execute", Operation("mask_event", target="e1", notes="teacher away"),
                       Operation("mask_event", target="e9"), Operation("mask_event", target="E2")),
            "今天下午ds301的课老师请假了，那段时间我有其他安排", self.store, NOW.replace(tzinfo=None), events=self.events)
        self.assertTrue(outcome.changed)
        self.assertEqual(self.store.event_mask_keys(), {event_key(CLASS), event_key(GRIND)})
        self.assertIn("Google 日历没有改动", outcome.reply.reply)
        self.assertIn("没有找到这个行程", outcome.reply.reply)
        self.assertEqual(self.store.event_masks()[0].reason, "teacher away")
        again = apply_task_reply(self.reply("execute", Operation("mask_event", target="e1")), "再清一次",
                                 self.store, NOW.replace(tzinfo=None), events=self.events)
        self.assertFalse(again.changed)
        self.assertIn("已经在本机取消", again.reply.reply)
        assert outcome.undo is not None
        outcome.undo.run(self.store)
        self.assertEqual(self.store.event_mask_keys(), set())

    def test_a_guess_is_staged_then_approval_writes_exactly_those_events(self) -> None:
        proposed = apply_task_reply(
            self.reply("propose", Operation("mask_event", target="e1"), Operation("mask_event", target="e2")),
            "今天我没有课，帮我把课程表清理一下", self.store, NOW.replace(tzinfo=None), events=self.events)
        self.assertEqual(self.store.event_mask_keys(), set())
        assert proposed.proposal is not None
        self.assertIn("在本机取消 12:30–13:45 Numerical Analysis", proposed.reply.reply)
        waiting = [op.for_model() for op in proposed.proposal.operations]
        self.assertEqual(waiting[0]["event"], "12:30–13:45 Numerical Analysis")
        self.assertNotIn("target", waiting[0])
        approved = approve(proposed.proposal, self.store, NOW.replace(tzinfo=None), True)
        self.assertTrue(approved.changed)
        self.assertEqual(len(self.store.event_mask_keys()), 2)

    def test_unmask_brings_an_event_back_and_refuses_events_that_were_never_cleared(self) -> None:
        mask_event(self.store, CLASS)
        done = apply_task_reply(self.reply("execute", Operation("unmask_event", target="e1"),
                                           Operation("unmask_event", target="e2")),
                                "恢复", self.store, NOW.replace(tzinfo=None), events=self.events)
        self.assertEqual(self.store.event_mask_keys(), set())
        self.assertIn("没有被取消", done.reply.reply)
        assert done.undo is not None
        done.undo.run(self.store)
        self.assertEqual(self.store.event_mask_keys(), {event_key(CLASS)})

    def test_a_lookup_that_reaches_the_app_never_writes(self) -> None:
        outcome = apply_task_reply(self.reply("lookup", Operation("lookup_events", date="2026-11-26")),
                                   "今天没有课", self.store, NOW.replace(tzinfo=None))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.store.event_mask_keys(), set())

    def test_the_new_plan_after_clearing_is_a_normal_slot_task(self) -> None:
        mask_event(self.store, GRIND)
        apply_task_reply(
            self.reply("execute", Operation("add", title="Coffee chat", date="2026-11-26", field="planned_day",
                                            start="15:30", end="16:30")),
            "那段时间我要 coffee chat", self.store, NOW.replace(tzinfo=None))
        task, = self.store.open_tasks()
        self.assertEqual((task.planned_day, task.start, task.end), ("2026-11-26", "15:30", "16:30"))

    def test_calendar_router_leaves_mask_requests_to_the_model(self) -> None:
        for message in ("今天我没有课，帮我把课程表清理一下，我有其他安排",
                        "今天下午ds301的课老师请假了，那段时间我有其他安排",
                        "明天早上的grind要因为coffeechat修改，你先帮我把这块schedule清理掉，然后我再进行新的schedule"):
            with self.subTest(message=message):
                self.assertIsNone(parse_calendar_query(message, connected=True, today=DAY))
        self.assertIsNotNone(parse_calendar_query("下午两点到四点，帮我排一下投履历和复习", connected=True, today=DAY))
        self.assertIsNotNone(parse_calendar_query("Plan my day from 10:00 to 15:00", connected=True, today=DAY))


class WindowMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        (self.folder / ".env").write_text("ANTHROPIC_API_KEY=fixture-only\n")
        self.window = MainWindow(self.folder / "portraits")
        self.addCleanup(self.window.quit_app)
        self.window.show()
        self.zone = self.window.calendar.zone
        self.day = datetime.now(self.zone).date() + timedelta(days=1)

    def stamp(self, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(self.day, time(hour, minute), self.zone)

    def wait_for_request(self) -> None:
        for _ in range(300):
            if self.window.request is None:
                return
            QTest.qWait(20)
        self.fail("request did not finish")

    def show_week(self, *events: Event) -> None:
        window = self.window
        window.calendar.day.setDate(QDate(self.day.year, self.day.month, self.day.day))
        window.calendar.snapshot = CalendarDay(self.day, self.zone, (), events, (), datetime.now(self.zone))
        window.calendar.week = {self.day: window.calendar.snapshot}
        window.calendar.render()

    def test_two_step_lookup_clears_the_events_the_model_picked_and_keeps_details_off_the_wire(self) -> None:
        window = self.window
        window.mode.setCurrentText("Claude")
        klass = event("Numerical Analysis", self.stamp(12, 30), self.stamp(13, 45), located=True)
        grind = event("afternoon grind", self.stamp(15, 30), self.stamp(16, 30))
        calls = []

        def fake(folder, message, history, **kwargs):
            calls.append(kwargs)
            context = json.loads(kwargs["task_context"])
            if "calendar_events" not in context:
                return TaskReply("我先看一下明天的课表。", "focused", TaskAction(
                    "lookup", (Operation("lookup_events", date=self.day.isoformat()),)))
            rows = context["calendar_events"]["days"][self.day.isoformat()]
            target = next(row["id"] for row in rows if row["has_place"])
            return TaskReply("好，课先清掉。", "smile", TaskAction(
                "execute", (Operation("mask_event", target=target, notes="没有课"),)))

        with patch("chihaya.request_reply", side_effect=fake), patch.object(
            window.calendar.reader, "read", return_value=CalendarDay(
                self.day, self.zone, (), (klass, grind), (), datetime.now(self.zone))) as read:
            window.entry.setText("明天我没有课，帮我把课程表清理一下，我有其他安排")
            window.send()
            self.wait_for_request()
        read.assert_called_once()
        self.assertNotIn("calendar_events", json.loads(calls[0]["task_context"]))
        shown = json.loads(calls[1]["task_context"])["calendar_events"]
        self.assertEqual([row["title"] for row in shown["days"][self.day.isoformat()]],
                         ["Numerical Analysis", "afternoon grind"])
        self.assertEqual(window.store.event_mask_keys(), {event_key(klass)})
        self.assertIn("Google 日历没有改动", window.history.toPlainText())
        self.assertIsNotNone(window.last_undo)
        self.assertEqual(window.claude_history[-1][0], "明天我没有课，帮我把课程表清理一下，我有其他安排")
        self.assertNotIn("calendar_events", window.claude_history[-1][1])

    def test_lookup_without_a_calendar_tells_the_model_it_is_unavailable(self) -> None:
        window = self.window
        window.mode.setCurrentText("Claude")
        contexts = []

        def fake(folder, message, history, **kwargs):
            contexts.append(json.loads(kwargs["task_context"]))
            if len(contexts) == 1:
                return TaskReply("看一下。", "focused", TaskAction(
                    "lookup", (Operation("lookup_events", date=self.day.isoformat()),)))
            return TaskReply("我现在看不到日历。", "focused", TaskAction("lookup", (
                Operation("lookup_events", date=self.day.isoformat()),)))

        with patch("chihaya.request_reply", side_effect=fake), patch.object(
                window.calendar.reader, "read", side_effect=CalendarError("尚未连接 Google 日历。")):
            window.entry.setText("明天早上的 grind 取消")
            window.send()
            self.wait_for_request()
        self.assertEqual(contexts[1]["calendar_events"], {"unavailable": "尚未连接 Google 日历。"})
        self.assertEqual(len(contexts), 2)
        self.assertIn("我现在看不到日历。", window.history.toPlainText())
        self.assertEqual(window.store.event_mask_keys(), set())

    def test_calendar_page_dims_a_cleared_class_and_draws_the_new_plan_in_a_narrow_lane(self) -> None:
        window = self.window
        klass = event("Numerical Analysis", self.stamp(12, 30), self.stamp(13, 45), located=True)
        grind = event("afternoon grind", self.stamp(15, 30), self.stamp(16, 30))
        self.show_week(klass, grind)
        calendar = window.calendar
        calendar.grid.select_event(klass)
        self.assertEqual(calendar.mask_button.text(), "在本机取消这个行程")
        calendar.mask_button.click()
        self.assertEqual(window.store.event_mask_keys(), {event_key(klass)})
        self.assertEqual(calendar.mask_button.text(), "恢复这个行程")
        self.assertIn("已在本机取消", calendar.detail_text.text())
        layout = {item["event"].title: item for item in calendar.grid.describe_layout()}
        self.assertTrue(layout["Numerical Analysis"]["masked"])
        self.assertFalse(layout["afternoon grind"]["masked"])
        self.assertEqual([item.title for item in calendar.week[self.day].events], ["afternoon grind"])

        window.store.add("Coffee chat", "", "60", self.day.isoformat(), today=self.day, start="15:45", end="16:45")
        window.refresh_task_views()
        layout = {item["event"].title: item for item in calendar.grid.describe_layout()}
        self.assertTrue(layout["Coffee chat"]["overlay"] and layout["Coffee chat"]["local"])
        self.assertTrue(layout["afternoon grind"]["dim"])
        self.assertNotIn("Coffee chat", [item.title for item in calendar.week[self.day].events])

        calendar.grid.select_event(klass)
        calendar.mask_button.click()
        self.assertEqual(window.store.event_mask_keys(), set())
        self.assertEqual(len(calendar.week[self.day].events), 2)

    def test_masks_survive_a_refresh_and_restore_all_in_settings(self) -> None:
        window = self.window
        klass = event("Numerical Analysis", self.stamp(12, 30), self.stamp(13, 45), located=True)
        mask_event(window.store, klass)
        window.apply_event_masks()
        self.assertIn("1 个", window.masked_summary.text())
        self.assertTrue(window.restore_masks_button.isEnabled())
        raw = CalendarDay(self.day, self.zone, (), (klass,), (), datetime.now(self.zone))
        with patch.object(window.calendar.reader, "_read", return_value=[raw]):
            window.calendar.reader.token.write_text("{}")
            result = window.calendar.reader.read(self.day, self.zone, connect=True, selected=("primary",))
        self.assertEqual((len(result.events), len(result.masked)), (0, 1))
        window.restore_masks_button.click()
        self.assertEqual(window.store.event_mask_keys(), set())
        self.assertFalse(window.restore_masks_button.isEnabled())

    def test_saved_plans_and_the_mask_count_show_up_at_startup(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        seeded = TaskStore(folder / "tasks.sqlite3")
        today = datetime.now(self.zone).date()
        seeded.add("Coffee chat", "", "60", today.isoformat(), today=today, start="09:00", end="10:00")
        mask_event(seeded, CLASS)
        window = MainWindow(folder / "portraits")
        self.addCleanup(window.quit_app)
        window.show()
        self.assertEqual([item["event"].title for item in window.calendar.grid.describe_layout()], ["Coffee chat"])
        self.assertIn("1 个", window.masked_summary.text())
        self.assertTrue(window.restore_masks_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
