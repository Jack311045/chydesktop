import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from datetime import UTC, date, datetime, timedelta
import sys

from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, QSettings, QSize, QStandardPaths, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPainter, QPainterPath, QPaintEvent, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)
from tzlocal import get_localzone

from availability import openings, place_tasks
from calendar_chat import CalendarContext, CalendarQuery, calendar_answer, parse_calendar_query, planning_time, validate_window
from calendar_reader import CalendarDay, CalendarError, CalendarReader, SNAPSHOT_MAX_AGE
from calendar_view import CalendarRefresh, CalendarView
from claude_reply import ClaudeError, HISTORY_TURNS, load_settings, request_reply
from dialogue import Expression, Reply, respond
from daily_brief import (
    DailyBriefRequest, action_label, action_line, action_source, brief_command, mail_text,
    matching_source, next_step, scan_text, word,
)
from desktop_today import briefing_text, collect_reminders
from gmail_reader import GmailReader, chat_mail_kind
from gmail_view import MailView
from mail_actions import CONTENT_LIMIT, SCAN_LIMIT, MailAction, MailScan
from office_view import OfficeView, SceneButton, office_icon
from task_chat import PendingAction, TaskReply, apply_task_reply, time_budget
from task_views import TaskView, TodayView
from tasks import Task, TaskError, TaskStore, is_due, minutes_value, plan_text, plan_today, task_line

CANVAS = QSize(640, 855)


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
        self.setToolTip("Using the built-in placeholder. Personal portraits: " + str(self.folder))
        for name in dict.fromkeys((expression, "neutral")):
            path = self.folder / f"{name}.png"
            if path.is_file():
                image = QPixmap(str(path))
                if image.size() == CANVAS:
                    self.artwork = image
                    self.setToolTip(f"Portrait: {name}")
                    break
        self.setAccessibleName(f"Portrait: {expression}")
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
                self.safe_calendar_reply = model_summary or "Calendar availability was unavailable."
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
            self.reply = request_reply(self.data_dir, self.message, self.history)
        except (CalendarError, ClaudeError, TaskError) as error:
            self.error = str(error)


