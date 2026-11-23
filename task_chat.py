from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import re

from dialogue import Reply
from tasks import TaskError, TaskStore, deadline_value, plan_text, plan_today, task_line

ACTION_KINDS = ("none", "list", "plan", "add", "complete", "reschedule")
ACTION_FIELDS = ("kind", "title", "target", "date", "field", "minutes")
TASK_INSTRUCTIONS = """
Return one task action with your reply. Use kind=none for ordinary chat, hypothetical or
casual mentions, and non-task questions. Use list for a request for open tasks and plan for
prioritizing today's tasks. For an explicit request to add one local task, use add and copy its
title literally from the latest user message. Copy any deadline as a literal date phrase
without 'due', 'before', or '前'. Copy the estimate as digits with no units. For a request to
mark one local task complete, use complete and copy its title or #ID into target. For a
request to reschedule one local task, use reschedule and copy the target and date literally.
Only set field to planned_day or deadline if the user specifically named that field; otherwise
leave it blank. Use empty strings for unused fields. Do not assume that any action succeeded.
The app resolves tasks, writes changes, and calculates plans locally. Do not invent local data.
If proposing one concrete change instead of executing a direct instruction, set confirm=true
and fill in the operation's fields. A proposal never writes until the user approves it.
"""


@dataclass(frozen=True)
class TaskAction:
    kind: str
    title: str = ""
    target: str = ""
    date: str = ""
    field: str = ""
    minutes: str = ""
    confirm: bool = False

    @classmethod
    def parse(cls, payload: object) -> "TaskAction":
        if not isinstance(payload, dict) or set(payload) - {"confirm"} != set(ACTION_FIELDS):
            raise TaskError("Claude returned an invalid task action. Nothing was changed.")
        if any(not isinstance(payload[key], str) for key in ACTION_FIELDS):
            raise TaskError("Claude returned an invalid task action. Nothing was changed.")
        confirm = payload.get("confirm", False)
        if not isinstance(confirm, bool):
            raise TaskError("Claude returned an invalid confirmation flag. Nothing was changed.")
        action = cls(**{key: payload[key].strip() for key in ACTION_FIELDS}, confirm=confirm)
        if action.kind not in ACTION_KINDS or action.field not in ("", "deadline", "planned_day"):
            raise TaskError("Claude returned an invalid task action. Nothing was changed.")
        return action


@dataclass(frozen=True)
class TaskReply(Reply):
    action: TaskAction


@dataclass(frozen=True)
class PendingAction:
    kind: str
    title: str
    task_id: int | None = None
    day: str = ""
    field: str = ""
    minutes: str = ""

    def description(self, chinese: bool) -> str:
        if self.kind == "add":
            details = (f", {self.minutes} min" if self.minutes else "") + (f", due {self.day}" if self.day else "")
            if chinese:
                details = (f"，{self.minutes} 分鐘" if self.minutes else "") + (f"，截止 {self.day}" if self.day else "")
            return f"新增「{self.title}」{details}" if chinese else f"add “{self.title}”{details}"
        target = f"#{self.task_id} {self.title}"
        if self.kind == "complete":
            return f"完成 {target}" if chinese else f"complete {target}"
        field = ("截止日期" if self.field == "deadline" else "計畫日期") if chinese else self.field.replace("_", " ")
        return f"將 {target} 的{field}改到 {self.day}" if chinese else f"move the {field} for {target} to {self.day}"

    def commit(self, store: TaskStore, now: datetime, chinese: bool) -> str:
        if self.kind == "add":
            task_id = store.add(self.title, self.day, self.minutes, "", today=now.date())
            label = f"#{task_id} {self.title}"
            if chinese:
                return f"已添加 {label}。" + (f"截止日期：{self.day}。" if self.day else "")
            return f"Saved {label}" + (f" (due {self.day})." if self.day else ".")
        assert self.task_id is not None
        if self.kind == "complete":
            store.complete(self.task_id)
            return f"已完成 #{self.task_id} {self.title}。" if chinese else f"Completed #{self.task_id} {self.title}."
        store.schedule(self.task_id, self.field, self.day, today=now.date())
        return f"已更新：{self.description(True)}。" if chinese else f"Updated: {self.description(False)}."


COMMANDS = {
    "add": r"add\b|create\b|帮我加|添加|新增",
    "complete": r"complete\b|mark\b.+\b(?:done|complete|completed)\b|帮我完成|(?:帮我)?把.+(?:标为|标记为)完成",
    "reschedule": r"move\b|reschedule\b|(?:change|update|set)\b|(?:帮我)?把.+(?:改|移|挪)|(?:帮我)?(?:调整|移动)",
}

COMMAND_PREFIX = r"^\s*(?:(?:please|can you|could you|would you)\s+|请\s*)*"


def matches_command(command: str, message: str) -> bool:
    return re.match(COMMAND_PREFIX + "(?:" + command + ")", message, re.IGNORECASE) is not None


def stated(value: str, message: str) -> bool:
    return bool(value) and value.casefold() in message.casefold()


