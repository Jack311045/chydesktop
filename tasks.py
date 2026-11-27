from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple
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
    raise TaskError("请输入有效日期（YYYY-MM-DD、今天、明天或星期几）。")


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
            raise TaskError("请输入有效的截止时间（HH:MM 或 HH:MM:SS）。") from None
        return f"{day} {clock.isoformat(timespec='seconds' if len(timed[2]) == 8 else 'minutes')}"
    return local_date(text, today).isoformat()


def minutes_value(value: str, label: str, *, optional: bool = False, minimum: int = 1) -> int | None:
    text = value.strip()
    if not text and optional:
        return None
    if not re.fullmatch(r"[0-9]{1,9}", text) or int(text) < minimum:
        raise TaskError(f"{label}必须是{'非负' if minimum == 0 else '正'}整数分钟数。")
    return int(text)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    deadline: str | None
    minutes: int | None
    planned_day: str | None
    mail_linked: bool = False
    category: str | None = None
    start: str | None = None
    end: str | None = None
    status: str = "open"
    notes: str | None = None
    completed_at: str | None = None


UNCATEGORIZED = "未分类"


def clean_category(name: str | None) -> str | None:
    """Normalize a category name; the reserved 未分类 label means no category."""
    text = " ".join((name or "").split())
    if len(text) > 40:
        raise TaskError("类别名称最多 40 个字。")
    return None if text == UNCATEGORIZED else text or None


def slot_values(day: str | None, start: str, end: str) -> tuple[str | None, str | None]:
    start, end = start.strip(), end.strip()
    if not start and not end:
        return None, None
    if day is None:
        raise TaskError("请先填写安排日期。")
    if not (re.fullmatch(r"[0-9]{2}:[0-9]{2}", start) and re.fullmatch(r"[0-9]{2}:[0-9]{2}", end)):
        raise TaskError("请填写开始和结束时间（HH:MM）。")
    try:
        start_time, end_time = time.fromisoformat(start), time.fromisoformat(end)
    except ValueError:
        raise TaskError("请输入有效的时间（HH:MM）。") from None
    if end_time <= start_time:
        raise TaskError("结束时间必须晚于开始时间。")
    return start, end