class MainWindow(QMainWindow):
    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.setWindowTitle("Chihaya Desktop")
        self.resize(1100, 680)
        self.data_dir = folder.parent
        self.claude_history: list[tuple[str, str]] = []
        self.calendar_context: CalendarContext | None = None
        self.pending_action: PendingAction | None = None
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
        self.mode.setAccessibleName("Reply mode")
        self.mode.addItems(["Offline", "Claude"])
        self.mode.currentIndexChanged.connect(self.change_mode)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Reply mode:"))
        mode_row.addWidget(self.mode)
        mode_row.addStretch()
        self.clear_button = QPushButton("Clear conversation")
        self.clear_button.clicked.connect(self.clear_conversation)
        mode_row.addWidget(self.clear_button)
        self.status = QLabel("Offline demo. No messages are sent.")
        self.status.setAccessibleName("Connection status")
        self.status.setWordWrap(True)
        self.history = QPlainTextEdit(readOnly=True)
        self.history.setAccessibleName("Chat history")
        self.history.setPlainText("Chihaya: Tell me what you'd like to work on.")
        self.entry = QLineEdit(placeholderText="Type a message…")
        self.entry.setAccessibleName("Message")
        self.send_button = QPushButton("Send")
        self.entry.returnPressed.connect(self.send)
        self.send_button.clicked.connect(self.send)

        composer = QHBoxLayout()
        composer.addWidget(self.entry, 1)
        composer.addWidget(self.send_button)
        chat = QVBoxLayout()
        chat.addLayout(mode_row)
        chat.addWidget(self.status)
        chat.addWidget(self.history, 1)
        chat.addLayout(composer)
        body = self.chat_page = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        layout.addWidget(self.portrait, 2)
        layout.addLayout(chat, 3)
        self.tabs = QTabWidget()
        self.tasks = TaskView(self.store, task_error)
        self.calendar = CalendarView(CalendarReader(self.data_dir), get_localzone())
        self.today = TodayView(self.store, task_error, self.calendar)
        self.today.due_list.itemActivated.connect(self.open_due_task)
        self.brief_links = QListWidget()
        self.brief_links.setAccessibleName("今日簡報連結")
        self.brief_links.setMaximumHeight(105)
        self.brief_links.itemActivated.connect(self.open_brief_link)
        self.settings = QSettings(str(self.data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.mail = MailView(GmailReader(self.data_dir), self.calendar.zone, self.settings)
        self.today.add_briefing_section(self.mail)
        self.daily_worker: DailyBriefRequest | None = None
        self.daily_scan: MailScan | None = None
        self.daily_error = ""
        self.daily_chat: tuple[str, str, str] | None = None
        self.daily_items: tuple[MailAction, ...] = ()
        self.setup_daily_mail()
        self.today.add_briefing_section(self.brief_links)
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
        menu.addAction("Open", self.open_window)
        menu.addAction("Today's briefing", self.open_briefing)
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Chihaya Desktop")
        self.tray.activated.connect(self.tray_activated)
        self.tray.messageClicked.connect(self.open_briefing)
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if tray_available:
            self.tray.show()
        self.today.close_to_tray.setEnabled(tray_available)
        self.today.close_to_tray.setToolTip("Windows system tray is unavailable." if not tray_available else
                                           "Hide instead of quitting when the window closes.")
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
        self.tabs.addTab(body, "Chat")
        self.tabs.addTab(self.tasks, "Tasks")
        self.tabs.addTab(self.today, "Today")
        self.office = OfficeView(lambda painter, target: self.portrait.paint_art(painter, target, badge=False))
        self.office.opened.connect(self.navigate)
        self.portrait.expression_changed.connect(self.office.update)
        self.tabs.addTab(self.office, "Office")
        self.tabs.tabBar().hide()
        self.tabs.setCurrentWidget(self.office)
        self.office_launcher: QWidget | None = None
        self.office_event_day: date | None = None
        self.office_task_id: int | None = None
        self.calendar_target: date | None = None
        self.navigation_section = "today"
        self.nav_buttons: dict[str, QPushButton] = {}
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.tabs, 1)
        nav = QHBoxLayout()
        nav.setContentsMargins(9, 5, 9, 5)
        for route, label in (("office", "事務所"), ("today", "今日"), ("tasks", "待辦"),
                             ("chat", "對話"), ("mail", "信箱")):
            button = SceneButton(label)
            button.setAccessibleName(label)
            button.setMinimumHeight(34)
            button.setCheckable(True)
            button.setProperty("navigation", True)
            button.setIconSize(QSize(21, 21))
            button.setStyleSheet("QPushButton { border: 1px solid #b5c7d4; border-radius: 6px; background: #f0f6fa; }"
                                 "QPushButton:hover { background: #d5ebf8; }"
                                 "QPushButton:checked { background: #1c5382; color: white; }"
                                 "QPushButton:focus { border: 2px solid #54a7d4; }")
            button.clicked.connect(lambda checked=False, target=route: self.navigate(target))
            self.nav_buttons[route] = button
            nav.addWidget(button, 1)
        shell_layout.addLayout(nav)
        self.tabs.currentChanged.connect(self.refresh_tab)
        self.tasks.changed.connect(self.refresh_task_views)
        self.setCentralWidget(shell)
        self.escape_to_office = QShortcut(Qt.Key.Key_Escape, self)
        self.escape_to_office.activated.connect(lambda: self.navigate("office"))
        self.office.buttons["clock"].setFocus()
        self.update_navigation()
        self.refresh_briefing()
        self.check_reminders()

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
            self.tabs.setCurrentWidget(self.chat_page)
            self.entry.setFocus()
        elif route in ("task", "tasks"):
            self.tabs.setCurrentWidget(self.tasks)
            if route == "task":
                wanted = task_id if task_id is not None else self.office_task_id
                for index in range(self.tasks.list.count()):
                    item = self.tasks.list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == wanted:
                        self.tasks.list.setCurrentItem(item)
                        self.tasks.list.scrollToItem(item)
                        break
            self.tasks.list.setFocus()
        else:
            self.navigation_section = route
            focus: QWidget
            if route in ("calendar", "event"):
                if route == "event" or launcher is self.office.buttons["clock"]:
                    day = self.office_event_day if route == "event" else None
                    day = day or datetime.now(self.calendar.zone).date()
                    if self.calendar.worker is None:
                        self.calendar.day.setDate(QDate(day.year, day.month, day.day))
                    else:
                        self.calendar_target = day
                focus = (self.calendar.connect_button if launcher is self.office.buttons["calendar_status"]
                         else self.calendar.day)
            elif route == "mail":
                if launcher in (self.office.buttons["monitor"], self.office.buttons["mail_hint"],
                                self.office.buttons["unread"]) and self.mail.filter.currentData() == "jobs":
                    self.mail.filter.setCurrentIndex(self.mail.filter.findData("recent"))
                focus = (self.mail.connect_button if launcher is self.office.buttons["mail_status"]
                         or not self.mail.reader.token.is_file() else self.mail.filter)
            elif route == "follow":
                focus = mail_rows if mail_rows is not None else self.daily_rows
            elif route == "due":
                self.today.refresh()
                focus = self.today.due_list
            else:
                focus = self.daily_refresh if route == "brief" else self.today.briefing_enabled
            self.tabs.setCurrentWidget(self.today)
            focus.setFocus()
            self.today.scroll.ensureWidgetVisible(focus, 0, 12)
            if route in ("brief", "today"):
                self.today.scroll.verticalScrollBar().setValue(0)
        self.update_navigation()

    def update_navigation(self) -> None:
        current = self.tabs.currentWidget()
        selected = ("office" if current is self.office else "chat" if current is self.chat_page else
                    "tasks" if current is self.tasks else "mail" if self.navigation_section == "mail" else "today")
        for route, button in self.nav_buttons.items():
            checked = route == selected
            button.setChecked(checked)
            button.setProperty("tone", "blue" if checked else "")
            button.setIcon(office_icon(route, "#ffffff" if checked else "#1c5382"))

    def open_brief_link(self, item: QListWidgetItem) -> None:
        route, task_id = item.data(Qt.ItemDataRole.UserRole)
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
        self.update_briefing()
        self.check_reminders()

    def save_preference(self, key: str, value: bool) -> None:
        self.settings.setValue(key, value)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            self.today.reminder_status.setText("Could not save local preferences.")
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
            self.daily_error = "Refresh Daily Brief for current mail actions."
            self.daily_status.setText(f"{word('stale', 'en')}. {self.daily_error}\n{scan_text(scan, 'en')}")
        self.check_reminders()
        if self.isVisible() and self.tabs.currentWidget() in (self.today, self.office):
            self.refresh_briefing()

    def setup_daily_mail(self) -> None:
        section = self.daily_section = QGroupBox("Mail to follow up")
        layout = QVBoxLayout(section)
        self.daily_status = QLabel("Refresh Daily Brief to scan Gmail for actions.")
        self.daily_status.setTextFormat(Qt.TextFormat.PlainText)
        self.daily_status.setWordWrap(True)
        self.daily_rows = QListWidget()
        self.daily_rows.setAccessibleName("Mail to follow up")
        self.daily_rows.setMaximumHeight(80)
        self.review_rows = QListWidget()
        self.review_rows.setAccessibleName("Review and status")
        self.review_rows.setMaximumHeight(74)
        self.review_heading = QLabel("Review / Status")
        self.review_heading.hide()
        self.review_rows.hide()
        self.daily_refresh = QPushButton("Refresh Daily Brief")
        self.daily_add = QPushButton("Add to Tasks")
        self.daily_source = QPushButton("Open source message")
        self.daily_evidence = QPushButton("Show excerpt")
        buttons = QHBoxLayout()
        for button in (self.daily_refresh, self.daily_add, self.daily_source, self.daily_evidence):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self.daily_status)
        layout.addWidget(self.daily_rows)
        layout.addWidget(self.review_heading)
        layout.addWidget(self.review_rows)
        self.today.add_briefing_section(section)
        self.daily_refresh.clicked.connect(lambda: self.start_daily_brief())
        self.active_mail_rows = self.daily_rows
        for rows in (self.daily_rows, self.review_rows):
            rows.currentItemChanged.connect(lambda item, _, source=rows: self.select_daily_row(source, item))
            rows.itemClicked.connect(lambda item, source=rows: self.select_daily_row(source, item))
        for rows in (self.daily_rows, self.review_rows):
            rows.itemActivated.connect(lambda _: self.navigate("follow", mail_rows=self.active_mail_rows))
        self.daily_source.clicked.connect(self.open_daily_source)
        self.daily_evidence.clicked.connect(self.show_daily_excerpt)
        self.daily_add.clicked.connect(self.add_daily_task)
        self.update_daily_buttons()

    def selected_daily_action(self) -> MailAction | None:
        item = self.active_mail_rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select_daily_row(self, rows: QListWidget, item: QListWidgetItem | None) -> None:
        if item is not None and rows is not self.active_mail_rows:
            previous, self.active_mail_rows = self.active_mail_rows, rows
            previous.setCurrentRow(-1)
        self.update_daily_buttons()

    def clear_daily_rows(self) -> None:
        self.daily_rows.clear()
        self.review_rows.clear()
        self.review_heading.hide()
        self.review_rows.hide()

    def update_daily_buttons(self) -> None:
        action = self.selected_daily_action()
        idle = self.daily_worker is None
        self.daily_refresh.setEnabled(idle and not self.mail.busy)
        self.daily_add.setEnabled(idle and action is not None and not action.review and not action.status
                                  and self.store is not None)
        self.daily_source.setEnabled(idle and not self.mail.busy and action is not None)
        self.daily_evidence.setEnabled(idle and action is not None)

    def open_daily_source(self) -> None:
        if action := self.selected_daily_action():
            self.mail.preview_message(action.message_id)
            self.today.scroll.ensureWidgetVisible(self.mail.preview)

    def show_daily_excerpt(self) -> None:
        if action := self.selected_daily_action():
            box = QMessageBox(self)
            box.setWindowTitle("Gmail source excerpt")
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(action.excerpt or "No excerpt available.")
            box.exec()

    def add_daily_task(self) -> None:
        action = self.selected_daily_action()
        if action is None or action.review or action.status or self.store is None:
            return
        try:
            task_id, added = self.store.add_mail_action(
                action_source(action), f"{action_label(action, 'en')}: {action.subject}", action.deadline,
                today=datetime.now(self.calendar.zone).date(),
            )
        except TaskError as error:
            self.daily_status.setText(str(error))
            return
        self.daily_status.setText(f"Added task #{task_id}." if added else f"Already saved as task #{task_id}.")
        self.refresh_task_views()

    def start_daily_brief(self, chat: tuple[str, str, str] | None = None) -> None:
        if self.daily_worker is not None:
            if chat is not None:
                self.show_reply(chat[0], Reply("The Daily Brief is refreshing. Try again when it finishes.", "focused"))
            return
        if self.mail.busy:
            note = "Gmail is busy. Try the Daily Brief again once the request finishes."
            if chat is not None:
                self.show_reply(chat[0], Reply(note, "focused"))
            else:
                self.daily_status.setText(note)
            return
        now = datetime.now(self.calendar.zone)
        scan = self.daily_scan
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
            self.status.setText("Preparing the Daily Brief locally…")
            for control in (self.mode, self.entry, self.send_button, self.clear_button):
                control.setEnabled(False)
        self.daily_status.setText("Refreshing Daily Brief…")
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
            self.daily_scan = worker.scan
        if self.daily_scan and self.daily_scan.day != worker.now.date():
            self.daily_scan = None
        lang = chat[2] if chat else "en"
        try:
            tasks = self.store.open_tasks() if self.store else []
        except TaskError:
            tasks = []
        mail_status = (word("not_connected", lang) if not self.mail.reader.token.is_file() else self.daily_error)
        self.daily_items = self.daily_scan.actions if self.daily_scan else ()
        self.clear_daily_rows()
        for action in self.daily_items:
            source = matching_source(action, tasks, self.briefing_snapshot)
            rows = self.review_rows if action.review or action.status else self.daily_rows
            item = QListWidgetItem(action_line(action, "en") + (f" | Linked to {source}" if source else ""), rows)
            item.setData(Qt.ItemDataRole.UserRole, action)
        if self.review_rows.count():
            self.review_heading.show()
            self.review_rows.show()
        self.daily_status.setText("\n".join(filter(None, (
            word("stale", "en") if self.daily_error and self.daily_scan else "",
            word("not_connected", "en") if not self.mail.reader.token.is_file() else self.daily_error,
            scan_text(self.daily_scan, "en") if self.daily_scan else "",
            word("none", "en") if self.daily_scan and not any(
                not item.review and not item.status for item in self.daily_items) else "",
            f"Review: {sum(item.review for item in self.daily_items)}" if self.daily_scan else "",
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
            self.status.setText("Daily Brief displayed locally.")
            self.show_reply(chat[0], Reply(prefix + answer, "focused"))

    def local_daily_text(self, now: datetime, snapshot: CalendarDay | None, status: str, lang: str = "en") -> str:
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
            self.today.reminder_status.setText("Local reminder history is unavailable.")
            return
        if not self.today.reminders_enabled.isChecked():
            self.today.reminder_status.setText("Task reminders are off.")
            return
        try:
            fired = collect_reminders(self.store, datetime.now(self.calendar.zone))
            has_history = bool(self.store.recent_reminders())
        except TaskError as error:
            self.today.reminder_status.setText(str(error))
            return
        self.today.reminder_status.setText("Reminders on. See Reminder history for recorded alerts." if has_history
                                           else "Reminders on. No alerts recorded yet.")
        if fired and self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage("Chihaya task reminder", f"{fired} task alert(s). Open Reminder history for details.")

    def show_reminders(self) -> None:
        try:
            if self.store is None:
                raise TaskError("Local reminder history is unavailable.")
            text = "\n".join(self.store.recent_reminders()) or "No alerts recorded yet."
        except TaskError as error:
            text = str(error)
        box = QMessageBox(self)
        box.setWindowTitle("Reminder history")
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
                                    "Calendar result incomplete. Refresh or use manual planning."
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
            note = ("Refreshing today's calendar..." if self.briefing_worker or self.calendar.worker else
                    self.briefing_status or "Calendar unavailable. Connect or refresh Google Calendar.")
            snapshot = self.briefing_snapshot
            if snapshot and (not self.calendar.reader.token.is_file() or not self.matches_calendar_selection(snapshot)
                             or now.timestamp() - snapshot.refreshed.timestamp() >= SNAPSHOT_MAX_AGE.total_seconds()):
                snapshot = None
            self.today.briefing.setText(self.local_daily_text(now, snapshot, note))
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
        ui.set_copy("clock", f"千早桌面助手\n{now:%Y-%m-%d}  週{weekday}  {now:%H:%M}")
        try:
            tasks = self.store.open_tasks() if self.store else None
        except TaskError:
            tasks = None
        due = None if tasks is None else sum(is_due(task, today) for task in tasks)
        ui.set_copy("due", "今日到期" if due is None else f"今日到期 · {due}")
        mail_connected = self.mail.reader.token.is_file()
        mail_state = ("更新中" if self.mail.busy else "未連線" if not mail_connected else
                      "需重新連線" if self.mail.needs_reconnect else "已授權")
        ui.set_copy("mail_status", f"Gmail\n{mail_state}")
        scan = self.daily_scan if mail_connected else None
        follow = tuple(action for action in scan.actions if not action.review and not action.status) if scan else ()
        follow_state = ("更新中" if self.daily_worker else
                        f"{len(follow)}（舊）" if scan and (self.daily_error or scan.day != today or
                        now - scan.refreshed >= SNAPSHOT_MAX_AGE) else
                        f"{len(follow)}（部分）" if scan and scan.failures else
                        str(len(follow)) if scan else "未載入")
        ui.set_copy("today_status", f"今日狀態\n到期 {due if due is not None else '—'}  ·  待回覆 {follow_state}")
        ui.set_copy("follow", "待回覆" if not scan else f"待回覆 · {len(follow)}")
        result = self.mail.result if mail_connected and not self.mail.needs_reconnect else None
        if result and not result.search.keywords and result.search.day in (today, None) and not self.mail.worker:
            ui.set_copy("unread", f"未讀 · {len(result.rows)}{'+' if result.more else ''}")
            ui.buttons["unread"].setToolTip("已載入的未讀郵件，非全收件匣總數")
        else:
            ui.set_copy("unread", "未讀郵件")

        snapshot = self.briefing_snapshot
        calendar_connected = self.calendar.reader.token.is_file()
        reason = ""
        if self.briefing_worker or self.calendar.worker or (self.daily_worker and self.daily_worker.include_calendar):
            reason = "更新中"
        elif not calendar_connected:
            reason = "尚未連線"
        elif snapshot is None:
            reason = "讀取失敗" if self.briefing_status else "尚未載入"
        elif (not self.matches_calendar_selection(snapshot) or snapshot.day != today or
              not timedelta(0) <= now - snapshot.refreshed < SNAPSHOT_MAX_AGE):
            reason = "資料已過期"
        elif snapshot.failures:
            reason = "只取得部分行程"
        ui.set_copy("calendar_status", f"行事曆\n{reason or '已連線'}")

        event_copy = opening_copy = task_copy = reason
        self.office_event_day = None
        self.office_task_id = None
        if snapshot is not None and not reason:
            event = min((item for item in snapshot.events if item.end.astimezone(UTC) > now.astimezone(UTC)),
                        key=lambda item: item.start.astimezone(UTC), default=None)
            event_copy = "今日無行程" if not snapshot.events else "今日無後續行程"
            if event is not None:
                start_text = "全天" if event.all_day else f"{event.start.astimezone(self.calendar.zone):%H:%M}"
                event_copy = f"{start_text} {event.title}"
                self.office_event_day = snapshot.day
            try:
                start, end = (planning_time(field.text()) for field in (self.today.start, self.today.end))
                validate_window(start, end)
                free = openings(snapshot, start, end, now)
                if free:
                    left, right = (stamp.astimezone(self.calendar.zone) for stamp in free[0])
                    opening_copy = f"{left:%H:%M}–{right:%H:%M}"
                else:
                    opening_copy = "設定時段內已無空檔"
            except TaskError:
                opening_copy = task_copy = "請檢查規劃時段"
            else:
                try:
                    budget = minutes_value(self.today.budget.text(), "Budget", optional=True, minimum=0)
                    if tasks is not None:
                        plan = place_tasks(snapshot, free, tasks, now, budget)
                        task_copy = "沒有符合空檔的待辦"
                        if plan.placed:
                            task, left, right, _ = min(plan.placed, key=lambda row: row[1].astimezone(UTC))
                            self.office_task_id = task.id
                            task_copy = (f"{left.astimezone(self.calendar.zone):%H:%M}–"
                                         f"{right.astimezone(self.calendar.zone):%H:%M} {task.title}")
                except TaskError:
                    task_copy = "請檢查可用分鐘數"
        if tasks is None:
            task_copy = "本機待辦無法載入"
        elif reason:
            task_copy = f"無法比對空檔 · {len(tasks)} 筆本機待辦"
        ui.set_copy("event", f"下一個行程\n{event_copy}")
        ui.set_copy("opening", f"下一段空檔\n{opening_copy}\n查看今日簡報")
        ui.set_copy("task", f"空檔可安排\n{task_copy}")
        links = [(f"下一個行程：{event_copy}", "event", None),
                 (f"下一段空檔：{opening_copy}", "calendar", None)]
        links.extend((f"待辦：{task_line(task)}", "task", task.id)
                     for task in tasks or () if is_due(task, today) or task.planned_day == today.isoformat()
                     or task.id == self.office_task_id)
        links.append((f"待回覆郵件（已載入）：{follow_state}", "follow", None))
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
            self.status.setText("Offline demo. No messages are sent.")
            return
        try:
            load_settings(self.data_dir)
        except ClaudeError as error:
            self.mode.setCurrentIndex(0)
            self.status.setText(str(error))
            return
        self.status.setText("Claude mode. Your Claude-mode messages are sent to Anthropic.")

    def send(self) -> None:
        message = self.entry.text().strip()
        if not message or self.request is not None or self.mail_chat_pending or self.daily_chat or self.quitting:
            return
        normalized = message.casefold().strip(" \t.!?。！？")
        if normalized in {"yes", "yes please", "yes, please", "ok", "okay", "confirm", "好", "好的", "可以", "確認", "确认"}:
            action, self.pending_action = self.pending_action, None
            if action is not None and self.store is not None:
                chinese = bool(re.search(r"[\u3400-\u9fff]", message))
                try:
                    text = action.commit(self.store, datetime.now(self.calendar.zone), chinese)
                except TaskError as error:
                    self.status.setText(str(error))
                    self.show_reply(message, Reply(str(error), "focused"))
                    return
                self.refresh_task_views()
                self.status.setText("Task saved locally.")
                self.remember(message, "The app committed the confirmed task change locally.")
                self.show_reply(message, Reply(text, "focused"))
                return
            self.show_reply(message, Reply("目前沒有待確認的操作。" if not normalized.isascii()
                                           else "There is no pending action to confirm.", "neutral"))
            return
        if normalized in {"no", "no thanks", "no, thanks", "cancel", "不要", "不用", "取消", "不"} and self.pending_action:
            self.pending_action = None
            self.show_reply(message, Reply("已取消，未變更任務。" if not normalized.isascii()
                                           else "Cancelled. No tasks were changed.", "neutral"))
            return
        self.pending_action = None
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
                    raise TaskError("Local task storage is unavailable.")
                now = datetime.now(self.calendar.zone).replace(tzinfo=None)
                text = plan_text(plan_today(self.store.open_tasks(), budget, now))
            except TaskError as error:
                text = str(error)
            self.remember("The user requested a manual time-budget plan.", "The app displayed the local plan.")
            self.show_reply(message, Reply(text, "focused"))
            return
        if kind := chat_mail_kind(message):
            self.mail_chat_pending = True
            self.status.setText("Checking Gmail locally…")
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
        if query is not None:
            active = self.calendar_context if query.follow_up else None
            self.request.calendar_context = active
            self.request.calendar = (query, self.calendar.reader, active.zone if active else self.calendar.zone, self.store,
                                     self.mode.currentIndex() != 0)
        self.request.finished.connect(self.finish_request)
        self.status.setText("Checking calendar availability..." if query is not None else "Waiting for Claude...")
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
        self.status.setText("Gmail results displayed locally." if success else text)
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
        self.daily_items = ()
        self.clear_daily_rows()
        self.daily_status.setText("Refresh Daily Brief to scan Gmail for actions." if self.mail.reader.token.is_file()
                                  else word("not_connected", "en"))
        self.update_daily_buttons()
        self.update_briefing()
        if not self.mail_chat_results:
            return
        transcript = self.history.toPlainText()
        for text in self.mail_chat_results:
            transcript = transcript.replace(text, "[Local Gmail results cleared.]")
        self.mail_chat_results.clear()
        self.history.setPlainText(transcript)

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
            self.status.setText(request.error or "Claude did not return a reply. Try again.")
            self.entry.setFocus()
            return
        if request.calendar is not None:
            self.calendar_context = request.calendar_context
            self.remember(request.message, request.safe_calendar_reply)
            self.status.setText(request.error or "Calendar answer ready.")
            self.show_reply(request.message, request.reply)
            return
        try:
            reply, changed = apply_task_reply(
                request.reply, request.message, self.store, datetime.now(self.calendar.zone).replace(tzinfo=None),
                propose=self.set_pending_action,
            )
        except TaskError as error:
            self.status.setText(str(error))
            self.entry.setFocus()
            return
        self.status.setText("Claude replied.")
        if changed:
            self.refresh_task_views()
        context = "The app handled that request locally." if isinstance(request.reply, TaskReply) else reply.reply
        self.remember(request.message, context)
        self.show_reply(request.message, reply)

    def set_pending_action(self, action: PendingAction) -> None:
        self.pending_action = action

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
        self.history.setPlainText("Chihaya: Tell me what you'd like to work on.")
        self.entry.clear()
        self.portrait.set_expression("neutral")
        self.change_mode(self.mode.currentIndex())

    def show_reply(self, message: str, response: Reply) -> None:
        self.history.appendPlainText(f"\nYou: {message}\nChihaya: {response.reply}")
        self.portrait.set_expression(response.expression)
        self.entry.clear()
        self.entry.setFocus()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.quitting and self.today.close_to_tray.isChecked() and self.tray.isVisible():
            self.hide()
            event.ignore()
            return
        if self.request is not None:
            self.status.setText("Close the window once the chat request finishes.")
            event.ignore()
            return
        if self.calendar.worker is not None or self.briefing_worker is not None:
            self.calendar.status.setText("Close the window once Google Calendar finishes.")
            event.ignore()
            return
        if self.mail.worker is not None or self.daily_worker is not None:
            self.mail.status.setText("Close the window once the Gmail request finishes.")
            event.ignore()
            return
        self.timer.stop()
        self.tray.hide()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Chihaya")
    window = MainWindow(personal_art_dir())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
