import os
from dataclasses import replace
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from datetime import UTC, date, datetime, timedelta
import sys

from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, QSettings, QSize, QStandardPaths, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QFont, QImage, QPainter, QPainterPath, QPaintEvent, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSizePolicy, QSplitter, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)
from tzlocal import get_localzone

from availability import openings, place_tasks, slot_intervals
from calendar_chat import CalendarContext, CalendarQuery, calendar_answer, parse_calendar_query, planning_time, validate_window
from calendar_masks import listing as event_listing, lookup_days, mask_event, plan_events
from calendar_reader import CalendarDay, CalendarError, CalendarReader, Event, SNAPSHOT_MAX_AGE, apply_masks, event_key
from calendar_view import CalendarRefresh, CalendarView
from chat_view import ChatView
from claude_reply import ClaudeError, HISTORY_TURNS, load_settings, request_reply
from dialogue import Expression, Reply, respond
from daily_brief import (
    DailyBriefRequest, action_label, action_line, action_source, brief_command, mail_text,
    matching_source, next_step, scan_text, word,
)
from desktop_today import briefing_text, collect_reminders
from desktop_style import PAGE_STYLE, install_desktop_theme, tame_list
from gmail_reader import GmailReader, chat_mail_kind
from gmail_view import MailView
from mail_actions import CONTENT_LIMIT, SCAN_LIMIT, MailAction, MailScan
from office_view import OfficeView, SceneButton, office_icon
from screenshot_ocr import OcrConfig, ScreenshotOcrLineEdit
from task_chat import (
    Operation, Proposal, TaskReply, UndoEntry, apply_task_reply, approve, task_context, time_budget,
    with_calendar_events,
)
from task_views import TaskView, TodayView
from tasks import Task, TaskError, TaskStore, is_due, minutes_value, plan_text, plan_today, task_fields

CANVAS = QSize(640, 855)
WELCOME = "制作人，今天想从哪件事开始？"


def ocr_language(executable: Path) -> str:
    """Use only the Tesseract language data that is actually installed."""
    folders = [Path(os.environ["TESSDATA_PREFIX"])] if os.environ.get("TESSDATA_PREFIX") else []
    folders.append(executable.parent / "tessdata")
    for folder in folders:
        found = [name for name in ("eng", "chi_sim", "chi_tra") if (folder / f"{name}.traineddata").is_file()]
        if found:
            return "+".join(found)
    return "eng"


def personal_art_dir() -> Path:
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "Chihaya" / "portraits"
    root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(root) / "portraits"


