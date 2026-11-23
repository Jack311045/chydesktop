from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

from calendar_reader import CalendarDay, day_bounds
from tasks import Task, deadline_order, ordered_tasks, task_line

Interval = tuple[datetime, datetime]


def window_end(day: date, end: time, zone: ZoneInfo) -> datetime:
    return day_bounds(day, zone)[1] if end == time.min else datetime.combine(day, end, zone)


def openings(snapshot: CalendarDay, start: time, end: time, now: datetime) -> list[Interval]:
    if snapshot.failures:
        return []
    day_start, _ = day_bounds(snapshot.day, snapshot.zone)
    current = now.astimezone(UTC)
    rounded = current.replace(second=0, microsecond=0)
    if rounded < current:
        rounded += timedelta(minutes=1)
    lower = max(datetime.combine(snapshot.day, start, snapshot.zone).astimezone(UTC), rounded)
    upper = window_end(snapshot.day, end, snapshot.zone).astimezone(UTC)
    if lower >= upper:
        return []
    busy = sorted((max(lower, event.start.astimezone(UTC)), min(upper, event.end.astimezone(UTC)))
                  for event in snapshot.events if event.busy)
    free = []
    cursor = max(lower, day_start.astimezone(UTC))
    for left, right in busy:
        if right <= left:
            continue
        if left > cursor:
            free.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < upper:
        free.append((cursor, upper))
    return free


@dataclass
class Schedule:
    placed: list[tuple[Task, datetime, datetime, bool]]
    unscheduled: list[Task]
    unestimated: list[Task]
    used: int


def place_tasks(snapshot: CalendarDay, intervals: Sequence[Interval], tasks: Sequence[Task],
                now: datetime, budget: int | None) -> Schedule:
    remaining = list(intervals)
    result = Schedule([], [], [], 0)
    local_now = now.astimezone(snapshot.zone).replace(tzinfo=None)
    for task in ordered_tasks(tasks, snapshot.day, local_now):
        if task.minutes is None:
            result.unestimated.append(task)
            continue
        overdue = deadline_order(task) < local_now
        deadline = None if overdue or not task.deadline or len(task.deadline) == 10 else (
            deadline_order(task).replace(tzinfo=snapshot.zone).astimezone(UTC))
        for index, (left, right) in enumerate(remaining):
            finish = left + timedelta(minutes=task.minutes)
            if (finish <= right and (deadline is None or finish <= deadline)
                    and (budget is None or result.used + task.minutes <= budget)):
                result.placed.append((task, left, finish, overdue))
                remaining[index] = (finish, right)
                result.used += task.minutes
                break
        else:
            result.unscheduled.append(task)
    return result


def availability_text(snapshot: CalendarDay, free: Sequence[Interval], schedule: Schedule | None = None,
                      *, chinese: bool = False, include_task_details: bool = True,
                      window_ended: bool = False) -> str:
    def clock(stamp: datetime) -> str:
        local = stamp.astimezone(snapshot.zone)
        return local.strftime("%H:%M:%S %Z" if local.second else "%H:%M %Z")

    def span(left: datetime, right: datetime) -> str:
        return f"{clock(left)}–{clock(right)}"

    def task_display(task: Task, overdue: bool) -> str:
        label = task_line(task) if include_task_details else f"#{task.id} ({task.minutes if task.minutes is not None else '?'} min)"
        return label + ((" [已逾期]" if chinese else " [overdue]") if overdue else "")

    none = "無" if chinese else "None"
    lines = [f"{snapshot.day} ({snapshot.zone.key})"]
    if not snapshot.events:
        lines.append("這天沒有行事曆事件。" if chinese else "No calendar events on this date.")
    if window_ended:
        lines.append("這個時段已結束。" if chinese else "This window has ended.")
    elif not free and snapshot.events:
        lines.append("這個時段已排滿。" if chinese else "This window is fully booked.")
    lines.append("可用空檔：" if chinese else "Usable openings:")
    lines.extend([span(left, right) for left, right in free] or [none])
    if schedule is not None:
        lines.append(f"\n已安排 {schedule.used} 分鐘：" if chinese else f"\nAllocated {schedule.used} minutes:")
        lines.extend([f"{span(left, right)}  {task_display(task, overdue)}"
                      for task, left, right, overdue in schedule.placed] or [none])
        local_refresh = snapshot.refreshed.astimezone(snapshot.zone).replace(tzinfo=None)
        for label, tasks in (
            ("排不下：" if chinese else "Does not fit:", schedule.unscheduled),
            ("待估時：" if chinese else "Needs an estimate:", schedule.unestimated),
        ):
            lines.append("\n" + label)
            lines.extend([task_display(task, deadline_order(task) < local_refresh) for task in tasks] or [none])
    return "\n".join(lines)
