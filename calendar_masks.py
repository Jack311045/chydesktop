"""Local-only changes on top of the read-only Google Calendar.

A *mask* hides one calendar instance on this computer (a class that was cancelled, a block the producer
replaced). A *plan* is a saved task slot drawn next to the real events. Neither is ever written to Google
Calendar, and both are applied to every snapshot, so a refresh or a new login cannot bring a cleared
event back.
"""
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from calendar_reader import CalendarDay, Event, event_key
from tasks import Task, TaskError, TaskStore

PLAN_CALENDAR = "本机待办"
MAX_LISTED_EVENTS = 60
MAX_LOOKUP_DAYS = 4


def plan_events(tasks: Sequence[Task], days: Sequence[date], zone: ZoneInfo) -> list[Event]:
    """Open tasks with a saved time slot on one of `days`, as local events for the calendar grid."""
    wanted = {day.isoformat(): day for day in days}
    plans = []
    for task in tasks:
        day = wanted.get(task.planned_day or "")
        if task.status != "open" or day is None or not (task.start and task.end):
            continue
        plans.append(Event(PLAN_CALENDAR, task.title, datetime.combine(day, time.fromisoformat(task.start), zone),
                           datetime.combine(day, time.fromisoformat(task.end), zone), False, True,
                           f"task:{task.id}", local=True))
    return plans


def mask_event(store: TaskStore, event: Event, *, reason: str = "") -> str:
    key = event_key(event)
    store.mask_event(key, event.calendar, event.title, event.start.astimezone(UTC).isoformat(timespec="seconds"),
                     event.end.astimezone(UTC).isoformat(timespec="seconds"), reason=reason)
    return key


def event_line(event: Event) -> str:
    """Local wall-clock text; event times are already converted to the app's time zone by the reader."""
    when = "全天" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"
    return f"{when} {event.title}"


def listing(snapshots: Sequence[CalendarDay], zone: ZoneInfo) -> tuple[dict[str, object], dict[str, Event]]:
    """The minimum the model needs to point at events: id, title, time, calendar name and whether a place
    is set. Descriptions, attendees, links and addresses are never read, let alone sent.

    Returns the JSON-ready payload and the id → event index the app uses to validate what the model picks.
    """
    index: dict[str, Event] = {}
    days: dict[str, list[dict[str, object]]] = {}
    truncated = False
    for snapshot in snapshots:
        rows = days.setdefault(snapshot.day.isoformat(), [])
        for event in sorted((*snapshot.events, *snapshot.masked), key=lambda item: item.start.astimezone(UTC)):
            if len(index) >= MAX_LISTED_EVENTS:
                truncated = True
                break
            ident = f"e{len(index) + 1}"
            index[ident] = event
            local = event.start.astimezone(zone), event.end.astimezone(zone)
            row: dict[str, object] = {
                "id": ident, "title": " ".join(event.title.split())[:80], "calendar": event.calendar[:40],
                "time": "all day" if event.all_day else f"{local[0]:%H:%M}-{local[1]:%H:%M}",
                "has_place": event.located}
            if event.masked:
                row["cleared_locally"] = True
            if not event.busy:
                row["shows_as_free"] = True
            rows.append(row)
    payload: dict[str, object] = {"days": days}
    if truncated:
        payload["note"] = f"Only the first {MAX_LISTED_EVENTS} events are listed."
    return payload, index


def lookup_days(dates: Sequence[str], today: date) -> list[date]:
    """Valid, de-duplicated dates the model asked about (at most MAX_LOOKUP_DAYS, near today)."""
    days: list[date] = []
    for value in dates:
        try:
            day = date.fromisoformat(value.strip()[:10])
        except ValueError:
            continue
        if abs((day - today).days) <= 31 and day not in days:
            days.append(day)
    return sorted(days)[:MAX_LOOKUP_DAYS]


def require_event(index: dict[str, Event] | None, target: str, chinese: bool) -> Event:
    event = (index or {}).get(target.strip().lower())
    if event is None:
        raise TaskError("没有找到这个行程，请再说明是哪一个。" if chinese else
                        "I could not find that calendar event. Which one do you mean?")
    return event
