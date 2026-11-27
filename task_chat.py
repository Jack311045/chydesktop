"""Model-driven task and category changes for chat.

The model decides what the producer wants (any wording, any language) and returns structured
operations. This module never judges intent from phrases: it validates every operation against
the local store, executes or stages it, and reports only what actually happened.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
import json
import re

from calendar_masks import event_line, mask_event, require_event
from calendar_reader import Event, event_key
from dialogue import Reply
from tasks import (
    Task, TaskError, TaskStore, clean_category, deadline_value, local_date, minutes_value, plan_text,
    plan_today, slot_values, task_line,
)

MODES = ("none", "propose", "execute", "approve_pending", "cancel_pending", "lookup")
OPERATION_KINDS = (
    "list", "plan", "add", "complete", "reopen", "reschedule", "update", "set_category",
    "create_category", "rename_category", "delete_category", "undo",
    "lookup_events", "mask_event", "unmask_event",
)
OPERATION_FIELDS = (
    "kind", "title", "target", "date", "field", "minutes", "category", "start", "end", "notes", "new_name",
)
READ_KINDS = ("list", "plan")
STAGED_ONLY_KINDS = ("delete_category",)
EVENT_KINDS = ("mask_event", "unmask_event")
MAX_OPERATIONS = 40
MAX_CONTEXT_TASKS = 40

TASK_INSTRUCTIONS = """
Besides your reply, return an `action` for the producer's local to-do list, task categories and
local calendar masks. The app owns the data: it validates and writes every change itself, then adds
its own receipt, so never say a change is saved before the app confirms it.

action.mode:
- none: ordinary conversation, a question, a casual or hypothetical mention, or something already
  done. Leave operations empty.
- execute: the producer asked you to make the change, in any wording or language. Fill in every
  operation and do not ask permission again. Make your reply a short in-character acknowledgement.
- propose: the producer told you plans, errands, deadlines or a new project they will need to
  track but did not ask you to save them. Your first reaction is to ask, in one short natural
  question, whether to put them on the to-do list. Do not list the items in the reply; the app
  shows the exact list. Fill in the operations you would perform so a simple yes saves them.
  Also use propose for calendar changes when you had to guess which events the producer meant.
- approve_pending / cancel_pending: `pending_proposal` in the app data is waiting for an answer.
  If the latest message agrees, in any wording, use approve_pending. If they agree with changes,
  use execute with the corrected operations. If they decline, use cancel_pending.
- lookup: you need to see calendar events first (see Calendar below). Operations are lookup_events.

Operations (use "" for fields a kind does not use; each is one change):
- add: title; optional date (+ field), minutes, category, start/end (HH:MM, with field=planned_day),
  notes. Split a list of items into one add per item. A new plan for a time slot is an add with
  field=planned_day, date, start and end.
- complete / reopen: target.
- reschedule: target, date, field=deadline or planned_day (+ optional start/end HH:MM).
- update: target, plus new_name, minutes or notes for what changes.
- set_category: target and category ("" removes it from any category).
- create_category: category. rename_category: category (current name) and new_name.
- delete_category: category. Its tasks are kept without a category. The app always asks first.
- list or plan: read-only; for plan put the available minutes in minutes. Keep reply to one line.
- undo: take back the last change the app made when the producer asks to revert it.

