from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QSettings, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from gmail_reader import DEFAULT_KEYWORDS, GmailError, GmailReader, LIMIT, MailResult, MailSearch, ReconnectGmail, job_keywords


class MailWorker(QThread):
    def __init__(self, reader: GmailReader, zone: ZoneInfo, *, search: MailSearch | None = None,
                 connect: bool = False, message_id: str = "", chat: str = "") -> None:
        super().__init__()
        self.reader, self.zone, self.search = reader, zone, search
        self.connect, self.message_id, self.chat = connect, message_id, chat
        self.result: MailResult | None = None
        self.body = ""
        self.error: GmailError | None = None

    def run(self) -> None:
        try:
            if self.search is not None:
                self.result = self.reader.read(self.search, self.zone, connect=self.connect)
            else:
                self.body = self.reader.preview(self.message_id)
        except GmailError as error:
            self.error = error


def mail_reply(result: MailResult, *, chinese: bool) -> str:
    if result.search.keywords:
        title = "符合求職關鍵字的未讀收件匣郵件" if chinese else "Unread inbox mail matching job keywords"
    else:
        title = "未讀收件匣郵件" if chinese else "Unread inbox mail"
    items = [row.display() for row in result.rows] or ["沒有符合的郵件。" if chinese else "No matching messages."]
    if result.more:
        items.append(f"只顯示最多 {LIMIT} 封，還有其他符合的郵件。" if chinese else
                     f"Showing up to {LIMIT}; more messages match.")
    return "\n".join([f"{result.search.day} | {title}", *items])