class EventMask(NamedTuple):
    key: str
    calendar: str
    title: str
    starts: str
    ends: str
    reason: str | None
    created_at: str


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise TaskError("无法创建本机待办文件夹。") from None
        with self.transaction("初始化") as db:
            db.execute("BEGIN IMMEDIATE")
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
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            for column in ("category", "slot_start", "slot_end", "notes", "completed_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            db.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT PRIMARY KEY COLLATE NOCASE, position INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS mail_dismissed (
                    source TEXT PRIMARY KEY, dismissed_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS event_masks (
                    key TEXT PRIMARY KEY, calendar TEXT NOT NULL, title TEXT NOT NULL,
                    starts TEXT NOT NULL, ends TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
                )
            """)
            # Categories that only existed on tasks become real categories, so they can stay when empty.
            db.execute(
                "INSERT OR IGNORE INTO categories (name, position) "
                "SELECT category, (SELECT COALESCE(MAX(position), 0) FROM categories) + MIN(id) FROM tasks "
                "WHERE category IS NOT NULL AND TRIM(category) <> '' GROUP BY category COLLATE NOCASE"
            )
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
            raise TaskError(f"本机待办数据库{operation}失败。请检查访问权限后重试。") from None

    def open_tasks(self) -> list[Task]:
        return self.list_tasks(status="open")

    def list_tasks(self, *, status: str | None = None) -> list[Task]:
        """Return rows by ID; omit status for both open and completed tasks."""
        with self.transaction("读取") as db:
            return [Task(*row[:5], bool(row[5]), *row[6:]) for row in db.execute(
                "SELECT t.id, title, deadline, minutes, planned_day, m.task_id IS NOT NULL, "
                "category, slot_start, slot_end, status, notes, completed_at "
                "FROM tasks t LEFT JOIN mail_task_sources m ON m.task_id = t.id "
                "WHERE (? IS NULL OR status = ?) ORDER BY t.id",
                (status, status),
            )]

    def add_mail_action(self, source: str, title: str, deadline: str, *, today: date) -> tuple[int, bool]:
        with self.transaction("保存") as db:
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
            raise TaskError("提醒类型无效。")
        with self.transaction("记录提醒") as db:
            result = db.execute(
                "INSERT OR IGNORE INTO fired_reminders (task_id, kind, scheduled) "
                "SELECT id, ?, ? FROM tasks WHERE id = ? AND status = 'open' AND " + check,
                (kind, scheduled, task.id, value),
            )
            return result.rowcount == 1

    def recent_reminders(self) -> list[str]:
        with self.transaction("读取提醒") as db:
            return [f"{scheduled} | {kind} | {title}" for scheduled, kind, title in db.execute(
                "SELECT r.scheduled, r.kind, t.title FROM fired_reminders r JOIN tasks t ON t.id = r.task_id "
                "ORDER BY r.rowid DESC LIMIT 10"
            )]

    def add(self, title: str, deadline: str, minutes: str, planned_day: str, *, today: date,
            category: str = "", start: str = "", end: str = "", notes: str = "") -> int:
        with self.transaction("保存") as db:
            return self.insert_task(db, title, deadline, minutes, planned_day, today=today,
                                    category=category, start=start, end=end, notes=notes)

    def insert_task(self, db: sqlite3.Connection, title: str, deadline: str, minutes: str,
                    planned_day: str, *, today: date, category: str = "", start: str = "", end: str = "",
                    notes: str = "") -> int:
        title = title.strip()
        if not title:
            raise TaskError("请输入任务名称。")
        fields = self.fields(deadline, minutes, planned_day, today)
        category_name = self.canonical_category(db, clean_category(category))
        task_id = db.execute(
            "INSERT INTO tasks (title, deadline, minutes, planned_day, category, slot_start, slot_end, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, *fields, category_name, *slot_values(fields[2], start, end), notes.strip() or None),
        ).lastrowid
        assert task_id is not None
        self.ensure_category(db, category_name)
        return task_id

    @staticmethod
    def canonical_category(db: sqlite3.Connection, name: str | None) -> str | None:
        """Reuse the stored spelling when a category differs only by case."""
        if name:
            existing = db.execute("SELECT name FROM categories WHERE name = ?", (name,)).fetchone()
            if existing:
                return existing[0]
        return name

    @staticmethod
    def ensure_category(db: sqlite3.Connection, name: str | None) -> None:
        if name:
            db.execute(
                "INSERT OR IGNORE INTO categories (name, position) "
                "VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM categories))", (name,))

    def categories(self) -> list[str]:
        with self.transaction("读取类别") as db:
            return [row[0] for row in db.execute("SELECT name FROM categories ORDER BY position, rowid")]

    def add_category(self, name: str) -> str:
        """Create a category; return the stored spelling (the existing one if it only differs by case)."""
        clean = clean_category(name)
        if clean is None:
            raise TaskError("“未分类”是保留名称，请换一个类别名称。" if " ".join(name.split()) == UNCATEGORIZED
                            else "请输入类别名称。")
        with self.transaction("保存类别") as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT name FROM categories WHERE name = ?", (clean,)).fetchone()
            if existing:
                return existing[0]
            self.ensure_category(db, clean)
        return clean

    def rename_category(self, old: str, new: str) -> str:
        clean = clean_category(new)
        if clean is None:
            raise TaskError("“未分类”是保留名称，请换一个类别名称。" if " ".join(new.split()) == UNCATEGORIZED
                            else "请输入新的类别名称。")
        with self.transaction("重命名类别") as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT name FROM categories WHERE name = ?", (old,)).fetchone()
            if current is None:
                raise TaskError(f"找不到类别“{old}”。")
            clash = db.execute("SELECT name FROM categories WHERE name = ?", (clean,)).fetchone()
            if clash and clash[0].casefold() != current[0].casefold():
                raise TaskError(f"类别“{clean}”已经存在。")
            db.execute("UPDATE categories SET name = ? WHERE name = ?", (clean, current[0]))
            db.execute("UPDATE tasks SET category = ? WHERE category = ? COLLATE NOCASE", (clean, current[0]))
        return clean

    def delete_category(self, name: str) -> int:
        """Remove a category; its tasks stay and become uncategorized. Returns how many tasks were affected."""
        with self.transaction("删除类别") as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT name FROM categories WHERE name = ?", (name,)).fetchone()
            if current is None:
                raise TaskError(f"找不到类别“{name}”。")
            moved = db.execute(
                "UPDATE tasks SET category = NULL WHERE category = ? COLLATE NOCASE", (current[0],)).rowcount
            db.execute("DELETE FROM categories WHERE name = ?", (current[0],))
            return moved

    def set_category(self, task_id: int, name: str | None) -> str | None:
        clean = clean_category(name)
        with self.transaction("更新") as db:
            db.execute("BEGIN IMMEDIATE")
            clean = self.canonical_category(db, clean)
            result = db.execute("UPDATE tasks SET category = ? WHERE id = ?", (clean, task_id))
            if result.rowcount != 1:
                raise TaskError("这条待办已不存在。")
            self.ensure_category(db, clean)
        return clean

    def delete(self, task_id: int) -> None:
        with self.transaction("删除") as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM mail_task_sources WHERE task_id = ?", (task_id,))
            db.execute("DELETE FROM fired_reminders WHERE task_id = ?", (task_id,))
            if db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount != 1:
                raise TaskError("这条待办已不存在。")

    def restore(self, task: Task) -> None:
        """Put a task row back exactly as captured, recreating it if it was deleted (used for undo)."""
        with self.transaction("恢复") as db:
            db.execute("BEGIN IMMEDIATE")
            values = (task.title, task.deadline, task.minutes, task.planned_day, task.category,
                      task.start, task.end, task.status, task.notes, task.completed_at)
            if db.execute("SELECT 1 FROM tasks WHERE id = ?", (task.id,)).fetchone():
                db.execute(
                    "UPDATE tasks SET title = ?, deadline = ?, minutes = ?, planned_day = ?, category = ?, "
                    "slot_start = ?, slot_end = ?, status = ?, notes = ?, completed_at = ? WHERE id = ?",
                    (*values, task.id))
            else:
                db.execute(
                    "INSERT INTO tasks (title, deadline, minutes, planned_day, category, slot_start, slot_end, "
                    "status, notes, completed_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, task.id))
            self.ensure_category(db, task.category)

    def dismissed_mail(self) -> set[str]:
        with self.transaction("读取") as db:
            return {row[0] for row in db.execute("SELECT source FROM mail_dismissed")}

    def dismiss_mail(self, source: str, *, now: datetime | None = None) -> None:
        moment = now or datetime.now()
        with self.transaction("保存") as db:
            db.execute("INSERT OR REPLACE INTO mail_dismissed (source, dismissed_at) VALUES (?, ?)",
                       (source, moment.isoformat(timespec="seconds")))
            db.execute("DELETE FROM mail_dismissed WHERE dismissed_at < ?",
                       ((moment - timedelta(days=60)).isoformat(timespec="seconds"),))

    def restore_mail(self, source: str) -> None:
        with self.transaction("保存") as db:
            db.execute("DELETE FROM mail_dismissed WHERE source = ?", (source,))

    def clear_dismissed_mail(self) -> None:
        with self.transaction("保存") as db:
            db.execute("DELETE FROM mail_dismissed")

    def event_mask_keys(self) -> frozenset[str]:
        with self.transaction("读取") as db:
            return frozenset(row[0] for row in db.execute("SELECT key FROM event_masks"))

    def event_masks(self) -> list[EventMask]:
        with self.transaction("读取") as db:
            return [EventMask(*row) for row in db.execute(
                "SELECT key, calendar, title, starts, ends, reason, created_at FROM event_masks "
                "ORDER BY starts, title")]

    def mask_event(self, key: str, calendar: str, title: str, starts: str, ends: str, *,
                   reason: str = "", now: datetime | None = None) -> None:
        """Hide one Google Calendar instance on this computer only; Google Calendar is never changed."""
        moment = now or datetime.now()
        with self.transaction("保存") as db:
            db.execute("INSERT OR REPLACE INTO event_masks (key, calendar, title, starts, ends, reason, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (key, calendar, title, starts, ends, reason.strip()[:200] or None,
                        moment.isoformat(timespec="seconds")))
            # Past events can no longer be on a calendar page, so their masks are dropped after a month.
            db.execute("DELETE FROM event_masks WHERE ends < ?",
                       ((moment - timedelta(days=30)).isoformat(timespec="seconds"),))

    def unmask_event(self, key: str) -> bool:
        with self.transaction("保存") as db:
            return db.execute("DELETE FROM event_masks WHERE key = ?", (key,)).rowcount == 1

    @staticmethod
    def fields(deadline: str, minutes: str, planned_day: str, today: date) -> tuple[str | None, int | None, str | None]:
        return (deadline_value(deadline, today), minutes_value(minutes, "预估时长", optional=True),
                local_date(planned_day, today).isoformat() if planned_day.strip() else None)

    def edit(self, task_id: int, deadline: str, minutes: str, planned_day: str, *, today: date) -> None:
        fields = self.fields(deadline, minutes, planned_day, today)
        self.update(
            "UPDATE tasks SET deadline = ?, minutes = ?, planned_day = ?, "
            "slot_start = CASE WHEN planned_day IS ? THEN slot_start END, "
            "slot_end = CASE WHEN planned_day IS ? THEN slot_end END",
            (*fields, fields[2], fields[2]), task_id,
        )

    def edit_details(self, task_id: int, title: str, deadline: str, minutes: str, planned_day: str, *,
                     today: date, category: str = "", start: str = "", end: str = "",
                     notes: str | None = None) -> None:
        """Replace editable fields without touching status or the mail source; notes=None keeps the old notes."""
        title = title.strip()
        if not title:
            raise TaskError("请输入任务名称。")
        fields = self.fields(deadline, minutes, planned_day, today)
        with self.transaction("更新") as db:
            db.execute("BEGIN IMMEDIATE")
            category_name = self.canonical_category(db, clean_category(category))
            result = db.execute(
                "UPDATE tasks SET title = ?, deadline = ?, minutes = ?, planned_day = ?, "
                "category = ?, slot_start = ?, slot_end = ?, notes = CASE WHEN ? THEN ? ELSE notes END WHERE id = ?",
                (title, *fields, category_name, *slot_values(fields[2], start, end),
                 notes is not None, (notes or "").strip() or None, task_id),
            )
            if result.rowcount != 1:
                raise TaskError("这条待办已不存在。")
            self.ensure_category(db, category_name)

    def complete(self, task_id: int) -> None:
        self.update("UPDATE tasks SET status = ?, completed_at = ?",
                    ("complete", datetime.now().isoformat(timespec="seconds")), task_id)

    def reopen(self, task_id: int) -> None:
        with self.transaction("更新") as db:
            result = db.execute(
                "UPDATE tasks SET status = 'open', completed_at = NULL WHERE id = ? AND status = 'complete'",
                (task_id,))
            if result.rowcount != 1:
                raise TaskError("这条待办不是已完成状态，请刷新后再试。")

    def schedule(self, task_id: int, field: str, value: str, *, today: date) -> str:
        if field == "deadline":
            resolved = deadline_value(value, today)
            statement = "UPDATE tasks SET deadline = ?"
            params = (resolved,)
        elif field == "planned_day":
            resolved = local_date(value, today).isoformat()
            statement = (
                "UPDATE tasks SET planned_day = ?, slot_start = CASE WHEN planned_day IS ? THEN slot_start END, "
                "slot_end = CASE WHEN planned_day IS ? THEN slot_end END"
            )
            params = (resolved, resolved, resolved)
        else:
            raise TaskError("请说明要改的是安排日期还是截止日期。")
        if resolved is None:
            raise TaskError("请说明新的日期。")
        self.update(statement, params, task_id)
        return resolved

    def update(self, statement: str, params: tuple[object, ...], task_id: int) -> None:
        with self.transaction("更新") as db:
            result = db.execute(statement + " WHERE id = ? AND status = ?", (*params, task_id, "open"))
            if result.rowcount != 1:
                raise TaskError("这条待办已不是未完成状态，请刷新后再试。")


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


class FormattedFields(NamedTuple):
    title: str
    category: str
    duration: str
    scheduled: str
    deadline: str
    status: str

    def line(self) -> str:
        return "｜".join(self)

    def details(self) -> str:
        labels = ("名称", "类别", "预估时长", "已安排时段", "截止日期", "完成状态")
        return "\n".join(f"{label}：{value}" for label, value in zip(labels, self, strict=True))


def format_slot(day: date | str | None = None, start: time | str | None = None,
                end: time | str | None = None, *, suggested: bool = False) -> str:
    """Distinguish a missing slot, a date without times, and a suggested range."""
    if day is None:
        return "未安排"
    if start is None or end is None:
        return f"{day}（未排时段）"
    start_text = start.isoformat(timespec="minutes") if isinstance(start, time) else start
    end_text = end.isoformat(timespec="minutes") if isinstance(end, time) else end
    prefix = "建议：" if suggested else ""
    return f"{prefix}{day} {start_text}–{end_text}"


def format_fields(title: str, *, category: str | None = None, minutes: int | None = None,
                  scheduled: str | None = None, deadline: str | date | None = None,
                  status: str | None = None) -> FormattedFields:
    return FormattedFields(
        title, category or "未分类", "未估时" if minutes is None else f"{minutes} 分钟",
        scheduled or "未安排", str(deadline) if deadline else "无截止日期",
        "不适用" if status is None else status,
    )


def task_fields(task: Task) -> FormattedFields:
    return format_fields(
        task.title, category=task.category, minutes=task.minutes,
        scheduled=format_slot(task.planned_day, task.start, task.end), deadline=task.deadline,
        status="已完成" if task.status == "complete" else "未完成",
    )


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
