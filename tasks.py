from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re
import sqlite3


class TaskError(Exception):
    """A task error safe to show in the UI."""


def local_date(value: str, today: date) -> date:
    text = value.strip().casefold()
    relative = {"today": 0, "tomorrow": 1, "今天": 0, "明天": 1}
    if text in relative:
        return today + timedelta(days=relative[text])
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    chinese = re.fullmatch(r"(这|本|下)?(?:周|星期)([一二三四五六日天])", text)
    if chinese:
        weekday = "一二三四五六日天".index(chinese[2]) % 7
        offset = weekday - today.weekday()
        if chinese[1] == "下":
            offset += 7
        else:
            offset %= 7
        return today + timedelta(days=offset)
    if text in weekdays:
        return today + timedelta(days=(weekdays.index(text) - today.weekday()) % 7)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise TaskError("Enter a valid date (YYYY-MM-DD, today, tomorrow, or a weekday).")


def deadline_value(value: str, today: date) -> str | None:
    text = value.strip()
    if not text:
        return None
    timed = re.fullmatch(r"(.+?)[ T](\d{2}:\d{2}(?::\d{2})?)", text)
    if timed:
        day = local_date(timed[1], today)
        try:
            clock = time.fromisoformat(timed[2])
        except ValueError:
            raise TaskError("Enter a valid local deadline time (HH:MM or HH:MM:SS).") from None
        return f"{day} {clock.isoformat(timespec='seconds' if len(timed[2]) == 8 else 'minutes')}"
    return local_date(text, today).isoformat()