class Portrait(QWidget):
    expression_changed = Signal()

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.setMinimumSize(215, 290)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.folder = folder
        self.expression: Expression = "neutral"
        self.artwork: QPixmap | None = None
        self.set_expression("neutral")

    def set_expression(self, expression: Expression) -> None:
        self.expression = expression
        self.artwork = None
        self.setToolTip("正在使用内置占位立绘。个人立绘文件夹：" + str(self.folder))
        for name in dict.fromkeys((expression, "neutral")):
            path = self.folder / f"{name}.png"
            if path.is_file():
                image = QPixmap(str(path))
                if image.size() == CANVAS:
                    self.artwork = image
                    self.setToolTip(f"立绘：{name}")
                    break
        self.setAccessibleName(f"立绘：{expression}")
        self.update()
        self.expression_changed.emit()

    def canvas_rect(self) -> QRectF:
        margin = 12
        scale = min((self.width() - 2 * margin) / CANVAS.width(),
                    (self.height() - 2 * margin) / CANVAS.height())
        width, height = CANVAS.width() * scale, CANVAS.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing |
                               QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e8eef9"))
        painter.drawRoundedRect(QRectF(self.rect()), 14, 14)
        self.paint_art(painter, self.canvas_rect())
        painter.end()

    def paint_art(self, painter: QPainter, target: QRectF, *, badge: bool = True) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        if self.artwork is not None:
            painter.drawPixmap(target, self.artwork, QRectF(self.artwork.rect()))
        else:
            painter.translate(target.topLeft())
            painter.scale(target.width() / CANVAS.width(), target.height() / CANVAS.height())
            self.draw_placeholder(painter, badge=badge)
        painter.restore()

    def silhouette_image(self) -> QImage:
        """The portrait on a transparent canvas, used to trace the hover outline."""
        if self.artwork is not None:
            return self.artwork.toImage()
        image = QImage(CANVAS, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        self.draw_placeholder(painter, badge=False)
        painter.end()
        return image

    def draw_placeholder(self, painter: QPainter, *, badge: bool = True) -> None:
        if badge:
            painter.setBrush(QColor("#cbd8f0"))
            painter.drawEllipse(QRectF(75, 120, 490, 490))
        painter.setBrush(QColor("#334778"))
        painter.drawRoundedRect(QRectF(175, 190, 290, 400), 120, 120)
        painter.setBrush(QColor("#486db0"))
        painter.drawRoundedRect(QRectF(104, 499, 432, 225), 80, 80)
        painter.setBrush(QColor("#ffddc9"))
        painter.drawRoundedRect(QRectF(208, 250, 224, 220), 88, 88)
        painter.setBrush(QColor("#25365e"))
        for x in (266, 374):
            painter.drawEllipse(QPointF(x, 347), 9, 11)
        pen = QPen(QColor("#25365e"))
        pen.setWidth(7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.expression == "focused":
            painter.drawLine(QPointF(249, 316), QPointF(280, 325))
            painter.drawLine(QPointF(360, 325), QPointF(391, 316))
        mouth = QPainterPath(QPointF(295, 400))
        if self.expression == "smile":
            mouth.quadTo(320, 434, 345, 400)
        else:
            mouth.lineTo(345, 400)
        painter.drawPath(mouth)
        if badge:
            painter.setFont(QFont("Segoe UI", 23))
            painter.drawText(QRectF(0, 749, 640, 50), Qt.AlignmentFlag.AlignCenter, "Chihaya")


class ClaudeRequest(QThread):
    def __init__(self, data_dir: Path, message: str, history: tuple[tuple[str, str], ...]) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.message = message
        self.history = history
        self.task_context: str | None = None
        self.pending: Proposal | None = None
        self.lookup_source: tuple[CalendarReader, ZoneInfo] | None = None
        self.events: dict[str, Event] = {}
        self.reply: Reply | None = None
        self.error = ""
        self.calendar: tuple[CalendarQuery, CalendarReader, ZoneInfo, TaskStore | None, bool] | None = None
        self.calendar_context: CalendarContext | None = None
        self.safe_calendar_reply = ""

    def run(self) -> None:
        try:
            if self.calendar is not None:
                query, reader, zone, store, use_claude = self.calendar
                try:
                    selected = self.calendar_context.selected if self.calendar_context else reader.selected()
                except CalendarError:
                    selected = None
                if selected is not None:
                    self.calendar_context = CalendarContext(query, zone, selected)
                summary, model_summary = calendar_answer(query, reader, zone, store, selected=selected)
                self.safe_calendar_reply = model_summary or "日历空档暂时不可用。"
                self.reply = Reply(summary, "focused")
                if use_claude and model_summary is not None:
                    try:
                        explanation = request_reply(self.data_dir, self.message, self.history,
                                                    calendar_summary=model_summary)
                    except ClaudeError as error:
                        self.error = str(error)
                    else:
                        self.reply = Reply(f"{explanation.reply}\n\n{summary}", explanation.expression)
                return
            reply = request_reply(self.data_dir, self.message, self.history, task_context=self.task_context)
            if isinstance(reply, TaskReply) and reply.action.mode == "lookup" and self.lookup_source is not None:
                reply = self.answer_lookup(reply)
            self.reply = reply
        except (CalendarError, ClaudeError, TaskError) as error:
            self.error = str(error)

    def answer_lookup(self, first: TaskReply) -> Reply:
        """The model asked to see calendar days. Read them here and ask once more; event details stay local."""
        assert self.lookup_source is not None
        reader, zone = self.lookup_source
        today = datetime.now(zone).date()
        days = lookup_days([op.date for op in first.action.operations if op.kind == "lookup_events"], today)
        shown: dict[str, object]
        try:
            if not days:
                raise CalendarError("没有收到有效的日期。")
            snapshots = [reader.read(day, zone, reuse=True) for day in days]
            shown, self.events = event_listing(snapshots, zone)
            if any(snapshot.failures for snapshot in snapshots):
                shown["note"] = "Some selected calendars could not be read, so these days may be incomplete."
        except CalendarError as error:
            shown = {"unavailable": str(error)}
        return request_reply(self.data_dir, self.message, self.history,
                             task_context=with_calendar_events(self.task_context, shown))


class MainWindow(QMainWindow):
    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.setWindowTitle("Chihaya Desktop")
        self.resize(1100, 680)
        self.data_dir = folder.parent
        self.claude_history: list[tuple[str, str]] = []
        self.calendar_context: CalendarContext | None = None
        self.pending_action: Proposal | None = None
        self.last_undo: UndoEntry | None = None
        self.request: ClaudeRequest | None = None
        self.mail_chat_pending = False
        self.mail_chat_results: set[str] = set()
        self.store: TaskStore | None = None
        task_error = ""
        try:
            self.store = TaskStore(self.data_dir / "tasks.sqlite3")
        except TaskError as error:
            task_error = str(error)

        self.portrait = Portrait(folder)
        self.mode = QComboBox()
        self.mode.setAccessibleName("回复模式")
        self.mode.addItems(["离线", "Claude"])
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.clear_button = QPushButton("清除对话")
        self.clear_button.clicked.connect(self.clear_conversation)
        self.status = QLabel("离线模式，不会发送任何消息。")
        self.status.setAccessibleName("连接状态")
        self.status.setWordWrap(True)
        self.history = ChatView()
        self.history.add_message("chihaya", WELCOME)
        installed_ocr = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"
        self.entry = ScreenshotOcrLineEdit(
            ocr=OcrConfig(executable=str(installed_ocr) if installed_ocr.is_file() else "tesseract",
                          language=ocr_language(installed_ocr) if installed_ocr.is_file() else "eng"),
            stage_task=self.stage_screenshot_task, placeholderText="输入消息…")
        self.entry.setAccessibleName("消息")
        self.send_button = QPushButton("发送")
        self.send_button.setProperty("primary", True)
        self.entry.returnPressed.connect(self.send)
        self.send_button.clicked.connect(self.send)

        composer_panel = QWidget()
        composer_panel.setObjectName("chatComposer")
        composer = QHBoxLayout(composer_panel)
        composer.setContentsMargins(8, 2, 8, 2)
        self.expand_chat = QPushButton("对话记录")
        self.expand_chat.clicked.connect(self.toggle_chat)
        composer.addWidget(self.expand_chat)
        composer.addWidget(self.entry, 1)
        screenshot = QPushButton("选择截图")
        screenshot.clicked.connect(self.entry.choose_image)
        composer.addWidget(screenshot)
        composer.addWidget(self.send_button)
        self.conversation = QGroupBox("与千早对话")
        self.conversation.setObjectName("conversation")
        chat = QVBoxLayout(self.conversation)
        chat.setContentsMargins(12, 22, 12, 10)
        chat_top = QHBoxLayout()
        chat_top.addWidget(self.status, 1)
        chat_top.addWidget(self.clear_button, alignment=Qt.AlignmentFlag.AlignTop)
        chat.addLayout(chat_top)
        chat.addWidget(self.history, 1)
        self.portrait.hide()
        self.portrait.setParent(self)
        self.tabs = QTabWidget()
        self.tasks = TaskView(self.store, task_error)
        self.calendar = CalendarView(CalendarReader(self.data_dir), get_localzone())
        if self.store is not None:
            self.calendar.reader.mask_keys = self.store.event_mask_keys
            self.calendar.plan_source = self.calendar_plans
        self.calendar.mask_requested.connect(self.toggle_event_mask)
        self.today = TodayView(self.store, task_error, self.calendar)
        self.today.due_list.itemActivated.connect(self.open_due_task)
        self.brief_links = QListWidget()
        self.brief_links.setAccessibleName("今日简报链接")
        self.brief_links.setMaximumHeight(160)
        tame_list(self.brief_links)
        self.brief_links.itemActivated.connect(self.open_brief_link)
        self.brief_links.itemClicked.connect(self.open_brief_link)
        self.settings = QSettings(str(self.data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.mail = MailView(GmailReader(self.data_dir), self.calendar.zone, self.settings)
        self.mail_page = QWidget()
        mail_layout = QVBoxLayout(self.mail_page)
        mail_layout.setContentsMargins(10, 8, 10, 8)
        self.daily_worker: DailyBriefRequest | None = None
        self.daily_scan: MailScan | None = None
        self.daily_raw: MailScan | None = None
        self.hidden_mail = 0
        self.daily_error = ""
        self.daily_chat: tuple[str, str, str] | None = None
        self.daily_items: tuple[MailAction, ...] = ()
        self.brief_refresh = QPushButton("刷新今日简报")
        self.setup_daily_mail()
        mail_split = QSplitter(Qt.Orientation.Vertical)
        mail_split.setChildrenCollapsible(False)
        mail_split.addWidget(self.mail)
        mail_split.addWidget(self.daily_section)
        mail_split.setStretchFactor(0, 3)
        mail_split.setStretchFactor(1, 2)
        mail_split.setSizes([360, 250])
        self.mail.rows.setMinimumHeight(180)
        self.mail.preview.setMinimumHeight(180)
        mail_layout.addWidget(mail_split)
        self.today.add_briefing_section(self.brief_links)
        self.brief_refresh.clicked.connect(lambda: self.start_daily_brief())
        self.today.add_briefing_section(self.brief_refresh)
        self.settings_page = QWidget()
        settings_layout = QVBoxLayout(self.settings_page)
        settings_layout.setContentsMargins(14, 10, 14, 10)
        self.masked_summary = QLabel()
        self.masked_summary.setWordWrap(True)
        self.masked_summary.setAccessibleName("本机取消的行程")
        self.restore_masks_button = QPushButton("恢复全部本机取消的行程")
        self.restore_masks_button.clicked.connect(self.restore_all_masks)
        for heading, controls in (
            ("Google 日历（只读）", (self.calendar.connect_button, QLabel("纳入的日历"), self.calendar.calendars,
                                  self.masked_summary, self.restore_masks_button)),
            ("Gmail（只读）", (self.mail.connect_button, self.mail.remove_button,
                              QLabel("求职邮件关键词"), self.mail.keywords)),
            ("对话与桌面", (QLabel("回复模式"), self.mode, self.today.briefing_enabled, self.today.reminders_enabled,
                            self.today.close_to_tray, self.today.reminders_button, self.today.reminder_status)),
        ):
            section = QGroupBox(heading)
            settings_layout.addWidget(section)
            section_layout = QVBoxLayout(section)
            for control in controls:
                section_layout.addWidget(control)
        settings_layout.addStretch()
        self.mail.chat_ready.connect(self.finish_mail_chat)
        self.mail.disconnected.connect(self.clear_mail_results)
        self.mail.reconnected.connect(self.clear_mail_results)
        self.mail.finished.connect(self.on_mail_finished)
        self.mail.started.connect(self.update_daily_buttons)
        self.mail.started.connect(self.update_office)
        self.briefing_worker: CalendarRefresh | None = None
        self.briefing_snapshot: CalendarDay | None = None
        self.briefing_status = ""
        self.briefing_last_attempt = float("-inf")
        self.quitting = False
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
        menu = QMenu(self)
        menu.addAction("打开", self.open_window)
        menu.addAction("今日简报", self.open_briefing)
        menu.addAction("退出", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Chihaya Desktop")
        self.tray.activated.connect(self.tray_activated)
        self.tray.messageClicked.connect(self.open_briefing)
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if tray_available:
            self.tray.show()
        self.today.close_to_tray.setEnabled(tray_available)
        self.today.close_to_tray.setToolTip("Windows 系统托盘不可用。" if not tray_available else
                                           "关闭窗口时缩到系统托盘，而不是退出。")
        for box, key, default in (
            (self.today.briefing_enabled, "briefing", True),
            (self.today.reminders_enabled, "reminders", False),
            (self.today.close_to_tray, "close_to_tray", False),
        ):
            box.setChecked(self.settings.value(key, default, type=bool))
            box.toggled.connect(lambda value, name=key: self.save_preference(name, value))
        self.today.reminders_button.clicked.connect(self.show_reminders)
        self.calendar.updated.connect(self.calendar_updated)
        self.calendar.started.connect(self.update_office)
        for field in (self.today.start, self.today.end, self.today.budget):
            field.editingFinished.connect(self.update_briefing)
        self.today.briefing.setVisible(self.today.briefing_enabled.isChecked())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(15_000)
        self.office = OfficeView(lambda painter, target: self.portrait.paint_art(painter, target, badge=False))
        self.office.mount_chat(composer_panel, self.conversation)
        self.office.opened.connect(self.navigate)
        self.portrait.expression_changed.connect(self.refresh_silhouette)
        self.refresh_silhouette()
        self.tabs.addTab(self.office, "事务所")
        self.tabs.addTab(self.today, "今日")
        self.tabs.addTab(self.tasks, "待办")
        self.tabs.addTab(self.mail_page, "信箱")
        self.tabs.addTab(self.settings_page, "设置")
        self.tabs.tabBar().hide()
        self.tabs.setCurrentWidget(self.office)
        self.office_launcher: QWidget | None = None
        self.office_event_day: date | None = None
        self.office_task_id: int | None = None
        self.calendar_target: date | None = None
        self.nav_buttons: dict[str, QPushButton] = {}
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.tabs, 1)
        nav_bar = QWidget()
        nav_bar.setObjectName("navBar")
        nav_bar.setStyleSheet("QWidget#navBar { background: #0a121d; border-top: 1px solid #243a52; }")
        nav = QHBoxLayout(nav_bar)
        nav.setContentsMargins(9, 6, 9, 6)
        for route, label in (("office", "事务所"), ("today", "今日"), ("tasks", "待办"),
                             ("mail", "信箱"), ("settings", "设置")):
            button = SceneButton(label)
            button.setAccessibleName(label)
            button.setMinimumHeight(36)
            button.setProperty("navigation", True)
            button.setIconSize(QSize(21, 21))
            button.setStyleSheet("QPushButton { border: 1px solid #35506e; border-radius: 8px; background: #152233;"
                                 " color: #dbe8f5; font-weight: 600; }"
                                 "QPushButton:hover { background: #20364f; border-color: #6aa6d8; }"
                                 "QPushButton[current=\"true\"] { background: #2a6fb0; border-color: #9fd0f7;"
                                 " color: white; }"
                                 "QPushButton:focus { border: 2px solid #8cc9f5; }")
            button.clicked.connect(lambda checked=False, target=route: self.navigate(target))
            self.nav_buttons[route] = button
            nav.addWidget(button, 1)
        shell_layout.addWidget(nav_bar)
        self.tabs.currentChanged.connect(self.refresh_tab)
        self.tasks.changed.connect(self.refresh_task_views)
        self.setCentralWidget(shell)
        self.setStyleSheet(PAGE_STYLE)
        self.escape_to_office = QShortcut(Qt.Key.Key_Escape, self)
        self.escape_to_office.activated.connect(self.return_to_office)
        self.office.buttons["clock"].setFocus()
        self.update_navigation()
        self.apply_event_masks()
        self.refresh_briefing()
        self.check_reminders()
        self.mode.setCurrentIndex(1)

    def show_conversation(self, expanded: bool) -> None:
        self.conversation.setVisible(expanded)
        self.expand_chat.setText("收起对话" if expanded else "对话记录")

    def refresh_silhouette(self) -> None:
        self.office.set_silhouette(self.portrait.silhouette_image())

    def stage_screenshot_task(self, title: str) -> None:
        self.show_conversation(True)
        if self.pending_action is not None or self.store is None:
            self.status.setText("请先处理待确认的操作，并确认本机待办可用。")
            return
        self.pending_action = Proposal((Operation("add", title=title),))
        self.history.add_message(
            "chihaya", f"确认{self.pending_action.describe(True).removeprefix('· ')}吗？请输入“确认”或“取消”。")

    def toggle_chat(self) -> None:
        self.show_conversation(self.conversation.isHidden())
        self.entry.setFocus()

    def return_to_office(self) -> None:
        if self.tabs.currentWidget() is self.office:
            self.show_conversation(False)
        self.navigate("office")

    @Slot(str, QWidget)
    def navigate(self, route: str, launcher: QWidget | None = None, *, task_id: int | None = None,
                 mail_rows: QListWidget | None = None) -> None:
        if launcher is not None and self.tabs.currentWidget() is self.office:
            self.office_launcher = launcher
        elif self.tabs.currentWidget() is self.office and route in self.nav_buttons and route != "office":
            self.office_launcher = self.nav_buttons[route]
        if route == "office":
            self.tabs.setCurrentWidget(self.office)
            self.update_office()
            (self.office_launcher or self.office.buttons["clock"]).setFocus()
            self.office_launcher = None
        elif route == "chat":
            self.tabs.setCurrentWidget(self.office)
            self.show_conversation(True)
            self.entry.setFocus()
        elif route == "settings" or launcher in (self.office.buttons["calendar_status"],
                                                  self.office.buttons["mail_status"]):
            self.tabs.setCurrentWidget(self.settings_page)
            (self.mail.connect_button if launcher is self.office.buttons["mail_status"]
             else self.calendar.connect_button).setFocus()
        elif route in ("mail", "follow"):
            self.tabs.setCurrentWidget(self.mail_page)
            self.mail.check_connection()
            if route == "mail" and launcher in (self.office.buttons["monitor"], self.office.buttons["unread"]):
                if self.mail.filter.currentData() == "jobs":
                    self.mail.filter.setCurrentIndex(self.mail.filter.findData("recent"))
            focus = self.mail.filter if route == "mail" else (
                mail_rows if mail_rows is not None else self.daily_rows)
            if route == "follow" and isinstance(focus, QListWidget):
                self.daily_tabs.setCurrentWidget(focus)
            focus.setFocus()
        elif route in ("task", "tasks"):
            self.tabs.setCurrentWidget(self.tasks)
            if route == "task":
                wanted = task_id if task_id is not None else self.office_task_id
                if wanted is not None:
                    self.tasks.select_task(wanted)
            self.tasks.focus_primary()
        else:
            focus: QWidget
            if route in ("calendar", "event"):
                if route == "event" or launcher is self.office.buttons["clock"]:
                    day = self.office_event_day if route == "event" else None
                    day = day or datetime.now(self.calendar.zone).date()
                    if self.calendar.worker is None:
                        self.calendar.day.setDate(QDate(day.year, day.month, day.day))
                    else:
                        self.calendar_target = day
                focus = self.calendar.day
            elif route == "due":
                self.today.refresh()
                self.today.show_panel(True)
                focus = self.today.due_list
            else:
                if route == "brief":
                    self.today.show_panel(True)
                focus = self.brief_links if route == "brief" else self.calendar.day
            self.tabs.setCurrentWidget(self.today)
            focus.setFocus()
            self.today.scroll.ensureWidgetVisible(focus, 0, 12)
            if route in ("brief", "today"):
                self.today.scroll.verticalScrollBar().setValue(0)
        self.update_navigation()

    def update_navigation(self) -> None:
        current = self.tabs.currentWidget()
        selected = ("office" if current is self.office else "settings" if current is self.settings_page else
                    "tasks" if current is self.tasks else "mail" if current is self.mail_page else "today")
        for route, button in self.nav_buttons.items():
            checked = route == selected
            button.setProperty("current", checked)
            button.setAccessibleDescription("当前页面" if checked else "")
            button.setProperty("tone", "blue" if checked else "")
            button.setIcon(office_icon(route, "#ffffff" if checked else "#9fc6ea"))
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def open_brief_link(self, item: QListWidgetItem) -> None:
        route, task_id = item.data(Qt.ItemDataRole.UserRole)
        if route == "mail-action":
            self.navigate("follow")
            self.daily_tabs.setCurrentWidget(self.daily_rows)
            for index in range(self.daily_rows.count()):
                row = self.daily_rows.item(index)
                action = row.data(Qt.ItemDataRole.UserRole)
                if action.message_id == task_id:
                    self.daily_rows.setCurrentItem(row)
                    self.daily_rows.scrollToItem(row)
                    break
            return
        self.navigate(route, task_id=task_id)

    def open_due_task(self, item: QListWidgetItem) -> None:
        if (task_id := item.data(Qt.ItemDataRole.UserRole)) is not None:
            self.navigate("task", task_id=task_id)

    def refresh_tab(self, index: int) -> None:
        if self.tabs.widget(index) is self.tasks:
            self.tasks.refresh()
        elif self.tabs.widget(index) is self.today:
            self.today.refresh()
            self.mail.check_connection()
            self.calendar.activate()
            self.refresh_briefing()
        self.update_navigation()

    def refresh_task_views(self) -> None:
        self.tasks.refresh()
        self.today.refresh()
        self.apply_event_masks()
        self.update_briefing()
        self.check_reminders()

    def calendar_plans(self, days: list[date]) -> list[Event]:
        """Saved task slots, drawn on the calendar next to the real events (never sent to Google)."""
        try:
            return plan_events(self.store.open_tasks(), days, self.calendar.zone) if self.store else []
        except TaskError:
            return []

    def apply_event_masks(self) -> None:
        """Re-split every held snapshot with the current local masks, then redraw."""
        try:
            keys = self.store.event_mask_keys() if self.store else frozenset()
        except TaskError:
            keys = frozenset()
        if self.briefing_snapshot is not None:
            self.briefing_snapshot = apply_masks(self.briefing_snapshot, keys)
        self.calendar.reapply_masks(keys)
        self.update_masked_summary()

    def update_masked_summary(self) -> None:
        try:
            count = len(self.store.event_masks()) if self.store else 0
        except TaskError:
            count = 0
        self.masked_summary.setText(f"本机已取消的行程：{count} 个。只在这台电脑生效，Google 日历没有改动。"
                                    if count else "没有在本机取消的行程。")
        self.restore_masks_button.setEnabled(count > 0 and self.store is not None)

    def toggle_event_mask(self, event: Event, mask: bool) -> None:
        if self.store is None or event.local:
            return
        try:
            if mask:
                mask_event(self.store, event)
            else:
                self.store.unmask_event(event_key(event))
        except TaskError as error:
            self.calendar.status.setText(str(error))
            return
        self.apply_event_masks()
        self.update_briefing()
        self.update_office()
        self.calendar.status.setText(
            f"已在本机取消“{event.title}”。Google 日历没有改动，重新连接或刷新也不会恢复它。" if mask
            else f"已恢复“{event.title}”。")

    def restore_all_masks(self) -> None:
        if self.store is None:
            return
        try:
            for item in self.store.event_masks():
                self.store.unmask_event(item.key)
        except TaskError as error:
            self.masked_summary.setText(str(error))
            return
        self.apply_event_masks()
        self.update_briefing()
        self.update_office()
        self.calendar.status.setText("已恢复所有在本机取消的行程。")

    def save_preference(self, key: str, value: bool) -> None:
        self.settings.setValue(key, value)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            self.today.reminder_status.setText("无法保存本机偏好设置。")
        if key == "briefing":
            self.today.briefing.setVisible(value)
            self.refresh_briefing()
        elif key == "reminders":
            self.check_reminders()

    def on_tick(self) -> None:
        self.mail.check_connection()
        now = datetime.now(self.calendar.zone)
        scan = self.daily_scan
        if scan and not self.daily_error and (scan.day != now.date() or now - scan.refreshed >= SNAPSHOT_MAX_AGE):
            self.daily_error = "请刷新今日简报，取得最新的邮件待办。"
            self.daily_status.setText(f"{word('stale', 'zh-Hans')}。{self.daily_error}\n{scan_text(scan, 'zh-Hans')}")
        self.check_reminders()
        if self.isVisible() and self.tabs.currentWidget() in (self.today, self.office):
            self.refresh_briefing()

    def setup_daily_mail(self) -> None:
        section = self.daily_section = QGroupBox("今日邮件待办")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 8, 10, 8)
        self.daily_status = QLabel("请刷新待跟进，让千早扫描 Gmail 找出需要处理的邮件。")
        self.daily_status.setTextFormat(Qt.TextFormat.PlainText)
        self.daily_status.setWordWrap(True)
        self.daily_rows = QListWidget()
        self.daily_rows.setAccessibleName("待跟进邮件")
        self.review_rows = QListWidget()
        self.review_rows.setAccessibleName("待确认与状态")
        for rows in (self.daily_rows, self.review_rows):
            tame_list(rows)
            rows.setMinimumHeight(96)
        self.daily_tabs = QTabWidget()
        self.daily_tabs.setAccessibleName("邮件待办分类")
        self.daily_tabs.addTab(self.daily_rows, "待跟进")
        self.daily_tabs.addTab(self.review_rows, "待确认")
        self.daily_refresh = QPushButton("刷新待跟进")
        self.daily_add = QPushButton("加入待办")
        self.daily_done = QPushButton("标为完成")
        self.daily_source = QPushButton("预览原信")
        self.daily_evidence = QPushButton("显示摘录")
        self.daily_restore = QPushButton("恢复已完成")
        buttons = QHBoxLayout()
        for button in (self.daily_refresh, self.daily_add, self.daily_done, self.daily_source,
                       self.daily_evidence, self.daily_restore):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self.daily_status)
        layout.addWidget(self.daily_tabs, 1)
        self.daily_refresh.clicked.connect(lambda: self.start_daily_brief())
        self.active_mail_rows = self.daily_rows
        self.daily_tabs.currentChanged.connect(self.switch_daily_tab)
        for rows in (self.daily_rows, self.review_rows):
            rows.currentItemChanged.connect(lambda item, _, source=rows: self.select_daily_row(source, item))
            rows.itemClicked.connect(lambda item, source=rows: self.select_daily_row(source, item))
            rows.itemActivated.connect(lambda _: self.navigate("follow", mail_rows=self.active_mail_rows))
        self.daily_source.clicked.connect(self.open_daily_source)
        self.daily_evidence.clicked.connect(self.show_daily_excerpt)
        self.daily_add.clicked.connect(self.add_daily_task)
        self.daily_done.clicked.connect(self.mark_daily_done)
        self.daily_restore.clicked.connect(self.restore_daily_done)
        self.update_daily_buttons()

    def selected_daily_action(self) -> MailAction | None:
        item = self.active_mail_rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def switch_daily_tab(self, index: int) -> None:
        rows = self.daily_tabs.widget(index)
        if isinstance(rows, QListWidget):
            self.active_mail_rows = rows
        self.update_daily_buttons()

    def select_daily_row(self, rows: QListWidget, item: QListWidgetItem | None) -> None:
        if item is not None and rows is not self.active_mail_rows:
            self.active_mail_rows = rows
            self.daily_tabs.setCurrentWidget(rows)
        self.update_daily_buttons()

    def clear_daily_rows(self) -> None:
        self.daily_rows.clear()
        self.review_rows.clear()
        self.daily_tabs.setTabText(0, "待跟进")
        self.daily_tabs.setTabText(1, "待确认")

    def update_daily_buttons(self) -> None:
        action = self.selected_daily_action()
        idle = self.daily_worker is None
        self.daily_refresh.setEnabled(idle and not self.mail.busy)
        self.brief_refresh.setEnabled(idle and not self.mail.busy)
        self.daily_add.setEnabled(idle and action is not None and not action.review and not action.status
                                  and self.store is not None)
        self.daily_done.setEnabled(idle and action is not None and self.store is not None)
        self.daily_source.setEnabled(idle and not self.mail.busy and action is not None)
        self.daily_evidence.setEnabled(idle and action is not None)
        self.daily_restore.setEnabled(idle and self.hidden_mail > 0)
        self.daily_restore.setText(f"恢复已完成 ({self.hidden_mail})" if self.hidden_mail else "恢复已完成")

    def visible_scan(self) -> MailScan | None:
        """The raw scan without the mails the producer already marked as done."""
        raw = self.daily_raw
        self.hidden_mail = 0
        if raw is None:
            return None
        try:
            hidden = self.store.dismissed_mail() if self.store else set()
        except TaskError:
            hidden = set()
        shown = tuple(action for action in raw.actions if action_source(action) not in hidden)
        self.hidden_mail = len(raw.actions) - len(shown)
        return replace(raw, actions=shown)

    def mark_daily_done(self) -> None:
        action = self.selected_daily_action()
        if action is None or self.store is None or self.daily_worker is not None:
            return
        rows = self.active_mail_rows
        row = rows.currentRow()
        try:
            self.store.dismiss_mail(action_source(action))
        except TaskError as error:
            self.daily_status.setText(str(error))
            return
        self.show_daily_rows()
        if rows.count():
            rows.setCurrentRow(min(row, rows.count() - 1))
        self.daily_status.setText(f"已标为完成，不再提醒：{action.subject}")
        self.update_daily_buttons()
        self.update_briefing()
        self.update_office()

    def restore_daily_done(self) -> None:
        if self.store is None or self.daily_worker is not None:
            return
        try:
            self.store.clear_dismissed_mail()
        except TaskError as error:
            self.daily_status.setText(str(error))
            return
        self.show_daily_rows()
        self.daily_status.setText("已恢复标为完成的邮件。")
        self.update_daily_buttons()
        self.update_briefing()
        self.update_office()

    def show_daily_rows(self) -> None:
        """Rebuild the two lists from the raw scan, minus the mails marked as done."""
        self.daily_scan = self.visible_scan()
        self.daily_items = self.daily_scan.actions if self.daily_scan else ()
        try:
            tasks = self.store.open_tasks() if self.store else []
        except TaskError:
            tasks = []
        self.clear_daily_rows()
        for action in self.daily_items:
            source = matching_source(action, tasks, self.briefing_snapshot)
            rows = self.review_rows if action.review or action.status else self.daily_rows
            item = QListWidgetItem(action_line(action, "zh-Hans") + (f"\n关联：{source}" if source else ""), rows)
            item.setData(Qt.ItemDataRole.UserRole, action)
        if self.daily_scan:
            self.daily_tabs.setTabText(0, f"待跟进 ({self.daily_rows.count()})")
            self.daily_tabs.setTabText(1, f"待确认 ({self.review_rows.count()})")

    def open_daily_source(self) -> None:
        if action := self.selected_daily_action():
            self.tabs.setCurrentWidget(self.mail_page)
            self.mail.preview_message(action.message_id)
            self.mail.preview.setFocus()

    def show_daily_excerpt(self) -> None:
        if action := self.selected_daily_action():
            box = QMessageBox(self)
            box.setWindowTitle("Gmail 原文摘录")
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(action.excerpt or "没有可显示的摘录。")
            box.exec()

    def add_daily_task(self) -> None:
        action = self.selected_daily_action()
        if action is None or action.review or action.status or self.store is None:
            return
        try:
            task_id, added = self.store.add_mail_action(
                action_source(action), f"{action_label(action, 'zh-Hans')}：{action.subject}", action.deadline,
                today=datetime.now(self.calendar.zone).date(),
            )
        except TaskError as error:
            self.daily_status.setText(str(error))
            return
        self.daily_status.setText(f"已加入待办 #{task_id}。" if added else f"已保存为待办 #{task_id}，没有重复添加。")
        self.refresh_task_views()

    def start_daily_brief(self, chat: tuple[str, str, str] | None = None) -> None:
        if self.daily_worker is not None:
            if chat is not None:
                self.show_reply(chat[0], Reply("今日简报正在刷新，请稍后再试。", "focused"))
            return
        if self.mail.busy:
            note = "Gmail 正在处理其他请求，请等它完成后再生成今日简报。"
            if chat is not None:
                self.show_reply(chat[0], Reply(note, "focused"))
            else:
                self.daily_status.setText(note)
            return
        now = datetime.now(self.calendar.zone)
        scan = self.daily_raw
        fresh = (scan is not None and not self.daily_error and scan.day == now.date()
                 and timedelta(0) <= now - scan.refreshed < SNAPSHOT_MAX_AGE)
        try:
            tasks = self.store.open_tasks() if self.store else []
        except TaskError as error:
            self.daily_status.setText(str(error))
            tasks = []
        try:
            candidate_limit = self.settings.value("gmail/candidate_threads", SCAN_LIMIT, type=int)
            content_limit = self.settings.value("gmail/content_checks", CONTENT_LIMIT, type=int)
        except (TypeError, ValueError):
            candidate_limit, content_limit = 0, 0
        self.daily_chat = chat
        self.daily_worker = DailyBriefRequest(now, self.calendar.zone, self.calendar.reader, self.mail.reader,
                                              scan if chat and fresh else None,
                                              include_calendar=chat is None or chat[1] == "brief",
                                              context=tuple(task.title for task in tasks),
                                              candidate_limit=candidate_limit, content_limit=content_limit,
                                              semantic=self.settings.value("gmail/local_classifier", True, type=bool))
        if chat is not None:
            self.status.setText("正在本机生成今日简报…")
            for control in (self.mode, self.entry, self.send_button, self.clear_button):
                control.setEnabled(False)
        self.daily_status.setText("正在刷新待跟进…")
        self.mail.set_brief_busy(True)
        self.update_daily_buttons()
        self.daily_worker.finished.connect(self.finish_daily_brief)
        self.daily_worker.start()
        self.update_office()

    @Slot()
    def finish_daily_brief(self) -> None:
        worker, self.daily_worker = self.daily_worker, None
        chat, self.daily_chat = self.daily_chat, None
        if worker is None:
            return
        worker.deleteLater()
        self.mail.set_brief_busy(False)
        if self.quitting:
            self.close()
            return
        if worker.include_calendar:
            self.briefing_snapshot = worker.snapshot
            self.briefing_status = worker.calendar_error
            self.briefing_last_attempt = worker.now.timestamp()
        self.daily_error = worker.mail_error
        self.mail.check_connection()
        if worker.disconnected or not self.mail.reader.token.is_file():
            self.clear_mail_results()
        elif worker.scan is not None:
            self.daily_raw = worker.scan
        if self.daily_raw and self.daily_raw.day != worker.now.date():
            self.daily_raw = None
        lang = chat[2] if chat else "zh-Hans"
        try:
            tasks = self.store.open_tasks() if self.store else []
        except TaskError:
            tasks = []
        mail_status = (word("not_connected", lang) if not self.mail.reader.token.is_file() else self.daily_error)
        self.show_daily_rows()
        self.daily_status.setText("\n".join(filter(None, (
            word("stale", "zh-Hans") if self.daily_error and self.daily_scan else "",
            word("not_connected", "zh-Hans") if not self.mail.reader.token.is_file() else self.daily_error,
            scan_text(self.daily_scan, "zh-Hans") if self.daily_scan else "",
            word("none", "zh-Hans") if self.daily_scan and not any(
                not item.review and not item.status for item in self.daily_items) else "",
            f"待确认：{sum(item.review for item in self.daily_items)}" if self.daily_scan else "",
            f"已标为完成的邮件 {self.hidden_mail} 封（可点“恢复已完成”）" if self.hidden_mail else "",
        ))))
        self.update_daily_buttons()
        self.update_briefing()
        if chat:
            for control in (self.mode, self.entry, self.send_button, self.clear_button):
                control.setEnabled(True)
            combined = chat[1] == "brief"
            fresh_scan = self.daily_scan if not self.daily_error else None
            _, lead = next_step(worker.now, tasks, fresh_scan, lang, worker.snapshot) if combined else ("", None)
            answer = mail_text(self.daily_scan, mail_status, lang, stale=bool(self.daily_error and self.daily_scan),
                               compact=combined, tasks=tasks, snapshot=worker.snapshot, lead=lead)
            prefix = ""
            if chat[1] == "brief":
                note = f"{word('calendar_error', lang)}: {worker.calendar_error}"
                prefix = self.local_daily_text(worker.now, worker.snapshot, note, lang) + "\n\n"
            if self.daily_scan is not None:
                self.mail_chat_results.add(prefix + answer)
            self.remember("The user requested a local Daily Brief.",
                          "Displayed local results. No private data is available to the model.")
            self.status.setText("今日简报已在本机显示。")
            self.show_reply(chat[0], Reply(prefix + answer, "focused"))

    def local_daily_text(self, now: datetime, snapshot: CalendarDay | None, status: str, lang: str = "zh-Hans") -> str:
        try:
            tasks = self.store.open_tasks() if self.store is not None else []
            start, end = (planning_time(field.text()) for field in (self.today.start, self.today.end))
            validate_window(start, end)
            budget = minutes_value(self.today.budget.text(), "Budget", optional=True, minimum=0)
            text = briefing_text(now, tasks, snapshot, status, start, end, budget, lang=lang)
        except TaskError as error:
            return str(error)
        scan = self.daily_scan
        if self.daily_error or scan is None or scan.day != now.date() or now - scan.refreshed >= SNAPSHOT_MAX_AGE:
            scan = None
        step, _ = next_step(now, tasks, scan, lang, snapshot)
        heading, separator, body = text.partition("\n")
        return heading + (f"\n{step}" if step else "") + separator + body

    def check_reminders(self) -> None:
        if self.store is None:
            self.today.reminder_status.setText("本机提醒记录不可用。")
            return
        if not self.today.reminders_enabled.isChecked():
            self.today.reminder_status.setText("待办提醒已关闭。")
            return
        try:
            fired = collect_reminders(self.store, datetime.now(self.calendar.zone))
            has_history = bool(self.store.recent_reminders())
        except TaskError as error:
            self.today.reminder_status.setText(str(error))
            return
        self.today.reminder_status.setText("提醒已开启，可在“提醒记录”查看已发出的提醒。" if has_history
                                           else "提醒已开启，目前还没有提醒记录。")
        if fired and self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage("千早待办提醒", f"有 {fired} 条待办提醒，请打开“提醒记录”查看。")

    def show_reminders(self) -> None:
        try:
            if self.store is None:
                raise TaskError("本机提醒记录不可用。")
            text = "\n".join(self.store.recent_reminders()) or "目前还没有提醒记录。"
        except TaskError as error:
            text = str(error)
        box = QMessageBox(self)
        box.setWindowTitle("提醒记录")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.exec()

    def calendar_updated(self) -> None:
        if self.quitting:
            self.close()
            return
        target, self.calendar_target = self.calendar_target, None
        if target is not None and target != self.calendar.day.date().toPython():
            self.calendar.day.setDate(QDate(target.year, target.month, target.day))
            self.update_briefing()
            return
        if self.calendar.day.date().toPython() == datetime.now(self.calendar.zone).date():
            self.briefing_snapshot = self.calendar.snapshot
            self.briefing_status = self.calendar.status.text()
            self.briefing_last_attempt = datetime.now(self.calendar.zone).timestamp()
            self.update_briefing()
        else:
            self.refresh_briefing()

    def refresh_briefing(self) -> None:
        if self.quitting:
            return
        now = datetime.now(self.calendar.zone)
        snapshot = self.briefing_snapshot
        selection_changed = bool(snapshot and not self.matches_calendar_selection(snapshot))
        if selection_changed:
            self.briefing_snapshot = snapshot = None
            self.briefing_last_attempt = float("-inf")
        if (self.briefing_worker is None and self.calendar.worker is None and self.calendar.reader.token.is_file()
                and now.timestamp() - self.briefing_last_attempt >= SNAPSHOT_MAX_AGE.total_seconds()
                and (snapshot is None or snapshot.day != now.date()
                     or now.timestamp() - snapshot.refreshed.timestamp() >= SNAPSHOT_MAX_AGE.total_seconds())):
            self.briefing_last_attempt = now.timestamp()
            self.briefing_worker = CalendarRefresh(self.calendar.reader, now.date(), self.calendar.zone,
                                                   False, reuse=True)
            self.briefing_worker.finished.connect(self.finish_briefing)
            self.briefing_worker.start()
        self.update_briefing()

    @Slot()
    def finish_briefing(self) -> None:
        worker, self.briefing_worker = self.briefing_worker, None
        if worker is not None:
            self.briefing_snapshot = worker.result
            self.briefing_status = (str(worker.error) if worker.error else
                                    "日历结果不完整，请刷新或手动规划。"
                                    if worker.result and worker.result.failures else "")
            worker.deleteLater()
        self.update_briefing()
        if self.quitting:
            self.close()

    def update_briefing(self) -> None:
        self.update_office()
        if not self.today.briefing_enabled.isChecked():
            return
        try:
            now = datetime.now(self.calendar.zone)
            note = ("正在更新今日日历…" if self.briefing_worker or self.calendar.worker else
                    "无法取得今日日历；请刷新，或到“设置”确认连接。")
            snapshot = self.briefing_snapshot
            if snapshot and (not self.calendar.reader.token.is_file() or not self.matches_calendar_selection(snapshot)
                             or now.timestamp() - snapshot.refreshed.timestamp() >= SNAPSHOT_MAX_AGE.total_seconds()):
                snapshot = None
            self.today.briefing.setText(self.local_daily_text(now, snapshot, note, "zh-Hans"))
        except TaskError as error:
            self.today.briefing.setText(str(error))

    def matches_calendar_selection(self, snapshot: CalendarDay) -> bool:
        try:
            return not snapshot.selected_ids or snapshot.selected_ids == self.calendar.reader.selected()
        except CalendarError:
            return False

    def update_office(self) -> None:
        now = datetime.now(self.calendar.zone)
        today = now.date()
        weekday = "一二三四五六日"[today.weekday()]
        ui = self.office
        ui.set_copy("clock", f"{now:%Y-%m-%d}  周{weekday}  {now:%H:%M}")
        try:
            tasks = self.store.open_tasks() if self.store else None
        except TaskError:
            tasks = None
        due = None if tasks is None else sum(is_due(task, today) for task in tasks)
        ui.set_copy("due", "今日到期" if due is None else f"今日到期 · {due}")
        mail_connected = self.mail.reader.token.is_file()
        mail_state = ("更新中" if self.mail.busy else "未连接" if not mail_connected else
                      "需重新连接" if self.mail.needs_reconnect else "已授权")
        ui.set_copy("mail_status", f"Gmail\n{mail_state}")
        scan = self.daily_scan if mail_connected else None
        follow = tuple(action for action in scan.actions if not action.review and not action.status) if scan else ()
        follow_state = ("更新中" if self.daily_worker else
                        f"{len(follow)}（旧）" if scan and (self.daily_error or scan.day != today or
                        now - scan.refreshed >= SNAPSHOT_MAX_AGE) else
                        f"{len(follow)}（部分）" if scan and scan.failures else
                        str(len(follow)) if scan else "未载入")
        ui.set_copy("today_status", f"今日状态\n到期 {due if due is not None else '—'}  ·  待回复 {follow_state}")
        ui.set_copy("follow", "待回复" if not scan else f"待回复 · {len(follow)}")
        result = self.mail.result if mail_connected and not self.mail.needs_reconnect else None
        if result and not result.search.keywords and result.search.day in (today, None) and not self.mail.worker:
            unread = f"未读 {len(result.rows)}{'+' if result.more else ''}"
            ui.set_copy("unread", f"未读 · {len(result.rows)}{'+' if result.more else ''}")
            ui.buttons["unread"].setToolTip("已载入的未读邮件，不是整个收件箱的总数")
        else:
            unread = ""
            ui.set_copy("unread", "未读邮件")
        screen_state = ("未连接" if not mail_connected else "需重新连接" if self.mail.needs_reconnect else
                        "更新中" if self.mail.busy else unread or (f"待回复 {len(follow)}" if scan else "点击查看"))
        ui.set_copy("monitor", f"信箱，{screen_state}")

        snapshot = self.briefing_snapshot
        calendar_connected = self.calendar.reader.token.is_file()
        reason = ""
        if self.briefing_worker or self.calendar.worker or (self.daily_worker and self.daily_worker.include_calendar):
            reason = "更新中"
        elif not calendar_connected:
            reason = "尚未连接"
        elif snapshot is None:
            reason = "读取失败" if self.briefing_status else "尚未载入"
        elif (not self.matches_calendar_selection(snapshot) or snapshot.day != today or
              not timedelta(0) <= now - snapshot.refreshed < SNAPSHOT_MAX_AGE):
            reason = "数据已过期"
        elif snapshot.failures:
            reason = "只取得部分行程"
        ui.set_copy("calendar_status", f"日历\n{reason or '已连接'}")

        event_copy = opening_copy = task_copy = reason
        self.office_event_day = None
        self.office_task_id = None
        if snapshot is not None and not reason:
            event = min((item for item in snapshot.events if item.end.astimezone(UTC) > now.astimezone(UTC)),
                        key=lambda item: item.start.astimezone(UTC), default=None)
            event_copy = "今日无行程" if not snapshot.events else "今日没有后续行程"
            if event is not None:
                start_text = "全天" if event.all_day else f"{event.start.astimezone(self.calendar.zone):%H:%M}"
                event_copy = f"{start_text} {event.title}"
                self.office_event_day = snapshot.day
            try:
                start, end = (planning_time(field.text()) for field in (self.today.start, self.today.end))
                validate_window(start, end)
                free = openings(snapshot, start, end, now,
                                slot_intervals(tasks or [], snapshot.day, snapshot.zone))
                if free:
                    left, right = (stamp.astimezone(self.calendar.zone) for stamp in free[0])
                    opening_copy = f"{left:%H:%M}–{right:%H:%M}"
                else:
                    opening_copy = "设定时段内已无空档"
            except TaskError:
                opening_copy = task_copy = "请检查规划时段"
            else:
                try:
                    budget = minutes_value(self.today.budget.text(), "预算", optional=True, minimum=0)
                    if tasks is not None:
                        plan = place_tasks(snapshot, free, tasks, now, budget)
                        task_copy = "没有适合空档的待办"
                        if plan.placed:
                            task, left, right, _ = min(plan.placed, key=lambda row: row[1].astimezone(UTC))
                            self.office_task_id = task.id
                            task_copy = (f"{left.astimezone(self.calendar.zone):%H:%M}–"
                                         f"{right.astimezone(self.calendar.zone):%H:%M} {task.title}")
                except TaskError:
                    task_copy = "请检查可用分钟数"
        if tasks is None:
            task_copy = "本机待办无法载入"
        elif reason:
            task_copy = f"无法比对空档 · {len(tasks)} 条本机待办"
        ui.set_copy("event", f"下一个行程\n{event_copy}")
        ui.set_copy("opening", f"下一段空档\n{opening_copy}\n查看今日简报")
        ui.set_copy("task", f"空档可安排\n{task_copy}")
        links = [(f"下一个行程：{event_copy}", "event", None),
                 (f"下一段空档：{opening_copy}", "calendar", None)]
        links.extend((f"待办：{task_fields(task).line()}", "task", task.id)
                     for task in tasks or () if is_due(task, today) or task.planned_day == today.isoformat()
                     or task.id == self.office_task_id)
        links.append((f"待回复邮件（已载入）：{follow_state}", "follow", None))
        links.extend((f"待跟进：{action.subject}", "mail-action", action.message_id) for action in follow[:3])
        existing = [(item.text(), *item.data(Qt.ItemDataRole.UserRole))
                    for i in range(self.brief_links.count()) if (item := self.brief_links.item(i))]
        if existing != links:
            self.brief_links.clear()
            for line, route, task_id in links:
                item = QListWidgetItem(line, self.brief_links)
                item.setData(Qt.ItemDataRole.UserRole, (route, task_id))

    def open_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_briefing(self) -> None:
        self.open_window()
        self.today.briefing_enabled.setChecked(True)
        self.navigate("brief")
        self.refresh_briefing()

    def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_window()

    def quit_app(self) -> None:
        self.quitting = True
        self.timer.stop()
        self.close()

    @Slot(int)
    def change_mode(self, index: int) -> None:
        if index == 0:
            self.status.setText("离线模式，不会发送任何消息。")
            return
        try:
            load_settings(self.data_dir)
        except ClaudeError as error:
            self.mode.setCurrentIndex(0)
            self.status.setText(str(error))
            return
        self.status.setText("Claude 模式：你发送的消息会传给 Anthropic。")

    def send(self) -> None:
        message = self.entry.text().strip()
        if not message or self.request is not None or self.mail_chat_pending or self.daily_chat or self.quitting:
            return
        normalized = message.casefold().strip(" \t.!?。！？")
        if normalized in {"yes", "yes please", "yes, please", "ok", "okay", "confirm", "好", "好的", "可以", "確認", "确认"}:
            action, self.pending_action = self.pending_action, None
            if action is not None and self.store is not None:
                chinese = bool(re.search(r"[\u3400-\u9fff]", message))
                outcome = approve(action, self.store, datetime.now(self.calendar.zone).replace(tzinfo=None), chinese)
                self.last_undo = outcome.undo or self.last_undo
                if outcome.changed:
                    self.refresh_task_views()
                    self.status.setText("待办已保存到本机。")
                self.remember(message, outcome.memory)
                self.show_reply(message, outcome.reply)
                return
            self.show_reply(message, Reply("目前没有待确认的操作。" if not normalized.isascii()
                                           else "There is no pending action to confirm.", "neutral"))
            return
        if normalized in {"no", "no thanks", "no, thanks", "cancel", "不要", "不用", "取消", "不"} and self.pending_action:
            self.pending_action = None
            self.show_reply(message, Reply("已取消，没有改动待办。" if not normalized.isascii()
                                           else "Cancelled. No tasks were changed.", "neutral"))
            return
        pending, self.pending_action = self.pending_action, None
        locale = QLocale().bcp47Name().lower()
        preferred = ("zh-Hant" if any(part in locale for part in ("hant", "-tw", "-hk", "-mo"))
                     else "zh-Hans" if locale.startswith("zh") else "en")
        if command := brief_command(message, preferred):
            self.start_daily_brief((message, *command))
            return
        if (re.search(r"\bwhat should i do\b", normalized)
                and re.search(r"\bi (?:only )?have\b", normalized)
                and (budget := time_budget(normalized)) is not None):
            try:
                if self.store is None:
                    raise TaskError("本机待办存储不可用。")
                now = datetime.now(self.calendar.zone).replace(tzinfo=None)
                text = plan_text(plan_today(self.store.open_tasks(), budget, now))
            except TaskError as error:
                text = str(error)
            self.remember("The user requested a manual time-budget plan.", "The app displayed the local plan.")
            self.show_reply(message, Reply(text, "focused"))
            return
        if kind := chat_mail_kind(message):
            self.mail_chat_pending = True
            self.status.setText("正在本机查看 Gmail…")
            for control in (self.mode, self.entry, self.send_button, self.clear_button):
                control.setEnabled(False)
            self.mail.refresh(chat=message, kind=kind)
            return
        try:
            query = parse_calendar_query(
                message, connected=self.calendar.reader.token.is_file(),
                today=datetime.now(self.calendar.zone).date(),
                window=(self.today.start.text(), self.today.end.text()),
                active=self.calendar_context.query if self.calendar_context else None,
            )
        except TaskError as error:
            self.status.setText(str(error))
            self.show_reply(message, Reply(str(error), "focused"))
            return
        if self.mode.currentIndex() == 0 and query is None:
            self.show_reply(message, respond(message))
            return
        self.request = ClaudeRequest(self.data_dir, message, tuple(self.claude_history))
        self.request.pending = pending
        if self.store is not None and self.mode.currentIndex() != 0:
            self.request.lookup_source = (self.calendar.reader, self.calendar.zone)
            try:
                self.request.task_context = task_context(
                    self.store, datetime.now(self.calendar.zone).replace(tzinfo=None), pending)
            except TaskError:
                self.request.task_context = None
        if query is not None:
            active = self.calendar_context if query.follow_up else None
            self.request.calendar_context = active
            self.request.calendar = (query, self.calendar.reader, active.zone if active else self.calendar.zone, self.store,
                                     self.mode.currentIndex() != 0)
        self.request.finished.connect(self.finish_request)
        self.status.setText("正在查询日历空档…" if query is not None else "正在等待 Claude…")
        for control in (self.mode, self.entry, self.send_button, self.clear_button):
            control.setEnabled(False)
        self.request.start()

    @Slot(str, str, bool)
    def finish_mail_chat(self, message: str, text: str, success: bool) -> None:
        self.mail_chat_pending = False
        if self.quitting:
            return
        for control in (self.mode, self.entry, self.send_button, self.clear_button):
            control.setEnabled(True)
        self.status.setText("Gmail 结果已在本机显示。" if success else text)
        if success:
            self.mail_chat_results.add(text)
        self.remember("The user asked to check Gmail locally.",
                      "Gmail was handled locally. No email content is available to the model.")
        self.show_reply(message, Reply(text, "focused"))

    @Slot()
    def on_mail_finished(self) -> None:
        self.update_daily_buttons()
        if self.quitting:
            self.close()
        else:
            self.update_office()

    @Slot()
    def clear_mail_results(self) -> None:
        self.mail.reader.thread_cache.clear()
        self.daily_scan = None
        self.daily_raw = None
        self.hidden_mail = 0
        self.daily_items = ()
        self.clear_daily_rows()
        self.daily_status.setText("请刷新待跟进，让千早扫描 Gmail 找出需要处理的邮件。" if self.mail.reader.token.is_file()
                                  else word("not_connected", "zh-Hans"))
        self.update_daily_buttons()
        self.update_briefing()
        if not self.mail_chat_results:
            return
        for text in self.mail_chat_results:
            self.history.replace_text(text, "[已清除本机 Gmail 结果。]")
        self.mail_chat_results.clear()

    @Slot()
    def finish_request(self) -> None:
        request = self.request
        if request is None:
            return
        self.request = None
        request.deleteLater()
        if self.quitting:
            self.close()
            return
        for control in (self.mode, self.entry, self.send_button, self.clear_button):
            control.setEnabled(True)
        if request.reply is None:
            self.status.setText(request.error or "Claude 没有返回回复，请再试一次。")
            self.entry.setFocus()
            return
        if request.calendar is not None:
            self.calendar_context = request.calendar_context
            self.remember(request.message, request.safe_calendar_reply)
            self.status.setText(request.error or "日历回答已准备好。")
            self.show_reply(request.message, request.reply)
            return
        try:
            outcome = apply_task_reply(
                request.reply, request.message, self.store, datetime.now(self.calendar.zone).replace(tzinfo=None),
                pending=request.pending, last_undo=self.last_undo, events=request.events,
            )
        except TaskError as error:
            self.status.setText(str(error))
            self.entry.setFocus()
            return
        self.status.setText("Claude 已回复。")
        self.pending_action = outcome.proposal
        if outcome.undone:
            self.last_undo = None
        elif outcome.undo is not None:
            self.last_undo = outcome.undo
        if outcome.changed:
            self.refresh_task_views()
        self.remember(request.message, outcome.memory)
        self.show_reply(request.message, outcome.reply)

    def remember(self, user: str, assistant: str) -> None:
        self.claude_history.append((user[:3000], assistant[:3000]))
        del self.claude_history[:-HISTORY_TURNS]

    def clear_conversation(self) -> None:
        if self.request is not None or self.mail_chat_pending or self.daily_chat:
            return
        self.claude_history.clear()
        self.mail_chat_results.clear()
        self.calendar_context = None
        self.pending_action = None
        self.last_undo = None
        self.history.set_messages([("chihaya", WELCOME)])
        self.entry.clear()
        self.portrait.set_expression("neutral")
        self.change_mode(self.mode.currentIndex())

    def show_reply(self, message: str, response: Reply) -> None:
        self.history.add_message("user", message)
        self.history.add_message("chihaya", response.reply)
        self.portrait.set_expression(response.expression)
        self.entry.clear()
        self.show_conversation(True)
        self.entry.setFocus()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.quitting and self.today.close_to_tray.isChecked() and self.tray.isVisible():
            self.hide()
            event.ignore()
            return
        if self.request is not None:
            self.status.setText("请等对话请求完成后再关闭窗口。")
            event.ignore()
            return
        if self.calendar.worker is not None or self.briefing_worker is not None:
            self.calendar.status.setText("请等 Google 日历读取完成后再关闭窗口。")
            event.ignore()
            return
        if self.mail.worker is not None or self.daily_worker is not None:
            self.mail.status.setText("请等 Gmail 请求完成后再关闭窗口。")
            event.ignore()
            return
        self.timer.stop()
        self.tray.hide()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Chihaya")
    install_desktop_theme(app)
    window = MainWindow(personal_art_dir())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