class MailView(QGroupBox):
    chat_ready = Signal(str, str, bool)
    finished = Signal()
    started = Signal()
    disconnected = Signal()
    reconnected = Signal()

    def __init__(self, reader: GmailReader, zone: ZoneInfo, settings: QSettings) -> None:
        super().__init__("Mail")
        self.reader, self.zone, self.settings = reader, zone, settings
        self.worker: MailWorker | None = None
        self.result: MailResult | None = None
        self.needs_reconnect = False
        self.had_token = reader.token.is_file()
        self.connect_button = QPushButton()
        self.refresh_button = QPushButton("Refresh mail")
        self.remove_button = QPushButton("Disconnect Gmail")
        self.filter = QComboBox()
        self.filter.setAccessibleName("Mail search")
        for title, key in (("Today", "today"), ("Today: job matches", "jobs"), ("All dates", "recent")):
            self.filter.addItem(title, key)
        self.keywords = QLineEdit(settings.value("gmail/job_keywords", DEFAULT_KEYWORDS, type=str))
        self.keywords.setAccessibleName("Gmail job keywords")
        self.keywords.setMaxLength(620)
        self.keywords.setToolTip("Comma-separated phrases. Used only for job matching.")
        self.status = QLabel("Saved Gmail authorization. Refresh to load mail." if self.had_token else
                             "Gmail is not connected.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Gmail status")
        self.rows = QListWidget()
        self.rows.setAccessibleName("Recent unread mail")
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setAccessibleName("Gmail message preview")
        self.preview.setPlaceholderText("Select a message to preview it without marking it read.")
        self.rows.setMaximumHeight(105)
        self.preview.setMaximumHeight(105)
        buttons, filters, panes = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        for button in (self.connect_button, self.refresh_button, self.remove_button):
            buttons.addWidget(button)
        filters.addWidget(self.filter)
        filters.addWidget(QLabel("Job keywords:"))
        filters.addWidget(self.keywords, 1)
        panes.addWidget(self.rows, 1)
        panes.addWidget(self.preview, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addLayout(filters)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Recent unread mail"))
        layout.addLayout(panes)
        self.connect_button.clicked.connect(lambda: self.refresh(connect=True))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.remove_button.clicked.connect(self.disconnect)
        self.filter.currentIndexChanged.connect(self.invalidate_search)
        self.keywords.textEdited.connect(self.invalidate_search)
        self.keywords.editingFinished.connect(self.save_keywords)
        self.rows.currentRowChanged.connect(self.open_preview)
        self.update_controls()

    def update_controls(self) -> None:
        connected = self.reader.token.is_file()
        idle = self.worker is None
        self.connect_button.setText("Reconnect Gmail" if connected or self.needs_reconnect else "Connect Gmail")
        self.connect_button.setEnabled(idle)
        self.refresh_button.setEnabled(idle and connected and not self.needs_reconnect)
        self.remove_button.setEnabled(idle and connected)
        self.filter.setEnabled(idle)
        self.keywords.setEnabled(idle)
        self.rows.setEnabled(idle and connected and not self.needs_reconnect)

    def clear_rows(self) -> None:
        self.result = None
        self.rows.clear()
        self.preview.clear()

    def check_connection(self) -> None:
        if self.worker is not None:
            return
        connected = self.reader.token.is_file()
        if self.had_token and not connected:
            self.clear_rows()
            self.needs_reconnect = False
            self.status.setText("Gmail connection removed.")
            self.disconnected.emit()
        self.had_token = connected
        if self.result is not None and self.result.search.day is not None:
            if self.result.search.day != datetime.now(self.zone).date() and not self.status.text().startswith("Stale results."):
                self.status.setText(f"Stale results. Mail shown is for {self.result.search.day}. Refresh for today.")
        self.update_controls()

    def disconnect(self) -> None:
        if self.worker is not None:
            return
        try:
            self.reader.token.unlink(missing_ok=True)
        except OSError:
            self.status.setText("Could not remove the saved Gmail connection.")
            return
        self.check_connection()

    def invalidate_search(self) -> None:
        if self.result is not None:
            self.clear_rows()
            self.status.setText("Mail filter changed. Refresh for results.")

    def save_keywords(self) -> None:
        try:
            keywords = job_keywords(self.keywords.text())
        except GmailError as error:
            self.status.setText(str(error))
            return
        value = ", ".join(keywords)
        if value != self.settings.value("gmail/job_keywords", DEFAULT_KEYWORDS, type=str):
            self.settings.setValue("gmail/job_keywords", value)
            self.settings.sync()
            if self.settings.status() != QSettings.Status.NoError:
                self.status.setText("Could not save Gmail keywords.")

    def make_search(self, kind: str) -> MailSearch:
        day = datetime.now(self.zone).date() if kind != "recent" else None
        return MailSearch(day, job_keywords(self.keywords.text()) if kind == "jobs" else ())

    def refresh(self, *, connect: bool = False, chat: str = "", kind: str = "") -> bool:
        if self.worker is not None:
            if chat:
                self.chat_ready.emit(chat, "Gmail is busy. Try again once this request finishes.", False)
            return False
        self.check_connection()
        kind = kind or self.filter.currentData()
        if chat:
            self.filter.setCurrentIndex(self.filter.findData(kind))
        try:
            if not connect and not self.reader.token.is_file():
                raise ReconnectGmail("Gmail is not connected. Choose Connect Gmail in Today.")
            search = self.make_search(kind)
        except GmailError as error:
            self.status.setText(str(error))
            if chat:
                self.chat_ready.emit(chat, str(error), False)
            return False
        if connect:
            self.clear_rows()
        elif self.result is not None and self.result.search != search:
            self.clear_rows()
        self.preview.clear()
        self.status.setText("Complete Gmail sign-in in your browser…" if connect else "Refreshing Gmail…")
        self.start(MailWorker(self.reader, self.zone, search=search, connect=connect, chat=chat))
        return True

    def open_preview(self, index: int) -> None:
        if self.worker is not None or self.result is None or not 0 <= index < len(self.result.rows):
            return
        self.preview_message(self.result.rows[index].id)

    def preview_message(self, message_id: str) -> None:
        if self.worker is not None or not self.reader.token.is_file():
            self.preview.setPlainText("Gmail is busy or disconnected. Try again after connecting or refreshing.")
            return
        self.preview.setPlainText("Loading message preview…")
        self.start(MailWorker(self.reader, self.zone, message_id=message_id))

    def start(self, worker: MailWorker) -> None:
        self.worker = worker
        self.update_controls()
        worker.finished.connect(self.finish)
        worker.start()
        self.started.emit()

    @Slot()
    def finish(self) -> None:
        worker, self.worker = self.worker, None
        if worker is None:
            return
        worker.deleteLater()
        removed = self.had_token and not self.reader.token.is_file()
        self.check_connection()
        if removed:
            if worker.chat:
                self.chat_ready.emit(worker.chat, self.status.text(), False)
        elif worker.error is not None:
            self.needs_reconnect = isinstance(worker.error, ReconnectGmail)
            if self.needs_reconnect:
                self.clear_rows()
            note = str(worker.error)
            if worker.search is None:
                self.preview.setPlainText(note)
            else:
                self.status.setText(("Stale results. " if self.result is not None else "") + note)
            if self.needs_reconnect:
                self.status.setText(note)
            if worker.chat:
                self.chat_ready.emit(worker.chat, note, False)
        elif worker.result is not None:
            self.needs_reconnect = False
            self.clear_rows()
            self.result = worker.result
            self.rows.addItems([row.display() for row in self.result.rows])
            label = "Matching mail" if self.result.search.keywords else "Unread inbox mail"
            scope = str(self.result.search.day) if self.result.search.day else "all dates"
            detail = "No matching messages." if not self.result.rows else (
                f"Showing {len(self.result.rows)}. More messages match." if self.result.more else
                f"Showing {len(self.result.rows)}.")
            self.status.setText(f"Gmail connected. {label} ({scope}). {detail} "
                                f"Refreshed {datetime.now(self.zone):%H:%M:%S %Z}.")
            if worker.chat:
                chinese = any("\u3400" <= char <= "\u9fff" for char in worker.chat)
                self.chat_ready.emit(worker.chat, mail_reply(self.result, chinese=chinese), True)
            if worker.connect:
                self.reconnected.emit()
        else:
            self.preview.setPlainText(worker.body)
        self.update_controls()
        self.finished.emit()