def minutes_value(value: str, label: str, *, optional: bool = False, minimum: int = 1) -> int | None:
    text = value.strip()
    if not text and optional:
        return None
    if not re.fullmatch(r"[0-9]{1,9}", text) or int(text) < minimum:
        raise TaskError(f"{label} must be a {'nonnegative' if minimum == 0 else 'positive'} whole number of minutes.")
    return int(text)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    deadline: str | None
    minutes: int | None
    planned_day: str | None
    mail_linked: bool = False


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise TaskError("Could not create the local task folder.") from None
        with self.transaction("initialize") as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
                    deadline TEXT,
                    minutes INTEGER CHECK(minutes > 0),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'complete')),
                    planned_day TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS mail_task_sources (
                    source TEXT PRIMARY KEY, task_id INTEGER NOT NULL UNIQUE
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS fired_reminders (
                    task_id INTEGER NOT NULL, kind TEXT NOT NULL, scheduled TEXT NOT NULL,
                    PRIMARY KEY (task_id, kind, scheduled)
                )
            """)

    @contextmanager
    def transaction(self, operation: str) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, timeout=0.25)) as db, db:
                yield db
        except sqlite3.Error:
            raise TaskError(f"Could not {operation} tasks in the local database. Check access and try again.") from None

    def open_tasks(self) -> list[Task]:
        with self.transaction("read") as db:
            return [Task(*row[:5], bool(row[5])) for row in db.execute(
                "SELECT t.id, title, deadline, minutes, planned_day, m.task_id IS NOT NULL "
                "FROM tasks t LEFT JOIN mail_task_sources m ON m.task_id = t.id WHERE status = ? ORDER BY t.id",
                ("open",),
            )]

    def add_mail_action(self, source: str, title: str, deadline: str, *, today: date) -> tuple[int, bool]:
        with self.transaction("save") as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT task_id FROM mail_task_sources WHERE source = ?", (source,)).fetchone()
            if existing:
                return existing[0], False
            task_id = self.insert_task(db, title, deadline, "", "", today=today)
            db.execute("INSERT INTO mail_task_sources (source, task_id) VALUES (?, ?)", (source, task_id))
            return task_id, True

    def claim_reminder(self, task: Task, kind: str, scheduled: str) -> bool:
        if kind == "deadline":
            check = "deadline = ?"
            value = task.deadline
        elif kind == "planned":
            check = "planned_day = ?"
            value = task.planned_day
        else:
            raise TaskError("Invalid reminder type.")
        with self.transaction("record reminders for") as db:
            result = db.execute(
                "INSERT OR IGNORE INTO fired_reminders (task_id, kind, scheduled) "
                "SELECT id, ?, ? FROM tasks WHERE id = ? AND status = 'open' AND " + check,
                (kind, scheduled, task.id, value),
            )
            return result.rowcount == 1

    def recent_reminders(self) -> list[str]:
        with self.transaction("read reminders for") as db:
            return [f"{scheduled} | {kind} | {title}" for scheduled, kind, title in db.execute(
                "SELECT r.scheduled, r.kind, t.title FROM fired_reminders r JOIN tasks t ON t.id = r.task_id "
                "ORDER BY r.rowid DESC LIMIT 10"
            )]

    def add(self, title: str, deadline: str, minutes: str, planned_day: str, *, today: date) -> int:
        with self.transaction("save") as db:
            return self.insert_task(db, title, deadline, minutes, planned_day, today=today)

    def insert_task(self, db: sqlite3.Connection, title: str, deadline: str, minutes: str,
                    planned_day: str, *, today: date) -> int:
        title = title.strip()
        if not title:
            raise TaskError("Enter a task title.")
        fields = self.fields(deadline, minutes, planned_day, today)
        task_id = db.execute(
        "INSERT INTO tasks (title, deadline, minutes, planned_day) VALUES (?, ?, ?, ?)",
        (title, *fields),
        ).lastrowid
        assert task_id is not None
        return task_id

    @staticmethod
    def fields(deadline: str, minutes: str, planned_day: str, today: date) -> tuple[str | None, int | None, str | None]:
        return (deadline_value(deadline, today), minutes_value(minutes, "Estimate", optional=True),
                local_date(planned_day, today).isoformat() if planned_day.strip() else None)

    def edit(self, task_id: int, deadline: str, minutes: str, planned_day: str, *, today: date) -> None:
        fields = self.fields(deadline, minutes, planned_day, today)
        self.update("UPDATE tasks SET deadline = ?, minutes = ?, planned_day = ?", fields, task_id)

    def complete(self, task_id: int) -> None:
        self.update("UPDATE tasks SET status = ?", ("complete",), task_id)

    def schedule(self, task_id: int, field: str, value: str, *, today: date) -> str:
        if field == "deadline":
            resolved = deadline_value(value, today)
            statement = "UPDATE tasks SET deadline = ?"
        elif field == "planned_day":
            resolved = local_date(value, today).isoformat()
            statement = "UPDATE tasks SET planned_day = ?"
        else:
            raise TaskError("Specify whether to move the planned day or the deadline.")
        if resolved is None:
            raise TaskError("Specify the new date.")
        self.update(statement, (resolved,), task_id)
        return resolved

    def update(self, statement: str, params: tuple[object, ...], task_id: int) -> None:
        with self.transaction("update") as db:
            result = db.execute(statement + " WHERE id = ? AND status = ?", (*params, task_id, "open"))
            if result.rowcount != 1:
                raise TaskError("That task is no longer open. Refresh and try again.")


def deadline_order(task: Task) -> datetime:
    if not task.deadline:
        return datetime.max
    if len(task.deadline) == 10:
        return datetime.combine(date.fromisoformat(task.deadline), time.max)
    return datetime.fromisoformat(task.deadline)


def is_today(task: Task, today: date) -> bool:
    return task.planned_day == today.isoformat() or is_due(task, today)


def is_due(task: Task, today: date) -> bool:
    return task.deadline is not None and task.deadline[:10] <= today.isoformat()


@dataclass(frozen=True)
class Plan:
    fitted: list[Task]
    unscheduled: list[Task]
    unestimated: list[Task]
    used: int
    budget: int


def ordered_tasks(tasks: Sequence[Task], today: date, now: datetime) -> list[Task]:
    candidates = [task for task in tasks if is_today(task, today) or task.planned_day is None]
    candidates.sort(key=lambda task: (
        deadline_order(task) >= now, deadline_order(task), task.planned_day != today.isoformat(), task.id,
    ))
    return candidates


def plan_today(tasks: Sequence[Task], budget: int, now: datetime, *, day: date | None = None) -> Plan:
    candidates = ordered_tasks(tasks, day or now.date(), now)
    fitted, unscheduled, unestimated = [], [], []
    used = 0
    for task in candidates:
        if task.minutes is None:
            unestimated.append(task)
        elif task.minutes + used <= budget:
            fitted.append(task)
            used += task.minutes
        else:
            unscheduled.append(task)
    return Plan(fitted, unscheduled, unestimated, used, budget)


def task_line(task: Task) -> str:
    estimate = f"{task.minutes} min" if task.minutes is not None else "unestimated"
    deadline = f" | due {task.deadline}" if task.deadline else ""
    planned = f" | planned {task.planned_day}" if task.planned_day else ""
    return f"#{task.id} {task.title} — {estimate}{deadline}{planned}"


def plan_text(plan: Plan, *, chinese: bool = False) -> str:
    lines = [f"可安排 {plan.used} / {plan.budget} 分钟" if chinese
             else f"Fits {plan.used} / {plan.budget} minutes"]
    headings = ("建议顺序", "排不下", "待估时") if chinese else ("Suggested order", "Does not fit", "Needs an estimate")
    for heading, group in zip(headings, (plan.fitted, plan.unscheduled, plan.unestimated), strict=True):
        lines.append(f"\n{heading}:")
        lines.extend(task_line(task) for task in group)
        if not group:
            lines.append("无" if chinese else "None")
    return "\n".join(lines)
