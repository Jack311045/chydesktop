from datetime import date, datetime, time, timedelta, UTC
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from availability import openings, place_tasks
from calendar_chat import calendar_answer, parse_calendar_query
from calendar_reader import CalendarDay, CalendarError, CalendarReader, Event
from tasks import TaskStore


def response(items, token=None, status=200):
    return Mock(status_code=status, json=Mock(return_value={"items": items, "nextPageToken": token}))


class CalendarTests(TestCase):
    def test_read_paged_dst_day_with_recurring_cancelled_and_all_day_events(self):
        session = Mock()
        timed = {"summary": "Recurring instance", "recurringEventId": "series",
                 "start": {"dateTime": "2026-03-08T10:00:00-04:00"},
                 "end": {"dateTime": "2026-03-08T11:00:00-04:00"}}
        all_day = {"summary": "Free all-day", "start": {"date": "2026-03-08"},
                   "end": {"date": "2026-03-09"}, "transparency": "transparent"}
        session.get.side_effect = [
            response([{"id": "a", "summary": "A", "primary": True}], "calendar-page"),
            response([{"id": "b", "summary": "B"}]),
            response([timed, {"status": "cancelled"}], "events-page"), response([all_day]),
            response([], status=503),
        ]
        day = date(2026, 3, 8)
        zone = ZoneInfo("America/New_York")
        snapshot = CalendarReader.read_day(session, day, zone, ("primary", "b"))
        self.assertEqual([event.title for event in snapshot.events], ["Free all-day", "Recurring instance"])
        self.assertEqual(snapshot.events[0].end.date(), date(2026, 3, 9))
        self.assertFalse(snapshot.events[0].busy)
        self.assertTrue(snapshot.events[1].busy)
        self.assertEqual(snapshot.failures, ("B",))
        self.assertEqual(openings(snapshot, time(9), time(12), datetime(2026, 3, 8, 9, tzinfo=zone)), [])
        calls = session.get.call_args_list
        self.assertEqual(calls[1].kwargs["params"]["pageToken"], "calendar-page")
        self.assertEqual(calls[3].kwargs["params"]["pageToken"], "events-page")
        params = calls[2].kwargs["params"]
        self.assertEqual(params["timeMin"], "2026-03-08T00:00:00-05:00")
        self.assertEqual(params["timeMax"], "2026-03-09T00:00:00-04:00")
        self.assertEqual(params["singleEvents"], "true")
        for field in ("description", "attendees", "conferenceData", "hangoutLink"):
            self.assertNotIn(field, params["fields"])
        self.assertTrue(all(call[0] == "get" for call in session.method_calls))

    def test_week_read_uses_one_query_per_calendar_and_splits_events_by_day(self):
        session = Mock()
        zone = ZoneInfo("America/New_York")
        overnight = {"summary": "Night shift", "start": {"dateTime": "2026-11-23T22:00:00-05:00"},
                     "end": {"dateTime": "2026-11-24T02:00:00-05:00"}}
        tuesday = {"summary": "Tuesday class", "start": {"dateTime": "2026-11-24T14:00:00-05:00"},
                   "end": {"dateTime": "2026-11-24T15:15:00-05:00"}}
        session.get.side_effect = [response([{"id": "a", "summary": "A", "primary": True}]),
                                   response([overnight, tuesday])]
        days = [date(2026, 11, 22) + timedelta(days=offset) for offset in range(7)]
        snapshots = CalendarReader.read_span(session, days, zone, ("primary",))
        self.assertEqual(session.get.call_count, 2)
        params = session.get.call_args_list[1].kwargs["params"]
        self.assertEqual((params["timeMin"], params["timeMax"]),
                         ("2026-11-22T00:00:00-05:00", "2026-11-29T00:00:00-05:00"))
        titles = {item.day: [event.title for event in item.events] for item in snapshots}
        self.assertEqual(titles[date(2026, 11, 22)], [])
        self.assertEqual(titles[date(2026, 11, 23)], ["Night shift"])
        self.assertEqual(titles[date(2026, 11, 24)], ["Night shift", "Tuesday class"])
        self.assertEqual(titles[date(2026, 11, 25)], [])
        with self.assertRaises(CalendarError):
            CalendarReader(Path("unused")).read_days([days[0], days[2]], zone)

    def test_contiguous_placement_and_database_remains_intact(self):
        zone = ZoneInfo("America/New_York")
        day = date(2026, 11, 20)

        def stamp(hour, minute=0):
            return datetime.combine(day, time(hour, minute), zone)

        snapshot = CalendarDay(day, zone, (), (
            Event("A", "Private A", stamp(14, 30), stamp(15, 15), False, True),
            Event("B", "Private B", stamp(15), stamp(15, 30), False, True),
            Event("B", "Free", stamp(14), stamp(16), False, False),
        ), (), stamp(13))
        free = openings(snapshot, time(14), time(16), stamp(13))
        expected = [(stamp(14), stamp(14, 30)), (stamp(15, 30), stamp(16))]
        self.assertEqual(free, [(a.astimezone(UTC), b.astimezone(UTC)) for a, b in expected])
        with TemporaryDirectory() as folder:
            store = TaskStore(Path(folder) / "tasks.sqlite3")
            store.add("Long task", "today 16:00", "45", "", today=day)
            store.add("Short task", "today 16:10", "30", "", today=day)
            original = store.open_tasks()
            result = place_tasks(snapshot, free, original, stamp(13), 60)
            self.assertEqual([entry[0].title for entry in result.placed], ["Short task"])
            self.assertEqual(result.placed[0][1:3], free[0])
            self.assertEqual(result.unscheduled, [original[0]])
            self.assertEqual(result.used, 30)
            self.assertEqual(store.open_tasks(), original)
            store.add("Deadline too early", "today 14:20", "25", "", today=day)
            result = place_tasks(snapshot, free, store.open_tasks(), stamp(13), 25)
            self.assertEqual(result.used, 0)
        blocked = CalendarDay(day, zone, (), (
            Event("A", "Busy all-day", stamp(0), datetime(2026, 11, 21, tzinfo=zone), True, True),
        ), (), stamp(13))
        self.assertEqual(openings(blocked, time(14), time(16), stamp(13)), [])

    def test_chat_uses_only_openings_and_falls_back_on_incomplete_read(self):
        today = datetime.now(UTC).date()
        query = parse_calendar_query("下午兩點到四點，幫我排一下投履歷和複習", connected=True, today=today)
        assert query is not None
        self.assertEqual((query.start, query.end, query.plan), (time(14), time(16), True))
        self.assertIsNone(parse_calendar_query("Move the planned day for #1 to tomorrow",
                                                connected=True, today=today))
        with TemporaryDirectory() as folder:
            reader = CalendarReader(Path(folder))
            zone = ZoneInfo("UTC")
            day = datetime.now(zone).date()
            reader.save_selection(("primary", "fake-calendar"))
            self.assertEqual(CalendarReader(Path(folder)).selected(), ("primary", "fake-calendar"))
            snapshot = CalendarDay(day, zone, (), (
                Event("Private calendar", "Private event name", datetime.combine(day, time(14), zone),
                      datetime.combine(day, time(15), zone), False, True),
            ), (), datetime.now(zone))
            store = TaskStore(Path(folder) / "tasks.sqlite3")
            store.add("Private task title", "", "30", "", today=day)
            with patch.object(reader, "read", return_value=snapshot):
                summary, model_summary = calendar_answer(query, reader, zone, store)
                self.assertIsNotNone(model_summary)
                self.assertNotIn("Private event name", summary)
                self.assertNotIn("Private calendar", model_summary or "")
                self.assertNotIn("Private task title", model_summary or "")
            fallback = parse_calendar_query("Plan today: 60 minutes", connected=True, today=today)
            assert fallback is not None
            with patch.object(reader, "read", return_value=CalendarDay(
                    day, zone, (), (), ("failed",), datetime.now(zone))):
                summary, model_summary = calendar_answer(fallback, reader, zone, store)
                self.assertIsNone(model_summary)
                self.assertIn("Fits 30 / 60 minutes", summary)

    def test_reported_prompts_share_local_date_window_and_cached_calendars(self):
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 22, 22, tzinfo=zone)  # UTC is already November 23.
        day = date(2026, 11, 23)
        with TemporaryDirectory() as folder:
            reader = CalendarReader(Path(folder))
            reader.token.touch()
            reader.save_selection(("primary", "secondary"))
            start = datetime(2026, 11, 23, 15, tzinfo=zone)
            event = Event("Private calendar", "Private meeting", start, start + timedelta(minutes=30), False, True)
            snapshot = CalendarDay(day, zone, (), (event,), (), datetime.now(zone))
            with patch.object(reader, "_read", return_value=[snapshot]) as fetch:
                reader.read(day, zone)
                for prompt in ("明天我什麼時候有空", "11/23我什麼時候有空", "2026-11-23我什麼時候有空"):
                    with self.subTest(prompt=prompt):
                        query = parse_calendar_query(prompt, connected=True, today=now.date(), window=("14:00", "16:00"))
                        assert query is not None
                        self.assertEqual(query.day, day)
                        answer, model = calendar_answer(query, reader, zone, None, now=now)
                        self.assertIn("14:00 EST–15:00 EST", answer)
                        self.assertIn("15:30 EST–16:00 EST", model or "")
                        self.assertNotIn("Private", model or "")
                fetch.assert_called_once_with([day], zone, False, ("primary", "secondary"))
                reader.save_selection(("primary",))
                reader.read(day, zone, reuse=True)
                fetch.assert_called_with([day], zone, False, ("primary",))
        override = parse_calendar_query("11/23 13:00到14:00有空檔嗎", connected=True,
                                        today=now.date(), window=("14:00", "16:00"))
        assert override is not None
        self.assertEqual((override.start, override.end), (time(13), time(14)))

    def test_empty_booked_and_failed_are_distinct(self):
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 22, 13, tzinfo=zone)
        query = parse_calendar_query("When am I free?", connected=True, today=now.date(),
                                     window=("14:00", "16:00"))
        assert query is not None
        event = Event("A", "Busy", now.replace(hour=14), now.replace(hour=16), False, True)
        reader = Mock(spec=CalendarReader)
        for events, failures, text in (
            ((), (), "No calendar events on this date."),
            ((event,), (), "This window is fully booked."),
            ((), ("A",), "Complete calendar availability is unavailable."),
        ):
            reader.read.return_value = CalendarDay(now.date(), zone, (), events, failures, now)
            answer, model = calendar_answer(query, reader, zone, None, now=now)
            self.assertIn(text, answer)
            self.assertEqual(model is None, bool(failures))
            self.assertEqual("manual plan" in answer, bool(failures))
