import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QThread

from calendar_reader import CalendarDay, CalendarError, CalendarReader
from gmail_reader import GmailError, GmailReader
from mail_actions import CONTENT_LIMIT, SCAN_DAYS, SCAN_LIMIT, MailAction, MailScan, topic_match
from tasks import Task, deadline_order

WORDS = {
    "brief": ("Daily Brief", "每日簡報", "每日简报"),
    "mail": ("Mail to follow up", "待跟進郵件", "待跟进邮件"),
    "review": ("Review", "待檢視", "待查看"),
    "availability": ("Confirm availability", "確認可面試時間", "确认可面试时间"),
    "interview": ("Schedule interview", "安排面試", "安排面试"),
    "resume": ("Send résumé", "寄送履歷", "发送简历"),
    "assessment": ("Complete assessment", "完成測驗", "完成测验"),
    "reply": ("Reply", "回覆", "回复"),
    "deadline": ("Deadline", "截止時間", "截止时间"),
    "quote": ("Deadline as written", "郵件原文時間", "邮件原文时间"),
    "not_connected": ("Gmail not connected. Connect Gmail in Today.", "尚未連接 Gmail。請在 Today 連接。",
                      "尚未连接 Gmail。请在 Today 连接。"),
    "scan": ("{count} candidate threads; {checked} checked for meaning over {days} local days (read and unread).",
             "最近 {days} 個本地日：{count} 個候選郵件串，已檢查 {checked} 個（已讀及未讀）。",
             "最近 {days} 个本地日：{count} 个候选邮件串，已检查 {checked} 个（已读及未读）。"),
    "candidate_cap": ("Candidate search capped ({limit}).", "已達候選上限（{limit}）。", "已达候选上限（{limit}）。"),
    "content_cap": ("Content checks capped ({limit}).", "已達內容檢查上限（{limit}）。", "已达内容检查上限（{limit}）。"),
    "exhausted": ("Candidate search exhausted.", "候選搜尋已完成。", "候选搜索已完成。"),
    "partial": ("Partial Gmail results ({count} failed requests).", "Gmail 結果不完整（{count} 個請求失敗）。",
                "Gmail 结果不完整（{count} 个请求失败）。"),
    "fallback": ("Rules-only fallback.", "僅使用保守規則。", "仅使用保守规则。"),
    "local_classifier": ("On-device classifier.", "裝置端分類器。", "设备端分类器。"),
    "update": ("Commitment changed", "行程或承諾有變更", "日程或承诺有变更"),
    "updates": ("Upcoming changes", "即將發生的變更", "即将发生的变更"),
    "next_action": ("Next action or date is unclear", "下一步或日期不明", "下一步或日期不明"),
    "reply_state": ("Reply status is unclear", "回覆狀態不明", "回复状态不明"),
    "remaining": ("{count} more in Today's mail follow-up view.", "Today 的待跟進郵件另有 {count} 項。",
                  "Today 的待跟进邮件另有 {count} 项。"),
    "next_calendar": ("Next step (Calendar)", "下一步（行事曆）", "下一步（日历）"),
    "mail_link": ("Linked Gmail source in Today", "可在 Today 開啟相關 Gmail 郵件", "可在 Today 打开相关 Gmail 邮件"),
    "none": ("No confirmed actions found in the checked threads.", "已檢查的郵件串沒有確認的待辦行動。",
             "已检查的邮件串没有确认的待办行动。"),
    "stale": ("Stale Gmail results", "Gmail 結果已過期", "Gmail 结果已过期"),
    "next_mail": ("Next step (Gmail, not scheduled)", "下一步（Gmail，未排程）", "下一步（Gmail，未排程）"),
    "next_local": ("Next step (Tasks)", "下一步（任務）", "下一步（任务）"),
    "overdue": ("Overdue", "已逾期", "已逾期"),
    "due": ("Due today", "今日到期", "今日到期"),
    "planned": ("Planned today", "今日安排", "今日安排"),
    "no_tasks": ("No overdue, due-today, or planned-today tasks.", "沒有逾期、今日到期或今日安排的任務。",
                 "没有逾期、今日到期或今日安排的任务。"),
    "more_tasks": ("{count} more in Today.", "Today 另有 {count} 項。", "Today 另有 {count} 项。"),
    "event": ("Next or ongoing event", "下一個或進行中的行程", "下一个或进行中的日程"),
    "all_day": ("All day", "全天", "全天"),
    "no_event": ("No upcoming calendar events.", "沒有接下來的行程。", "没有接下来的日程。"),
    "empty_calendar": ("No calendar events today.", "今天沒有行程。", "今天没有日程。"),
    "opening": ("Next opening", "下一個空檔", "下一个空档"),
    "no_opening": ("No remaining openings in this window.", "此時段沒有剩餘空檔。", "此时段没有剩余空档。"),
    "fits": ("Next task that fits", "下一項可安排的任務", "下一项可安排的任务"),
    "no_fit": ("No estimated task fits in the remaining openings.", "剩餘空檔無法安排已估時的任務。",
               "剩余空档无法安排已估时的任务。"),
    "calendar_error": ("Calendar unavailable", "無法取得行事曆", "无法取得日历"),
}