def time_budget(message: str) -> int | None:
    duration = re.search(r"(?:([0-9]{1,9})\s*(?:hours?\b|小时|个小时|min(?:ute)?s?\b|分钟)|一[个]?小时|半[个]?小时|(?:an|one) hour\b)", message.casefold())
    if duration is None:
        return None
    text = duration[0]
    if duration[1]:
        return int(duration[1]) * (60 if "hour" in text or "小时" in text else 1)
    return 30 if "半" in text else 60


def apply_task_reply(reply: Reply, message: str, store: TaskStore | None,
                     now: datetime, *, propose: Callable[[PendingAction], None] | None = None) -> tuple[Reply, bool]:
    if not isinstance(reply, TaskReply) or reply.action.kind == "none":
        return reply, False
    action = reply.action
    chinese = bool(re.search(r"[\u3400-\u9fff]", message))

    def result(english: str, chinese_text: str, *, changed: bool = False) -> tuple[Reply, bool]:
        return Reply(chinese_text if chinese else english, reply.expression), changed

    if store is None:
        raise TaskError("Local task storage is unavailable. Nothing was changed.")

    def perform(operation: PendingAction) -> tuple[Reply, bool]:
        if action.confirm:
            if propose is None:
                raise TaskError("No confirmation session is available. Nothing was changed.")
            propose(operation)
            return result(f"Should I {operation.description(False)}? Nothing has changed yet.",
                          f"要{operation.description(True)}嗎？目前尚未變更。")
        return Reply(operation.commit(store, now, chinese), reply.expression), True

    if action.kind == "list":
        tasks = store.open_tasks()
        if not tasks:
            return result("There are no open tasks.", "没有未完成的任务。")
        listing = "\n" + "\n".join(task_line(task) for task in tasks)
        return result("Open tasks:" + listing, "未完成的任务：" + listing)
    if action.kind == "plan":
        budget = time_budget(message)
        if budget is None:
            return result("How many minutes do you have today?", "你今天有多少分钟可以安排？")
        plan = plan_today(store.open_tasks(), budget, now)
        return Reply(plan_text(plan, chinese=chinese), reply.expression), False

    command = COMMANDS.get(action.kind)
    if command is None or (not action.confirm and not matches_command(command, message)):
        return result("No changes made. To update a task, give a direct instruction and name the task.",
                      "没有修改任务。如需更新，请明确说明操作和任务名称。")
    today = now.date()
    if action.kind == "add":
        if not action.confirm and (not stated(action.title, message) or (action.date and not stated(action.date, message))):
            return result("Please specify the exact title and any deadline to save.", "请明确要保存的任务名称和截止日期。")
        if not action.confirm and action.minutes and not re.search(
            r"(?<![\d.-])" + re.escape(action.minutes) + r"\s*(?:min(?:ute)?s?\b|分钟)", message, re.IGNORECASE
        ):
            return result("Please specify the estimate in minutes.", "请明确预计需要多少分钟。")
        if not action.title.strip():
            raise TaskError("Enter a task title.")
        due, minutes, _ = store.fields(action.date, action.minutes, "", today)
        return perform(PendingAction("add", action.title, day=due or "",
                                     minutes=str(minutes) if minutes is not None else ""))

    if action.kind == "reschedule":
        due = bool(re.search(r"\b(?:deadline|due date)\b|截止", message, re.IGNORECASE))
        planned = bool(re.search(r"\b(?:planned day|planned date|scheduled day)\b|计划(?:日期|日)|安排(?:日期|日)", message, re.IGNORECASE))
        if not action.field or (not action.confirm and (due == planned or action.field != ("deadline" if due else "planned_day"))):
            return result("Do you mean the planned day or the deadline?", "你想改计划日期，还是截止日期？")

    number = re.fullmatch(r"#([0-9]{1,19})", action.target)
    grounded = bool(re.search(re.escape(action.target) + r"(?![0-9])", message)) if number else stated(action.target, message)
    if not action.target or (not action.confirm and not grounded):
        return result("Which open task? Give its title or #ID.", "是哪一项未完成的任务？请提供名称或 #编号。")
    tasks = store.open_tasks()
    if number:
        matches = [task for task in tasks if task.id == int(number[1])]
    else:
        matches = [task for task in tasks if action.target.casefold() in task.title.casefold()]
    if len(matches) != 1:
        if len(matches) > 1:
            choices = "\n" + "\n".join(task_line(task) for task in matches)
            return result("Which task did you mean? Repeat the request with its #ID." + choices,
                          "你指的是哪一项？请用 #编号 重述请求。" + choices)
        return result("No open task matched. Give its full title or #ID.", "没有找到匹配的未完成任务。请给出完整名称或 #编号。")
    task = matches[0]
    if action.kind == "complete":
        return perform(PendingAction("complete", task.title, task.id))
    if not action.date or (not action.confirm and not stated(action.date, message)):
        return result("What date should I use?", "要改到哪一天？")
    if action.field == "deadline":
        new_date = deadline_value(action.date, today)
    else:
        _, _, new_date = store.fields("", "", action.date, today)
    assert new_date is not None
    return perform(PendingAction("reschedule", task.title, task.id, new_date, action.field))