Calendar. You cannot change Google Calendar and never say you did. The producer's classes, study
blocks and meetings live there, and you cannot see them until the app shows them to you. The only
calendar change available is a local mask: it hides one event on this computer, frees that time for
planning and leaves a faded copy on the calendar page; refreshing or reconnecting never undoes it.
Use it when the producer says an event is cancelled, skipped, moved away or replaced ("no class
today", "the teacher is out", "I have something else then", "clear that block").
- To find the events, return mode=lookup with one lookup_events operation per date you need
  (date=YYYY-MM-DD, at most 4) and a one-line reply. The app then shows you those days in
  `calendar_events` (id, title, time, calendar, has_place, cleared_locally) and asks again. Never
  invent events or ids; if `calendar_events` is unavailable, say you cannot see the calendar now.
- mask_event: target is the event id from `calendar_events`; one operation per event. Include
  every event that matches what the producer described (a whole day, "this afternoon", "the
  morning grind", "DS301") and nothing else, for example not study blocks when they only said
  classes. Classes usually have a place (has_place) and a course-like title. Add the reason in notes.
- unmask_event: target is the id of an event with cleared_locally=true to bring it back.
- After clearing a block the producer may plan the time later: do not invent a replacement. When
  they give the new plan, add it as a task with a planned day and start/end.

Resolve words like "these three", "that one" or "the report" from the recent conversation and the
task data, and give task targets as "#id" from the data. Compute dates yourself from today's date
in the data and give them as YYYY-MM-DD (a deadline may add " HH:MM"). If which task, which date
field or which day is genuinely unclear, ask in the reply with mode=none instead of guessing. Use
an existing category when one clearly fits; create a new category only when the producer asks.
"""


def say(chinese: bool, zh: str, en: str) -> str:
    return zh if chinese else en


@dataclass(frozen=True)
class Operation:
    kind: str
    title: str = ""
    target: str = ""
    date: str = ""
    field: str = ""
    minutes: str = ""
    category: str = ""
    start: str = ""
    end: str = ""
    notes: str = ""
    new_name: str = ""
    task_id: int | None = None
    label: str = ""
    event: Event | None = None

    @classmethod
    def parse(cls, payload: object) -> "Operation":
        invalid = TaskError("Claude returned an invalid task action. Nothing was changed.")
        if not isinstance(payload, dict) or set(payload) != set(OPERATION_FIELDS):
            raise invalid
        if any(not isinstance(payload[key], str) for key in OPERATION_FIELDS):
            raise invalid
        operation = cls(**{key: payload[key].strip() for key in OPERATION_FIELDS})
        if operation.kind not in OPERATION_KINDS or operation.field not in ("", "deadline", "planned_day"):
            raise invalid
        return operation

    def for_model(self) -> dict[str, str | int]:
        values: dict[str, str | int] = {"kind": self.kind}
        for name in OPERATION_FIELDS[1:]:
            if value := getattr(self, name):
                values[name] = value
        if self.task_id is not None:
            values["target"] = f"#{self.task_id}"
        if self.event is not None:
            # Event ids only live for one request, so a waiting proposal is described by the event itself.
            values.pop("target", None)
            values["event"] = self.label
        return values


@dataclass(frozen=True)
class TaskAction:
    mode: str = "none"
    operations: tuple[Operation, ...] = ()

    @classmethod
    def parse(cls, payload: object) -> "TaskAction":
        invalid = TaskError("Claude returned an invalid task action. Nothing was changed.")
        if not isinstance(payload, dict) or set(payload) != {"mode", "operations"}:
            raise invalid
        operations = payload["operations"]
        if payload["mode"] not in MODES or not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
            raise invalid
        return cls(payload["mode"], tuple(Operation.parse(item) for item in operations))


@dataclass(frozen=True)
class TaskReply(Reply):
    action: TaskAction


@dataclass(frozen=True)
class Proposal:
    """Validated changes that are waiting for the producer's approval."""
    operations: tuple[Operation, ...]

    def describe(self, chinese: bool) -> str:
        return "\n".join("· " + describe(operation, chinese) for operation in self.operations)


@dataclass(frozen=True)
class UndoEntry:
    steps: tuple[Callable[[TaskStore], None], ...]

    def run(self, store: TaskStore) -> None:
        for step in reversed(self.steps):
            step(store)


@dataclass(frozen=True)
class TaskOutcome:
    reply: Reply
    changed: bool = False
    proposal: Proposal | None = None
    undo: UndoEntry | None = None
    undone: bool = False
    memory: str = ""


@dataclass
class Applied:
    receipts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps: list[Callable[[TaskStore], None]] = field(default_factory=list)


def describe(op: Operation, chinese: bool) -> str:
    target = f"#{op.task_id} {op.label}".strip()
    which = say(chinese, "截止日期", "deadline") if op.field == "deadline" else say(chinese, "计划日期", "planned day")
    if op.kind == "add":
        parts = []
        if op.category:
            parts.append(say(chinese, f"类别：{op.category}", f"list: {op.category}"))
        if op.minutes:
            parts.append(say(chinese, f"{op.minutes} 分钟", f"{op.minutes} min"))
        if op.date:
            slot = f" {op.start}-{op.end}" if op.start and op.end else ""
            parts.append(f"{which} {op.date}{slot}")
        suffix = f"（{'，'.join(parts)}）" if chinese and parts else f" ({', '.join(parts)})" if parts else ""
        return say(chinese, f"新增“{op.title}”", f"add “{op.title}”") + suffix
    if op.kind == "complete":
        return say(chinese, f"标记完成 {target}", f"mark {target} complete")
    if op.kind == "reopen":
        return say(chinese, f"重新打开 {target}", f"reopen {target}")
    if op.kind == "reschedule":
        slot = f" {op.start}-{op.end}" if op.start and op.end else ""
        return say(chinese, f"把 {target} 的{which}改到 {op.date}{slot}",
                   f"move the {which} of {target} to {op.date}{slot}")
    if op.kind == "update":
        changes = []
        if op.new_name:
            changes.append(say(chinese, f"名称改为“{op.new_name}”", f"rename to “{op.new_name}”"))
        if op.minutes:
            changes.append(say(chinese, f"时长改为 {op.minutes} 分钟", f"estimate {op.minutes} min"))
        if op.notes:
            changes.append(say(chinese, "更新备注", "update notes"))
        return say(chinese, f"修改 {target}：", f"update {target}: ") + say(chinese, "，", ", ").join(changes)
    if op.kind == "set_category":
        if op.category:
            return say(chinese, f"把 {target} 放入类别“{op.category}”", f"put {target} in “{op.category}”")
        return say(chinese, f"把 {target} 移出类别", f"remove {target} from its category")
    if op.kind == "create_category":
        return say(chinese, f"新建类别“{op.category}”", f"create the category “{op.category}”")
    if op.kind == "rename_category":
        return say(chinese, f"把类别“{op.category}”改名为“{op.new_name}”",
                   f"rename the category “{op.category}” to “{op.new_name}”")
    if op.kind == "delete_category":
        return say(chinese, f"删除类别“{op.category}”（其中的待办会保留，变成未分类）",
                   f"delete the category “{op.category}” (its tasks stay, uncategorized)")
    if op.kind == "mask_event":
        return say(chinese, f"在本机取消 {op.label}（不会改动 Google 日历）",
                   f"clear {op.label} on this computer only (Google Calendar is not changed)")
    if op.kind == "unmask_event":
        return say(chinese, f"恢复 {op.label}", f"bring back {op.label}")
    return say(chinese, "撤销上一次修改", "undo the last change")


def find_task(store: TaskStore, target: str, chinese: bool, *, statuses: Sequence[str] = ("open",)) -> Task:
    tasks = [task for task in store.list_tasks() if task.status in statuses]
    text = target.strip()
    number = re.fullmatch(r"#?\s*([0-9]{1,19})", text)
    if number:
        matches = [task for task in tasks if task.id == int(number[1])]
    else:
        needle = text.casefold()
        matches = [task for task in tasks if needle and task.title.casefold() == needle] or [
            task for task in tasks if needle and needle in task.title.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not text:
        raise TaskError(say(chinese, "请说明是哪一项待办。", "Which task do you mean?"))
    if matches:
        listing = "\n".join(task_line(task) for task in matches)
        raise TaskError(say(chinese, f"“{text}”对应多项待办，请用 #编号 指定：\n{listing}",
                            f"“{text}” matches several tasks; name one by #ID:\n{listing}"))
    raise TaskError(say(chinese, f"没有找到“{text}”对应的待办。", f"No task matched “{text}”."))


def find_category(store: TaskStore, name: str, chinese: bool) -> str:
    clean = clean_category(name)
    for existing in store.categories():
        if clean is not None and existing.casefold() == clean.casefold():
            return existing
    raise TaskError(say(chinese, f"没有名为“{name}”的类别。", f"There is no category named “{name}”."))


def prepare(op: Operation, store: TaskStore, today: date, chinese: bool,
            events: Mapping[str, Event] | None = None) -> Operation:
    """Validate against the local store and resolve names and dates without writing anything."""
    kind = op.kind
    if kind in EVENT_KINDS:
        event = op.event or require_event(events, op.target, chinese)
        key = event_key(event)
        masked = key in store.event_mask_keys()
        label = event_line(event)
        if kind == "mask_event" and masked:
            raise TaskError(say(chinese, f"“{label}”已经在本机取消了。", f"“{label}” is already cleared here."))
        if kind == "unmask_event" and not masked:
            raise TaskError(say(chinese, f"“{label}”没有被取消，不需要恢复。", f"“{label}” is not cleared."))
        return replace(op, event=event, label=label)
    if kind == "add":
        if not op.title:
            raise TaskError(say(chinese, "请说明待办的名称。", "Give the task a title."))
        when_field = (op.field or "deadline") if op.date else ""
        resolved = ""
        if op.date:
            resolved = (deadline_value(op.date, today) if when_field == "deadline"
                        else local_date(op.date, today).isoformat()) or ""
        minutes = minutes_value(op.minutes, "预估时长", optional=True)
        slot_values(resolved if when_field == "planned_day" else None, op.start, op.end)
        return replace(op, date=resolved, field=when_field, minutes=str(minutes or ""),
                       category=clean_category(op.category) or "")
    if kind in ("complete", "reopen", "reschedule", "update", "set_category"):
        statuses = ("complete",) if kind == "reopen" else ("open",) if kind in ("complete", "reschedule") else (
            "open", "complete")
        task = find_task(store, op.target, chinese, statuses=statuses)
        prepared = replace(op, task_id=task.id, target=f"#{task.id}", label=task.title)
        if kind == "reschedule":
            if op.field not in ("deadline", "planned_day"):
                raise TaskError(say(chinese, "要改的是计划日期还是截止日期？", "Is that the planned day or the deadline?"))
            if not op.date:
                raise TaskError(say(chinese, "要改到哪一天？", "Which date should I use?"))
            resolved = (deadline_value(op.date, today) if op.field == "deadline"
                        else local_date(op.date, today).isoformat()) or ""
            slot_values(resolved if op.field == "planned_day" else None, op.start, op.end)
            return replace(prepared, date=resolved)
        if kind == "update":
            if not (op.new_name or op.minutes or op.notes):
                raise TaskError(say(chinese, "没有要修改的内容。", "There is nothing to change."))
            minutes = minutes_value(op.minutes, "预估时长", optional=True)
            return replace(prepared, minutes=str(minutes or ""))
        if kind == "set_category":
            return replace(prepared, category=clean_category(op.category) or "")
        return prepared
    if kind == "create_category":
        name = clean_category(op.category)
        if name is None:
            raise TaskError(say(chinese, "请说明类别名称。", "Give the category a name."))
        return replace(op, category=name)
    if kind == "rename_category":
        new = clean_category(op.new_name)
        if new is None:
            raise TaskError(say(chinese, "请说明类别的新名称。", "Give the category a new name."))
        return replace(op, category=find_category(store, op.category, chinese), new_name=new)
    if kind == "delete_category":
        return replace(op, category=find_category(store, op.category, chinese))
    return op


def run(op: Operation, store: TaskStore, today: date, chinese: bool) -> tuple[str, Callable[[TaskStore], None] | None]:
    """Write one prepared operation. Returns a receipt and the step that takes it back."""
    kind = op.kind
    if kind == "add":
        planned = op.date if op.field == "planned_day" else ""
        deadline = op.date if op.field == "deadline" else ""
        task_id = store.add(op.title, deadline, op.minutes, planned, today=today, category=op.category,
                            start=op.start, end=op.end, notes=op.notes)
        return (say(chinese, f"已新增 #{task_id} {op.title}", f"Added #{task_id} {op.title}"),
                lambda s: s.delete(task_id))
    if kind in ("complete", "reopen", "reschedule", "update", "set_category"):
        assert op.task_id is not None
        before = find_task(store, f"#{op.task_id}", chinese, statuses=("open", "complete"))
        if kind == "complete":
            store.complete(before.id)
        elif kind == "reopen":
            store.reopen(before.id)
        elif kind == "set_category":
            store.set_category(before.id, op.category or None)
        else:
            title, deadline, minutes = before.title, before.deadline or "", str(before.minutes or "")
            planned, start, end = before.planned_day or "", before.start or "", before.end or ""
            notes = None
            if kind == "reschedule":
                if op.field == "deadline":
                    deadline = op.date
                else:
                    planned = op.date
                    start, end = (op.start, op.end) if op.start and op.end else (
                        (start, end) if planned == before.planned_day else ("", ""))
            else:
                title = op.new_name or title
                minutes = op.minutes or minutes
                notes = op.notes if op.notes else None
            store.edit_details(before.id, title, deadline, minutes, planned, today=today,
                               category=before.category or "", start=start, end=end, notes=notes)
        return (say(chinese, f"已更新：{describe(op, True)}", f"Done: {describe(op, False)}"),
                lambda s: s.restore(before))
    if kind == "create_category":
        existed = any(name.casefold() == op.category.casefold() for name in store.categories())
        name = store.add_category(op.category)
        if existed:
            return say(chinese, f"类别“{name}”已经存在", f"The category “{name}” already exists"), None
        return (say(chinese, f"已新建类别“{name}”", f"Created the category “{name}”"),
                lambda s: s.delete_category(name))
    if kind == "rename_category":
        new = store.rename_category(op.category, op.new_name)
        return (say(chinese, f"已把类别“{op.category}”改名为“{new}”", f"Renamed “{op.category}” to “{new}”"),
                lambda s: s.rename_category(new, op.category))
    if kind == "delete_category":
        members = [task for task in store.list_tasks() if (task.category or "").casefold() == op.category.casefold()]
        store.delete_category(op.category)

        def put_back(s: TaskStore) -> None:
            s.add_category(op.category)
            for task in members:
                s.restore(task)
        return say(chinese, f"已删除类别“{op.category}”", f"Deleted the category “{op.category}”"), put_back
    if kind in EVENT_KINDS:
        assert op.event is not None
        event, key = op.event, event_key(op.event)
        if kind == "mask_event":
            mask_event(store, event, reason=op.notes)
            return (say(chinese, f"已在本机取消 {op.label}（Google 日历没有改动）",
                        f"Cleared {op.label} on this computer only (Google Calendar is unchanged)"),
                    lambda s: s.unmask_event(key))
        store.unmask_event(key)

        def clear_again(s: TaskStore) -> None:
            mask_event(s, event, reason=op.notes)
        return say(chinese, f"已恢复 {op.label}", f"Brought back {op.label}"), clear_again
    raise TaskError(say(chinese, "不支持这项操作。", "That operation is not supported."))


def commit(operations: Sequence[Operation], store: TaskStore, now: datetime, chinese: bool,
           events: Mapping[str, Event] | None = None) -> tuple[Applied, bool]:
    """Prepare and write each operation independently so one failure never hides the others."""
    applied = Applied()
    today = now.date()
    for op in operations:
        try:
            receipt, step = run(prepare(op, store, today, chinese, events), store, today, chinese)
        except TaskError as error:
            applied.errors.append(str(error))
            continue
        applied.receipts.append(receipt)
        if step is not None:
            applied.steps.append(step)
    return applied, bool(applied.receipts)


def report(applied: Applied, chinese: bool) -> str:
    lines = []
    if applied.receipts:
        lines.append(say(chinese, "已保存到本机：", "Saved locally:"))
        lines.extend("· " + receipt for receipt in applied.receipts)
    if applied.errors:
        lines.append(say(chinese, "没有处理：", "Not changed:"))
        lines.extend("· " + error for error in applied.errors)
    return "\n".join(lines)


def approve(proposal: Proposal, store: TaskStore, now: datetime, chinese: bool) -> TaskOutcome:
    applied, changed = commit(proposal.operations, store, now, chinese)
    text = report(applied, chinese)
    return TaskOutcome(
        Reply(text, "focused"), changed, undo=UndoEntry(tuple(applied.steps)) if applied.steps else None,
        memory=f"[App: the producer approved the proposal. {text}]")


def time_budget(message: str) -> int | None:
    duration = re.search(r"(?:([0-9]{1,9})\s*(?:hours?\b|小时|个小时|min(?:ute)?s?\b|分钟)|一[个]?小时|半[个]?小时|(?:an|one) hour\b)", message.casefold())
    if duration is None:
        return None
    text = duration[0]
    if duration[1]:
        return int(duration[1]) * (60 if "hour" in text or "小时" in text else 1)
    return 30 if "半" in text else 60


# Used only to keep an obvious task command away from the calendar-availability router.
COMMANDS = {
    "add": r"add\b|create\b|帮我加|添加|新增",
    "complete": r"complete\b|mark\b.+\b(?:done|complete|completed)\b|帮我完成|(?:帮我)?把.+(?:标为|标记为)完成",
    "reschedule": r"move\b|reschedule\b|(?:change|update|set)\b|(?:帮我)?把.+(?:改|移|挪)|(?:帮我)?(?:调整|移动)",
}
COMMAND_PREFIX = r"^\s*(?:(?:please|can you|could you|would you)\s+|请\s*)*"


def matches_command(command: str, message: str) -> bool:
    return re.match(COMMAND_PREFIX + "(?:" + command + ")", message, re.IGNORECASE) is not None


def task_context(store: TaskStore, now: datetime, pending: Proposal | None = None,
                 calendar_events: object | None = None) -> str:
    """The minimum local task data the model needs to resolve targets. Notes and mail titles stay local."""
    tasks = store.list_tasks()
    open_tasks = [task for task in tasks if task.status == "open"][:MAX_CONTEXT_TASKS]

    def entry(task: Task) -> dict[str, str | int | bool]:
        values: dict[str, str | int | bool] = {
            "id": task.id, "title": "(task from an email)" if task.mail_linked else task.title}
        for name, value in (("category", task.category), ("deadline", task.deadline),
                            ("planned_day", task.planned_day), ("minutes", task.minutes)):
            if value:
                values[name] = value
        if task.start and task.end:
            values["slot"] = f"{task.start}-{task.end}"
        return values

    data: dict[str, object] = {
        "today": f"{now:%Y-%m-%d} ({now:%A})", "local_time": f"{now:%H:%M}",
        "categories": store.categories(),
        "open_tasks": [entry(task) for task in open_tasks],
        "recently_completed": [entry(task) for task in tasks if task.status == "complete"][-8:],
    }
    if pending is not None:
        data["pending_proposal"] = [operation.for_model() for operation in pending.operations]
    if calendar_events is not None:
        data["calendar_events"] = calendar_events
    return json.dumps(data, ensure_ascii=False)


def with_calendar_events(context: str | None, calendar_events: object) -> str:
    """Add the events the model asked to see to an existing context (one lookup per request)."""
    data = json.loads(context) if context else {}
    data["calendar_events"] = calendar_events
    return json.dumps(data, ensure_ascii=False)


def read_reply(operation: Operation, reply: Reply, message: str, store: TaskStore, now: datetime,
               chinese: bool) -> TaskOutcome:
    if operation.kind == "list":
        tasks = store.open_tasks()
        text = say(chinese, "没有未完成的待办。", "There are no open tasks.") if not tasks else (
            say(chinese, "未完成的待办：", "Open tasks:") + "\n" + "\n".join(task_line(task) for task in tasks))
    else:
        try:
            budget = minutes_value(operation.minutes, "可用时间", optional=True)
        except TaskError:
            budget = None
        budget = budget or time_budget(message)
        text = (say(chinese, "你今天有多少分钟可以安排？", "How many minutes do you have today?") if budget is None
                else plan_text(plan_today(store.open_tasks(), budget, now), chinese=chinese))
    return TaskOutcome(Reply(text, reply.expression), memory=f"{reply.reply}\n[App displayed: {text}]")


def apply_task_reply(reply: Reply, message: str, store: TaskStore | None, now: datetime, *,
                     pending: Proposal | None = None, last_undo: UndoEntry | None = None,
                     events: Mapping[str, Event] | None = None) -> TaskOutcome:
    if not isinstance(reply, TaskReply):
        return TaskOutcome(reply, memory=reply.reply)
    action = reply.action
    chinese = bool(re.search(r"[\u3400-\u9fff]", message))
    if (action.mode == "none" and not action.operations) or action.mode == "lookup":
        # A lookup the app could not satisfy is just a reply; the calendar was never changed.
        return TaskOutcome(Reply(reply.reply, reply.expression), memory=reply.reply)
    if store is None:
        raise TaskError("Local task storage is unavailable. Nothing was changed.")
    voice = Reply(reply.reply, reply.expression)

    if action.mode == "cancel_pending":
        return TaskOutcome(voice, memory=f"{reply.reply}\n[App: the pending proposal was cancelled; nothing changed.]")
    if action.mode == "none":
        reads = [op for op in action.operations if op.kind in READ_KINDS]
        if reads:
            return read_reply(reads[0], voice, message, store, now, chinese)
        return TaskOutcome(voice, memory=reply.reply)

    if action.mode == "approve_pending":
        if pending is None:
            text = say(chinese, "目前没有待确认的操作。", "There is no pending change to confirm.")
            return TaskOutcome(Reply(text, reply.expression), memory=f"[App: {text}]")
        return approve(pending, store, now, chinese)

    if any(op.kind in READ_KINDS for op in action.operations) and all(
            op.kind in READ_KINDS for op in action.operations):
        return read_reply(action.operations[0], voice, message, store, now, chinese)
    if any(op.kind == "undo" for op in action.operations):
        if last_undo is None:
            text = say(chinese, "没有可以撤销的修改。", "There is no change to undo.")
            return TaskOutcome(Reply(text, reply.expression), memory=f"[App: {text}]")
        last_undo.run(store)
        text = say(chinese, "已撤销上一次修改。", "Undid the last change.")
        return TaskOutcome(Reply(text, reply.expression), changed=True, undone=True, memory=f"[App: {text}]")

    operations = tuple(op for op in action.operations if op.kind not in (*READ_KINDS, "lookup_events"))
    if not operations:
        return TaskOutcome(voice, memory=reply.reply)
    stage = action.mode == "propose" or any(op.kind in STAGED_ONLY_KINDS for op in operations)
    if stage:
        ready, errors = [], []
        for op in operations:
            try:
                ready.append(prepare(op, store, now.date(), chinese, events))
            except TaskError as error:
                errors.append(str(error))
        if not ready:
            text = report(Applied(errors=errors), chinese)
            return TaskOutcome(Reply(text, reply.expression), memory=f"{reply.reply}\n[App: {text}]")
        proposal = Proposal(tuple(ready))
        text = f"{reply.reply}\n{proposal.describe(chinese)}"
        if errors:
            text += "\n" + report(Applied(errors=errors), chinese)
        return TaskOutcome(
            Reply(text, reply.expression), proposal=proposal,
            memory=f"{reply.reply}\n[App: waiting for the producer's approval to:\n{proposal.describe(False)}]")

    applied, changed = commit(operations, store, now, chinese, events)
    text = report(applied, chinese)
    shown = text if applied.errors else f"{reply.reply}\n{text}"
    return TaskOutcome(Reply(shown, reply.expression), changed,
                       undo=UndoEntry(tuple(applied.steps)) if applied.steps else None,
                       memory=f"{reply.reply}\n[App result: {text}]")
