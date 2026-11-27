from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, UTC
import re
from zoneinfo import ZoneInfo

from availability import availability_text, openings, place_tasks, slot_intervals, window_end
from calendar_reader import CalendarError, CalendarReader, day_bounds
from task_chat import COMMANDS, matches_command, time_budget
from tasks import TaskError, TaskStore, plan_text, plan_today


@dataclass(frozen=True)
class CalendarQuery:
    day: date
    start: time
    end: time
    plan: bool
    budget: int | None
    chinese: bool
    events: bool = False
    point: time | None = None
    follow_up: bool = False


@dataclass(frozen=True)
class CalendarContext:
    query: CalendarQuery
    zone: ZoneInfo
    selected: tuple[str, ...]


def parse_calendar_query(message: str, *, connected: bool, today: date,
                         window: tuple[str, str] = ("08:00", "18:00"),
                         active: CalendarQuery | None = None) -> CalendarQuery | None:
    text = message.casefold()
    if any(matches_command(command, text) for command in COMMANDS.values()):
        return None
    clock_range = re.search(
        r"(?<!\d)(\d{1,2}):(\d{2})\s*(?:to|[-–]|到|至)\s*(\d{1,2}):(\d{2})(?!\d)"
        r"|(?:上午|下午|晚上)?([一二兩两三四五六七八九十\d]{1,2})[點点]\s*[到至]\s*(?:上午|下午|晚上)?([一二兩两三四五六七八九十\d]{1,2})[點点]",
        text,
    )
    asks_free = bool(re.search(r"空[檔档]|有[沒没]有空|(?:什[麼么]時候|什么时候|幾點|几点).*?有空"
                               r"|free time|free slot|availability|when am i free"
                               r"|(?:check|show|look at).*calendar|(?:看|查).*?(?:行事曆|日历)", text))
    # "安排" and "schedule" are also ordinary nouns ("我有其他安排"), so only a request to arrange something counts.
    asks_plan = bool(re.search(
        r"排一下|排程|(?:幫我|帮我|请|請)\s*安排|安排一下|安排下|先做哪"
        r"|\b(?:plan|prioriti[sz]e)\s+(?:my|the|these|those|this|that|it|today|tomorrow|me|some)\b"
        r"|\bschedule\s+(?:my|the|these|those|this|that|it|today|tomorrow|me|some)\b|what should i do first", text))
    events = bool(re.search(r"其他(?:時間|时间)|剩下的(?:時間|时间)|what (?:am i doing|is booked)|rest of (?:the|that) (?:time|window)", text))
    single = re.fullmatch(
        r"\s*(?:那|then\s+|what about\s+)?\s*(?:(\d{1,2}:\d{2})|"
        r"(上午|下午|晚上)?([一二兩两三四五六七八九十\d]{1,2})[點点])\s*(?:呢|[？?])*\s*", text)
    if not (asks_free or events or single or (asks_plan and (clock_range or connected))):
        return None
    explicit = re.search(r"(?<![\d/-])(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})/(\d{1,2}))(?![\d/-])", text)
    relative = re.search(r"今天|明天|\b(?:today|tomorrow)\b", text)
    follow_up = active is not None and bool(events or single or re.match(r"\s*(?:那|then\b|what about\b)", text))
    if not follow_up:
        active = None
    chinese = bool(re.search(r"[\u3400-\u9fff]", message))
    if (events or single) and active is None and not (explicit or relative or clock_range):
        raise TaskError("你想查哪一天或哪个时段？" if chinese else "Which date or time window should I check?")
    try:
        if explicit and explicit[1]:
            day = date(int(explicit[1]), int(explicit[2]), int(explicit[3]))
        elif explicit:
            day = date(today.year, int(explicit[4]), int(explicit[5]))
        elif relative:
            day = today + timedelta(days=int(relative[0] in ("明天", "tomorrow")))
        else:
            day = active.day if active else today
    except ValueError:
        raise TaskError("Use a valid calendar date (YYYY-MM-DD or M/D).") from None
    if clock_range:
        try:
            if clock_range[1]:
                start = time(int(clock_range[1]), int(clock_range[2]))
                end = time(int(clock_range[3]), int(clock_range[4]))
            else:
                def hour(value: str) -> int:
                    number = int(value) if value.isascii() else "一二三四五六七八九十".index(
                        value.replace("兩", "二").replace("两", "二")) + 1
                    if ("下午" in text or "晚上" in text) and number < 12:
                        number += 12
                    return number
                start, end = time(hour(clock_range[5])), time(hour(clock_range[6]))
        except ValueError:
            raise TaskError("Use a valid time window, for example 14:00–16:00.") from None
    elif single and active:
        start, end = active.start, active.end
    elif re.search(r"下午|afternoon", text):
        start, end = time(12), time(18)
    elif re.search(r"上午|morning", text):
        start, end = time(8), time(12)
    elif re.search(r"晚上|evening", text):
        start, end = time(18), time.min
    elif active:
        start, end = active.start, active.end
    else:
        start, end = (planning_time(value) for value in window)
    validate_window(start, end)
    point = None
    if single:
        try:
            if single[1]:
                point = planning_time(single[1])
            else:
                value = single[3].replace("兩", "二").replace("两", "二")
                hour = int(value) if value.isascii() else "一二三四五六七八九十".index(value) + 1
                if single[2] in ("下午", "晚上") and hour < 12:
                    hour += 12
                elif not single[2] and hour < 12:
                    candidates = [time(h) for h in (hour, hour + 12)
                                  if start <= time(h) and (end == time.min or time(h) < end)]
                    if len(candidates) != 1:
                        raise TaskError("请说明是上午还是下午。" if chinese else "Do you mean AM or PM?")
                    hour = candidates[0].hour
                point = time(hour)
        except ValueError:
            raise TaskError("请使用有效的本地时间（HH:MM）。") from None
    return CalendarQuery(day, start, end, asks_plan, time_budget(text), chinese, events, point, follow_up)


