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
        title = "符合求职关键词的未读收件箱邮件" if chinese else "Unread inbox mail matching job keywords"
    else:
        title = "未读收件箱邮件" if chinese else "Unread inbox mail"
    items = [row.display() for row in result.rows] or ["没有符合的邮件。" if chinese else "No matching messages."]
    if result.more:
        items.append(f"最多只显示 {LIMIT} 封，还有其他符合的邮件。" if chinese else
                     f"Showing up to {LIMIT}; more messages match.")
    return "\n".join([f"{result.search.day} | {title}", *items])


class MailView(QGroupBox):
    chat_ready = Signal(str, str, bool)
    finished = Signal()
    started = Signal()
    disconnected = Signal()
    reconnected = Signal()

    def __init__(self, reader: GmailReader, zone: ZoneInfo, settings: QSettings) -> None:
        super().__init__("Gmail 未读邮件")
        self.reader, self.zone, self.settings = reader, zone, settings
        self.worker: MailWorker | None = None
        self._brief_busy = False
        self.result: MailResult | None = None
        self.needs_reconnect = False
        self.had_token = reader.token.is_file()
        self.connect_button = QPushButton()
        self.refresh_button = QPushButton("刷新邮件")
        self.remove_button = QPushButton("断开 Gmail 连接")
        self.filter = QComboBox()
        self.filter.setAccessibleName("邮件搜索")
        for title, key in (("今日", "today"), ("今日求职邮件", "jobs"), ("所有日期", "recent")):
            self.filter.addItem(title, key)
        self.keywords = QLineEdit(settings.value("gmail/job_keywords", DEFAULT_KEYWORDS, type=str))
        self.keywords.setAccessibleName("Gmail 求职关键词")
        self.keywords.setMaxLength(620)
        self.keywords.setToolTip("用逗号分隔，只用于筛选求职邮件。")
        self.status = QLabel("已保存 Gmail 授权，请刷新邮件。" if self.had_token else
                             "Gmail 尚未连接，请到“设置”连接。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Gmail 状态")
        self.rows = QListWidget()
        self.rows.setAccessibleName("最近未读邮件")
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setAccessibleName("Gmail 邮件预览")
        self.preview.setPlaceholderText("选择邮件即可预览，不会标为已读。")
        self.rows.setWordWrap(True)
        self.rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        filters, panes = QHBoxLayout(), QHBoxLayout()
        filters.addWidget(self.filter)
        filters.addStretch()
        filters.addWidget(self.refresh_button)
        panes.addWidget(self.rows, 1)
        panes.addWidget(self.preview, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addWidget(self.status)
        layout.addLayout(panes, 1)
        self.connect_button.clicked.connect(lambda: self.refresh(connect=True))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.remove_button.clicked.connect(self.disconnect)
        self.filter.currentIndexChanged.connect(self.invalidate_search)
        self.keywords.textEdited.connect(self.invalidate_search)
        self.keywords.editingFinished.connect(self.save_keywords)
        self.rows.currentRowChanged.connect(self.open_preview)
        self.update_controls()

    @property
    def busy(self) -> bool:
        return self.worker is not None or self._brief_busy

    def set_brief_busy(self, busy: bool) -> None:
        self._brief_busy = busy
        self.update_controls()

    def update_controls(self) -> None:
        connected = self.reader.token.is_file()
        idle = not self.busy
        self.connect_button.setText("重新连接 Gmail" if connected or self.needs_reconnect else "连接 Gmail")
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
        if self.busy:
            return
        connected = self.reader.token.is_file()
        if self.had_token and not connected:
            self.clear_rows()
            self.needs_reconnect = False
            self.status.setText("已断开 Gmail 连接。")
            self.disconnected.emit()
        self.had_token = connected
        if self.result is not None and self.result.search.day is not None:
            if self.result.search.day != datetime.now(self.zone).date() and not self.status.text().startswith("数据已过期"):
                self.status.setText(f"数据已过期：当前显示 {self.result.search.day} 的邮件，请刷新。")
        self.update_controls()

    def disconnect(self) -> None:
        if self.busy:
            return
        try:
            self.reader.token.unlink(missing_ok=True)
        except OSError:
            self.status.setText("无法移除已保存的 Gmail 授权。")
            return
        self.check_connection()

    def invalidate_search(self) -> None:
        if self.result is not None:
            self.clear_rows()
            self.status.setText("筛选条件已变更，请刷新。")

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
        if self.busy:
            if chat:
                self.chat_ready.emit(chat, "Gmail is busy. Try again once this request finishes.", False)
            return False
        self.check_connection()
        kind = kind or self.filter.currentData()
        if chat:
            self.filter.setCurrentIndex(self.filter.findData(kind))
        try:
            if not connect and not self.reader.token.is_file():
                raise ReconnectGmail("Gmail 尚未连接，请到“设置”页连接 Gmail。")
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
        self.status.setText("请在浏览器完成 Gmail 登录…" if connect else "正在刷新 Gmail…")
        self.start(MailWorker(self.reader, self.zone, search=search, connect=connect, chat=chat))
        return True

    def open_preview(self, index: int) -> None:
        if self.busy or self.result is None or not 0 <= index < len(self.result.rows):
            return
        self.preview_message(self.result.rows[index].id)

    def preview_message(self, message_id: str) -> None:
        if self.busy or not self.reader.token.is_file():
            self.preview.setPlainText("Gmail 正忙或尚未连接，请稍后再试。")
            return
        self.preview.setPlainText("正在载入邮件预览…")
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
                self.status.setText(("显示的是先前结果。" if self.result is not None else "") + f"更新失败：{note}")
            if self.needs_reconnect:
                self.status.setText(note)
            if worker.chat:
                self.chat_ready.emit(worker.chat, note, False)
        elif worker.result is not None:
            self.needs_reconnect = False
            self.clear_rows()
            self.result = worker.result
            self.rows.addItems([row.display() for row in self.result.rows])
            label = "符合条件的邮件" if self.result.search.keywords else "收件箱未读邮件"
            scope = str(self.result.search.day) if self.result.search.day else "所有日期"
            detail = "没有符合的邮件。" if not self.result.rows else (
                f"显示 {len(self.result.rows)} 封，另有其他符合的邮件。" if self.result.more else
                f"显示 {len(self.result.rows)} 封。")
            self.status.setText(f"{label}（{scope}） · {detail} 更新于 {datetime.now(self.zone):%H:%M:%S %Z}")
            if worker.chat:
                chinese = any("\u3400" <= char <= "\u9fff" for char in worker.chat)
                self.chat_ready.emit(worker.chat, mail_reply(self.result, chinese=chinese), True)
            if worker.connect:
                self.reconnected.emit()
        else:
            self.preview.setPlainText(worker.body)
        self.update_controls()
        self.finished.emit()
