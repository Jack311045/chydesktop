from datetime import datetime, time, UTC

from availability import openings, place_tasks
from calendar_reader import CalendarDay
from daily_brief import word
from tasks import Task, TaskStore, deadline_order, task_line


def collect_reminders(store: TaskStore, now: datetime) -> int:
    fired = 0
    local = now.replace(tzinfo=None)
    for task in store.open_tasks():
        moments = []
        if task.deadline:
            deadline = deadline_order(task) if len(task.deadline) > 10 else datetime.combine(
                datetime.fromisoformat(task.deadline).date(), time(9))
            moments.append(("deadline", deadline))
        if task.planned_day:
            moments.append(("planned", datetime.combine(datetime.fromisoformat(task.planned_day).date(), time(9))))
        for kind, stamp in moments:
            if stamp.date() == local.date() and stamp <= local:
                fired += int(store.claim_reminder(task, kind, stamp.isoformat(timespec="seconds")))
    return fired


def briefing_text(now: datetime, tasks: list[Task], snapshot: CalendarDay | None, status: str,
                  start: time, end: time, budget: int | None, *, lang: str = "en") -> str:
    day = now.date().isoformat()
    text = [f"{word('brief', lang)} — {now:%Y-%m-%d %Z}"]
    due = sorted((task for task in tasks if (task.deadline and task.deadline[:10] <= day) or task.planned_day == day),
                 key=lambda task: (deadline_order(task), task.id))
    local_now = now.replace(tzinfo=None)
    for task in due[:3]:
        labels = []
        if task.deadline and task.deadline[:10] <= day:
            labels.append(word("overdue" if deadline_order(task) < local_now else "due", lang))
        if task.planned_day == day:
            labels.append(word("planned", lang))
        text.append(f"{' / '.join(labels)}: {task_line(task)}")
    if not due:
        text.append(word("no_tasks", lang))
    if len(due) > 3:
        text.append(word("more_tasks", lang).format(count=len(due) - 3))
    if snapshot is None or snapshot.failures or snapshot.day != now.date():
        return "\n".join([*text, status])
    utc_now = now.astimezone(UTC)
    upcoming = [event for event in snapshot.events if event.end.astimezone(UTC) > utc_now]
    if upcoming:
        event = min(upcoming, key=lambda item: item.start.astimezone(UTC))
        start_label = word("all_day", lang) if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"
        text.append(f"{word('event', lang)}: {start_label} {event.title} ({event.calendar})")
    else:
        text.append(word("no_event" if snapshot.events else "empty_calendar", lang))
    free = openings(snapshot, start, end, now)
    if free:
        left, right = (value.astimezone(snapshot.zone) for value in free[0])
        text.append(f"{word('opening', lang)}: {left:%H:%M}–{right:%H:%M}")
    else:
        text.append(word("no_opening", lang))
    plan = place_tasks(snapshot, free, tasks, now, budget)
    if plan.placed:
        task, left, right, _ = min(plan.placed, key=lambda item: item[1])
        text.append(f"{word('fits', lang)}: {left.astimezone(snapshot.zone):%H:%M}–"
                    f"{right.astimezone(snapshot.zone):%H:%M} {task.title}")
    else:
        text.append(word("no_fit", lang))
    return "\n".join(text)