def planning_time(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        raise TaskError("请选择有效的本地时间（HH:MM）。") from None


def validate_window(start: time, end: time) -> None:
    if end != time.min and end <= start:
        raise TaskError("规划时段的结束时间必须晚于开始时间。")


def calendar_answer(query: CalendarQuery, reader: CalendarReader, zone: ZoneInfo,
                    store: TaskStore | None, *, now: datetime | None = None,
                    selected: tuple[str, ...] | None = None) -> tuple[str, str | None]:
    window_label = f"{query.start:%H:%M}–{query.end:%H:%M}"
    heading = f"{query.day} ({zone.key}) | {window_label}"
    try:
        snapshot = (reader.read(query.day, zone, reuse=True) if selected is None
                    else reader.read(query.day, zone, reuse=True, selected=selected))
        if snapshot.failures:
            raise CalendarError("One or more selected calendars could not be read.")
    except CalendarError:
        notice = ("无法取得完整的日历空档。" if query.chinese else
                  "Complete calendar availability is unavailable.")
        if query.plan and query.budget is not None and store is not None:
            local_now = (now or datetime.now(zone)).astimezone(zone).replace(tzinfo=None)
            result = plan_today(store.open_tasks(), query.budget, local_now, day=query.day)
            return heading + "\n" + notice + "\n" + plan_text(result, chinese=query.chinese), None
        hint = ("请先到“今日”页连接或刷新日历，也可以告诉我可用的分钟数。" if query.chinese else
                "Connect or refresh in Today, or tell me your available minutes for a manual plan.")
        return heading + "\n" + notice + "\n" + hint, None
    now = now or datetime.now(zone)
    try:
        saved_slots = slot_intervals(store.open_tasks(), query.day, zone) if store else []
    except TaskError:
        saved_slots = []
    free = openings(snapshot, query.start, query.end, now, saved_slots)
    if query.plan and store is None:
        raise TaskError("本机待办存储不可用。")
    tasks = store.open_tasks() if query.plan and store else []
    schedule = place_tasks(snapshot, free, tasks, now, query.budget) if query.plan and store else None
    ended = now.timestamp() >= window_end(query.day, query.end, zone).timestamp()
    lower = datetime.combine(query.day, query.start, zone).astimezone(UTC)
    upper = window_end(query.day, query.end, zone).astimezone(UTC)
    busy_events = [event for event in snapshot.events if event.busy
                   and event.start.astimezone(UTC) < upper and event.end.astimezone(UTC) > lower]
    facts = [heading]
    if query.point is not None:
        instant = datetime.combine(query.day, query.point, zone).astimezone(UTC)
        matches = [event for event in snapshot.events if event.busy
                   and event.start.astimezone(UTC) <= instant < event.end.astimezone(UTC)]
        status = ("忙碌" if matches else "有空") if query.chinese else ("busy" if matches else "free")
        facts.append(f"{query.point:%H:%M}: {status}.")
        busy_events = matches
    else:
        # Historical busy blocks describe calendar commitments, not remaining time from now.
        all_free = openings(snapshot, query.start, query.end, day_bounds(query.day, zone)[0], saved_slots)
        cursor = lower
        busy = []
        for left, right in [*all_free, (upper, upper)]:
            if cursor < left:
                busy.append(f"{cursor.astimezone(zone):%H:%M}–{left.astimezone(zone):%H:%M}")
            cursor = right
        if busy:
            label = "其余忙碌时段：" if query.chinese else "Busy blocks in this window: "
            facts.append(label + ", ".join(busy))
    safe = "\n".join(facts) + "\n" + availability_text(
        snapshot, free, None if any(task.mail_linked for task in tasks) else schedule,
        chinese=query.chinese, include_task_details=False, window_ended=ended)
    local = "\n".join(facts) + "\n" + availability_text(
        snapshot, free, schedule, chinese=query.chinese, window_ended=ended)
    if query.events or query.point is not None:
        label = "日历项目：" if query.chinese else "Calendar entries:"
        items = [f"{event.start:%H:%M}–{event.end:%H:%M}  {event.title}" if not event.all_day
                 else f"{'全天' if query.chinese else 'All day'}  {event.title}" for event in busy_events]
        local += "\n" + label + "\n" + ("\n".join(items) or ("无" if query.chinese else "None"))
    return local, safe