def word(key: str, lang: str) -> str:
    return WORDS[key][{"en": 0, "zh-Hant": 1, "zh-Hans": 2}.get(lang, 0)]


def brief_command(message: str, preferred: str = "en") -> tuple[str, str] | None:
    text = " ".join(message.casefold().replace("’", "'").split()).rstrip("?!。.？！").strip()
    if text in {"brief", "daily brief", "briefing"}:
        return "brief", preferred
    if re.fullmatch(r"(?:(?:(?:give|show) me (?:my |the |a )?|what(?:'s| is) in my )daily brief(?:ing)?"
                    r"|show me today's briefing)(?: please)?", text):
        return "brief", "en"
    if re.fullmatch(r"(?:any |which |what )?(?:e-?mails?)(?: do)? i (?:need|have) to (?:follow up on|reply to)", text):
        return "mail", "en"
    if text in {"今天有什麼要處理", "今天有什麼需要處理", "今日簡報", "每日簡報"}:
        return "brief", "zh-Hant"
    if text in {"今天有什么要处理", "今天有什么需要处理", "今日简报", "每日简报"}:
        return "brief", "zh-Hans"
    if text in {"有哪些信要回", "有哪些郵件要回", "有哪些信要跟進"}:
        return "mail", "zh-Hant"
    if text in {"有哪些邮件要回", "有哪些邮件要跟进"}:
        return "mail", "zh-Hans"
    return None


def action_label(action: MailAction, lang: str) -> str:
    return word(action.kind, lang)


def action_line(action: MailAction, lang: str) -> str:
    text = f"{action_label(action, lang)} — {action.sender} | {action.subject}"
    if action.deadline:
        text += f" | {word('deadline', lang)}: {action.deadline}"
    elif action.deadline_quote:
        text += f" | {word('quote', lang)}: “{action.deadline_quote}”"
    if action.review:
        text += f" | {word(action.missing or 'reply_state', lang)}"
    if action.status:
        text += f" | “{action.excerpt}”"
    return text


def action_source(action: MailAction) -> str:
    return hashlib.sha256(f"gmail:{action.message_id}:{action.kind}".encode()).hexdigest()


def scan_text(scan: MailScan, lang: str) -> str:
    lines = [word("scan", lang).format(count=scan.scanned, checked=scan.checked, days=SCAN_DAYS)]
    if scan.limited:
        lines.append(word("candidate_cap", lang).format(limit=scan.candidate_limit))
    elif not scan.failures:
        lines.append(word("exhausted", lang))
    if scan.content_capped:
        lines.append(word("content_cap", lang).format(limit=scan.content_limit))
    if scan.failures:
        lines.append(word("partial", lang).format(count=scan.failures))
    lines.append(word("local_classifier" if scan.semantic else "fallback", lang))
    return " ".join(lines)


def mail_text(scan: MailScan | None, status: str, lang: str, *, stale: bool = False,
              tasks: list[Task] | None = None, snapshot: CalendarDay | None = None,
              compact: bool = False, lead: MailAction | None = None) -> str:
    lines = [word("mail", lang)]
    if stale:
        lines.append(word("stale", lang))
    if status:
        lines.append(status)
    if scan is not None:
        lines.append(scan_text(scan, lang))
        confirmed = [item for item in scan.actions if not item.review and not item.status]
        shown = [item for item in confirmed if item != lead and not merged_obligation(item, tasks or [], snapshot)]
        for item in shown[:3] if compact else confirmed:
            source = matching_source(item, tasks or [], snapshot)
            rendered = action_line(item, lang)
            lines.append(rendered + (f" | {word('mail_link', lang)}" if source else ""))
        if compact and any(merged_obligation(item, tasks or [], snapshot) for item in confirmed):
            lines.append(word("mail_link", lang))
        if not confirmed:
            lines.append(word("none", lang))
        if compact and len(shown) > 3:
            lines.append(word("remaining", lang).format(count=len(shown) - 3))
        if updates := [action_line(item, lang) for item in scan.actions if item.status]:
            lines.extend([word("updates", lang), *(updates[:2] if compact else updates)])
        if review := [action_line(item, lang) for item in scan.actions if item.review]:
            lines.extend([f"{word('review', lang)} ({len(review)})", *(review[:2] if compact else review)])
    return "\n".join(lines)


