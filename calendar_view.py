from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDateEdit, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from calendar_reader import CalendarDay, CalendarError, CalendarReader, ReconnectRequired


class CalendarRefresh(QThread):
    def __init__(self, reader: CalendarReader, day: date, zone: ZoneInfo, connect: bool, *, reuse: bool = False) -> None:
        super().__init__()
        self.reader, self.day, self.zone, self.connect = reader, day, zone, connect
        self.reuse = reuse
        self.result: CalendarDay | None = None
        self.error: CalendarError | None = None

    def run(self) -> None:
        try:
            if self.reuse:
                self.result = self.reader.read(self.day, self.zone, reuse=True)
            else:
                self.result = self.reader.read(self.day, self.zone, connect=self.connect)
        except CalendarError as error:
            self.error = error


class CalendarView(QGroupBox):
    updated = Signal()
    started = Signal()
    def __init__(self, reader: CalendarReader, zone: ZoneInfo) -> None:
        super().__init__("Google Calendar (read-only)")
        self.reader, self.zone = reader, zone
        self.worker: CalendarRefresh | None = None
        self.snapshot: CalendarDay | None = None
        self.last_success: datetime | None = None
        self.attempted = False
        self.day = QDateEdit(QDate.currentDate())
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.day.setCalendarPopup(True)
        self.day.setAccessibleName("Calendar date")
        self.connect_button = QPushButton("Reconnect" if reader.token.is_file() else "Connect Google Calendar")
        self.refresh_button = QPushButton("Refresh")
        self.status = QLabel("Saved authorization available." if reader.token.is_file() else "Not connected.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Calendar status")
        self.last = QLabel("Last successful refresh: never")
        self.calendars = QListWidget()
        self.calendars.setAccessibleName("Included calendars")
        self.calendars.setMaximumHeight(65)
        self.events = QListWidget()
        self.events.setAccessibleName("Calendar events")
        header = QHBoxLayout()
        for widget in (self.day, QLabel(zone.key), self.connect_button, self.refresh_button):
            header.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.status)
        layout.addWidget(self.last)
        layout.addWidget(QLabel("Included calendars:"))
        layout.addWidget(self.calendars)
        layout.addWidget(self.events)
        self.connect_button.clicked.connect(lambda: self.refresh(connect=True))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.day.dateChanged.connect(lambda: self.refresh() if self.reader.token.is_file() else self.invalidate())
        self.calendars.itemChanged.connect(self.save_selection)

    def invalidate(self) -> None:
        self.snapshot = None
        self.events.clear()

    def activate(self) -> None:
        if not self.attempted and self.reader.token.is_file():
            self.refresh()

    def refresh(self, *, connect: bool = False) -> None:
        if self.worker is not None:
            return
        self.attempted = True
        self.invalidate()
        self.status.setText("Complete sign-in in your browser..." if connect else "Refreshing Google Calendar...")
        self.refresh_button.setText("Refresh")
        for widget in (self.day, self.connect_button, self.refresh_button, self.calendars):
            widget.setEnabled(False)
        self.worker = CalendarRefresh(self.reader, self.day.date().toPython(), self.zone, connect)
        self.worker.finished.connect(self.finish)
        self.worker.finished.connect(self.updated)
        self.worker.start()
        self.started.emit()

    @Slot()
    def finish(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        worker.deleteLater()
        for widget in (self.day, self.connect_button, self.refresh_button, self.calendars):
            widget.setEnabled(True)
        self.connect_button.setText("Reconnect" if self.reader.token.is_file() else "Connect Google Calendar")
        if worker.result is None:
            self.status.setText(str(worker.error))
            self.refresh_button.setText("Retry")
            if worker.connect and not self.reader.token.is_file():
                self.connect_button.setText("Retry sign-in")
            elif isinstance(worker.error, ReconnectRequired) and self.reader.token.is_file():
                self.connect_button.setText("Reconnect")
            return
        self.snapshot = worker.result
        try:
            selected = self.reader.selected()
        except CalendarError as error:
            self.invalidate()
            self.status.setText(str(error))
            return
        with QSignalBlocker(self.calendars):
            self.calendars.clear()
            for calendar in self.snapshot.calendars:
                item = QListWidgetItem(calendar.name, self.calendars)
                item.setData(Qt.ItemDataRole.UserRole, calendar.id)
                item.setCheckState(Qt.CheckState.Checked if calendar.id in selected else Qt.CheckState.Unchecked)
        for event in self.snapshot.events:
            if event.all_day:
                when = "All day"
            else:
                when = f"{event.start:%m-%d %H:%M %Z}–{event.end:%m-%d %H:%M %Z}"
            self.events.addItem(f"{when} | {event.calendar} | {event.title} ({'busy' if event.busy else 'free'})")
        if self.snapshot.failures:
            self.status.setText("Incomplete: " + ", ".join(self.snapshot.failures) + ". Use manual planning or retry.")
            self.refresh_button.setText("Retry")
        else:
            self.last_success = self.snapshot.refreshed
            self.last.setText(f"Last successful refresh: {self.last_success:%Y-%m-%d %H:%M:%S %Z}")
            self.status.setText("Connected." if self.snapshot.events else "Connected. No events for this day.")

    def save_selection(self, changed: QListWidgetItem) -> None:
        ids = tuple(item.data(Qt.ItemDataRole.UserRole) for i in range(self.calendars.count())
                    if (item := self.calendars.item(i)).checkState() == Qt.CheckState.Checked)
        try:
            self.reader.save_selection(ids)
        except CalendarError as error:
            with QSignalBlocker(self.calendars):
                changed.setCheckState(Qt.CheckState.Unchecked if changed.checkState() == Qt.CheckState.Checked
                                      else Qt.CheckState.Checked)
            self.status.setText(str(error))
            return
        self.refresh()