def matching_source(item: MailAction, tasks: list[Task], snapshot: CalendarDay | None) -> str:
    for task in tasks:
        if topic_match(item.subject, task.title):
            return f"#{task.id}"
    for event in snapshot.events if snapshot and not snapshot.failures else ():
        if topic_match(item.subject, event.title):
            return event.title
    return ""


def merged_obligation(item: MailAction, tasks: list[Task], snapshot: CalendarDay | None) -> bool:
    if matching_source(item, tasks, None):
        return True
    # Calendar can settle scheduling, not a reply or a separate submission.
    return (item.kind == "interview" and bool(matching_source(item, [], snapshot))
            and not re.search(r"\b(?:rsvp|confirm|respond|reply|availability)\b", item.excerpt, re.I))


def next_step(now: datetime, tasks: list[Task], scan: MailScan | None, lang: str,
              snapshot: CalendarDay | None = None) -> tuple[str, MailAction | None]:
    actions = [item for item in scan.actions if not item.review and not item.status
               and not merged_obligation(item, tasks, snapshot)] if scan else []
    action = next((item for item in actions if item.deadline), None)
    local = min((task for task in tasks if task.deadline or task.planned_day == now.date().isoformat()),
                key=deadline_order, default=None)
    upcoming = min((event for event in snapshot.events if event.end.timestamp() > now.timestamp()
                    and event.start.timestamp() <= now.timestamp() + 3600),
                   key=lambda event: event.start.timestamp(), default=None) if snapshot and not snapshot.failures else None
    if upcoming and (local is None or upcoming.start.replace(tzinfo=None) < deadline_order(local)) and (
            action is None or upcoming.start.replace(tzinfo=None) < datetime.fromisoformat(action.deadline)):
        return f"{word('next_calendar', lang)}: {upcoming.start:%H:%M}", None
    if action:
        stamp = datetime.fromisoformat(action.deadline)
        if len(action.deadline) == 10:
            stamp = stamp.replace(hour=23, minute=59)
        if stamp <= now.replace(tzinfo=None) + timedelta(days=1) and (local is None or stamp < deadline_order(local)):
            return f"{word('next_mail', lang)}: {action_line(action, lang)}", action
    if local:
        return f"{word('next_local', lang)}: #{local.id} {local.title}", None
    if actions:
        return f"{word('next_mail', lang)}: {action_line(actions[0], lang)}", actions[0]
    return "", None


class DailyBriefRequest(QThread):
    def __init__(self, now: datetime, zone: ZoneInfo, calendar: CalendarReader, gmail: GmailReader,
                 cached: MailScan | None, *, include_calendar: bool, context: tuple[str, ...] = (),
                 candidate_limit: int = SCAN_LIMIT, content_limit: int = CONTENT_LIMIT, semantic: bool = True) -> None:
        super().__init__()
        self.now, self.zone, self.calendar, self.gmail, self.cached = now, zone, calendar, gmail, cached
        self.include_calendar = include_calendar
        self.snapshot: CalendarDay | None = None
        self.calendar_error = ""
        self.scan: MailScan | None = cached
        self.mail_error = ""
        self.disconnected = False
        self.context, self.candidate_limit, self.content_limit, self.semantic = context, candidate_limit, content_limit, semantic

    def run(self) -> None:
        zone = self.zone
        if self.include_calendar:
            try:
                self.snapshot = self.calendar.read(self.now.date(), zone, reuse=True)
                if self.snapshot.failures:
                    self.calendar_error = "Calendar read incomplete. Refresh in Today."
            except CalendarError as error:
                self.calendar_error = str(error)
        if not self.gmail.token.is_file():
            self.scan = None
            self.disconnected = True
        elif self.cached is None:
            try:
                event_titles = tuple(event.title for event in self.snapshot.events) if self.snapshot else ()
                self.scan = self.gmail.scan_actions(self.now, zone, candidate_limit=self.candidate_limit,
                                                   content_limit=self.content_limit, semantic=self.semantic,
                                                   context=self.context + event_titles)
            except GmailError as error:
                self.mail_error = str(error)
