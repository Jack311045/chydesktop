from collections.abc import Callable, Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QLocale, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCalendarWidget, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from calendar_reader import CalendarDay, CalendarError, CalendarReader, Event, ReconnectRequired, apply_masks
from tasks import format_fields
from week_view import EVENT_PALETTE, WeekGrid, calendar_color


class CalendarRefresh(QThread):
    def __init__(self, reader: CalendarReader, day: date, zone: ZoneInfo, connect: bool, *, reuse: bool = False,
                 days: Sequence[date] | None = None) -> None:
        super().__init__()
        self.reader, self.day, self.zone, self.connect = reader, day, zone, connect
        self.reuse = reuse
        self.days = tuple(days or ())
        self.result: CalendarDay | None = None
        self.week: list[CalendarDay] = []
        self.error: CalendarError | None = None

    def run(self) -> None:
        try:
            if self.days:
                self.week = self.reader.read_days(self.days, self.zone, connect=self.connect, reuse=self.reuse)
                self.result = next((item for item in self.week if item.day == self.day), self.week[0])
            elif self.reuse:
                self.result = self.reader.read(self.day, self.zone, reuse=True)
            else:
                self.result = self.reader.read(self.day, self.zone, connect=self.connect)
        except CalendarError as error:
            self.error = error


def swatch(color: str) -> QIcon:
    image = QPixmap(14, 14)
    image.fill(QColor(color))
    return QIcon(image)


class CalendarView(QWidget):
    """Google-Calendar-style day/week page with a mini month, calendar list and event details."""

    updated = Signal()
    started = Signal()
    panel_toggled = Signal(bool)
    mask_requested = Signal(object, bool)

    def __init__(self, reader: CalendarReader, zone: ZoneInfo) -> None:
        super().__init__()
        self.reader, self.zone = reader, zone
        self.worker: CalendarRefresh | None = None
        self.snapshot: CalendarDay | None = None
        self.week: dict[date, CalendarDay] = {}
        # Supplied by the app: saved task slots for the visible days, drawn as local plans.
        self.plan_source: Callable[[list[date]], list[Event]] | None = None
        self.detail_event: Event | None = None
        self.last_error = ""
        self.last_success: datetime | None = None
        self.attempted = False

        self.day = QDateEdit(self.local_today())
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.day.setCalendarPopup(True)
        self.day.setAccessibleName("日历日期")
        self.connect_button = QPushButton("重新连接 Google 日历" if reader.token.is_file() else "连接 Google 日历")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setAccessibleName("刷新日历")
        self.status = QLabel("已授权，请刷新。" if reader.token.is_file() else "尚未连接，请到“设置”连接。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("日历状态")
        self.last = QLabel("尚未成功更新")
        self.calendars = QListWidget()
        self.calendars.setAccessibleName("纳入的日历")
        self.calendars.setMaximumHeight(130)
        self.side_calendars = QListWidget()
        self.side_calendars.setAccessibleName("我的日历")
        self.side_calendars.setMaximumHeight(190)
        self.grid = WeekGrid()
        self.detail = QFrame()
        self.detail.setObjectName("eventDetail")
        self.detail_text = QLabel()
        self.detail_text.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_text.setWordWrap(True)
        self.detail_text.setAccessibleName("行程明细")
        self.mask_button = QPushButton()
        self.mask_button.setAccessibleName("本机取消或恢复这个行程")
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.addWidget(self.detail_text)
        detail_layout.addWidget(self.mask_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.mask_button.clicked.connect(self.request_mask)
        self.detail.hide()

        self.go_today = QPushButton("今天")
        self.previous = QPushButton("‹")
        self.next = QPushButton("›")
        for button, name in ((self.previous, "上一段"), (self.next, "下一段")):
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setFixedWidth(40)
        self.date_controls = (self.go_today, self.previous, self.next)
        self.title = QLabel()
        self.title.setAccessibleName("当前日期范围")
        self.title.setStyleSheet("font-size: 20px; font-weight: 700; color: #f2f7fc;")
        self.view_box = QComboBox()
        self.view_box.setAccessibleName("视图")
        self.view_box.addItem("日", "day")
        self.view_box.addItem("周", "week")
        self.view_box.setCurrentIndex(1)
        self.panel_button = QPushButton("简报与规划")
        self.panel_button.setCheckable(True)
        self.panel_button.setAccessibleName("简报与规划")

        self.mini = QCalendarWidget()
        self.mini.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Script.SimplifiedChineseScript,
                                    QLocale.Country.China))
        self.mini.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
        self.mini.setGridVisible(False)
        self.mini.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.mini.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.mini.setAccessibleName("迷你月历")
        self.mini.setSelectedDate(self.day.date())

        side = QWidget()
        side.setFixedWidth(236)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 6, 0)
        side_layout.addWidget(self.mini)
        heading = QLabel("我的日历")
        heading.setStyleSheet("font-weight: 700; color: #c4e4ff; padding-top: 6px;")
        side_layout.addWidget(heading)
        side_layout.addWidget(self.side_calendars)
        side_layout.addStretch()
        self.sidebar = side

        toolbar = QHBoxLayout()
        for widget in (self.go_today, self.previous, self.next):
            toolbar.addWidget(widget)
        toolbar.addWidget(self.title)
        toolbar.addStretch()
        for widget in (self.day, self.view_box, self.refresh_button, self.panel_button):
            toolbar.addWidget(widget)
        main = QVBoxLayout()
        main.addLayout(toolbar)
        main.addWidget(self.grid, 1)
        main.addWidget(self.detail)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.last)
        main.addLayout(status_row)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.addWidget(side)
        layout.addLayout(main, 1)

        self.connect_button.clicked.connect(lambda: self.refresh(connect=True))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.day.dateChanged.connect(self.date_changed)
        self.view_box.currentIndexChanged.connect(lambda: self.date_changed(force=True))
        self.previous.clicked.connect(lambda: self.shift(-1))
        self.next.clicked.connect(lambda: self.shift(1))
        self.go_today.clicked.connect(lambda: self.day.setDate(self.local_today()))
        self.mini.clicked.connect(self.day.setDate)
        self.grid.event_selected.connect(self.show_event)
        self.grid.day_selected.connect(lambda chosen: self.day.setDate(QDate(chosen.year, chosen.month, chosen.day)))
        self.panel_button.toggled.connect(self.panel_toggled)
        for source in (self.calendars, self.side_calendars):
            source.itemChanged.connect(lambda item, origin=source: self.save_selection(item, origin))
        self.render()

    def local_today(self) -> QDate:
        day = datetime.now(self.zone).date()
        return QDate(day.year, day.month, day.day)

    @property
    def view_mode(self) -> str:
        return self.view_box.currentData()

    def set_view_mode(self, mode: str) -> None:
        self.view_box.setCurrentIndex(max(0, self.view_box.findData(mode)))

    def range_days(self) -> list[date]:
        anchor = self.day.date().toPython()
        if self.view_mode == "day":
            return [anchor]
        first = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
        return [first + timedelta(days=offset) for offset in range(7)]

    def shift(self, direction: int) -> None:
        step = 1 if self.view_mode == "day" else 7
        self.day.setDate(self.day.date().addDays(direction * step))

    def date_changed(self, *_args, force: bool = False) -> None:
        with QSignalBlocker(self.mini):
            self.mini.setSelectedDate(self.day.date())
        days = self.range_days()
        if not self.reader.token.is_file():
            self.invalidate()
            self.render()
            return
        if not force and self.week and all(day in self.week for day in days) and not self.last_error:
            self.snapshot = self.week[self.day.date().toPython()]
            self.render()
            self.updated.emit()
            return
        self.refresh()

    def invalidate(self) -> None:
        self.snapshot = None
        self.week = {}
        self.grid.set_range(self.range_days(), (), self.zone, datetime.now(self.zone))

    def activate(self) -> None:
        if not self.attempted and self.reader.token.is_file():
            self.refresh()

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.day, self.connect_button, self.refresh_button, self.calendars, self.side_calendars,
                       self.view_box, self.mini, *self.date_controls):
            widget.setEnabled(enabled)

    def refresh(self, *, connect: bool = False) -> None:
        if self.worker is not None:
            return
        self.attempted = True
        self.last_error = ""
        if not connect and not self.reader.token.is_file():
            self.invalidate()
            self.status.setText("尚未连接，请到“设置”连接。")
            self.render()
            self.updated.emit()
            return
        self.status.setText("请在浏览器完成登录…" if connect else "正在载入行程…")
        self.refresh_button.setText("刷新")
        self.set_controls_enabled(False)
        self.invalidate()
        days = self.range_days() if self.view_mode == "week" else None
        self.worker = CalendarRefresh(self.reader, self.day.date().toPython(), self.zone, connect, days=days)
        self.worker.finished.connect(self.finish)
        self.worker.finished.connect(self.updated)
        self.render()
        self.worker.start()
        self.started.emit()

    @Slot()
    def finish(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        worker.deleteLater()
        self.set_controls_enabled(True)
        self.connect_button.setText("重新连接 Google 日历" if self.reader.token.is_file() else "连接 Google 日历")
        if worker.result is None:
            self.last_error = str(worker.error)
            self.status.setText(f"更新失败：{worker.error}")
            self.refresh_button.setText("重试")
            if worker.connect and not self.reader.token.is_file():
                self.connect_button.setText("重试 Google 日历登录")
            elif isinstance(worker.error, ReconnectRequired) and self.reader.token.is_file():
                self.connect_button.setText("重新连接 Google 日历")
            self.render()
            return
        self.snapshot = worker.result
        self.week = {item.day: item for item in (worker.week or [worker.result])}
        try:
            selected = self.reader.selected()
        except CalendarError as error:
            self.invalidate()
            self.last_error = str(error)
            self.status.setText(str(error))
            self.render()
            return
        self.populate_calendars(self.snapshot, selected)
        if self.snapshot.failures:
            self.status.setText("只取得部分行程：" + "、".join(self.snapshot.failures) + "。请重试或手动规划。")
            self.refresh_button.setText("重试")
        else:
            self.last_success = self.snapshot.refreshed
            self.last.setText(f"上次成功更新：{self.last_success:%Y-%m-%d %H:%M:%S %Z}")
            self.status.setText("已载入行程。")
        self.render()

    def populate_calendars(self, snapshot: CalendarDay, selected: tuple[str, ...]) -> None:
        for source in (self.calendars, self.side_calendars):
            with QSignalBlocker(source):
                source.clear()
                for calendar in snapshot.calendars:
                    item = QListWidgetItem(calendar.name, source)
                    item.setData(Qt.ItemDataRole.UserRole, calendar.id)
                    item.setCheckState(Qt.CheckState.Checked if calendar.id in selected else Qt.CheckState.Unchecked)
                    if source is self.side_calendars:
                        item.setIcon(swatch(calendar_color(calendar.name, self.color_map())))

    def week_events(self) -> list[Event]:
        seen: set[tuple[str, str, datetime, datetime]] = set()
        events: list[Event] = []
        for day in self.range_days():
            snapshot = self.week.get(day)
            for event in (*snapshot.events, *snapshot.masked) if snapshot else ():
                key = (event.calendar, event.title, event.start, event.end)
                if key not in seen:
                    seen.add(key)
                    events.append(event)
        return events

    def color_map(self) -> dict[str, str]:
        """The primary calendar is blue like Google Calendar; others keep their stable palette color."""
        snapshot = self.snapshot or next(iter(self.week.values()), None)
        if snapshot is None:
            return {}
        return {calendar.name: EVENT_PALETTE[0] for calendar in snapshot.calendars if calendar.id == "primary"}

    def reapply_masks(self, keys: Collection[str]) -> None:
        """Local masks changed: re-split the held snapshots without asking Google again."""
        if self.snapshot is not None:
            self.snapshot = apply_masks(self.snapshot, keys)
        self.week = {day: apply_masks(item, keys) for day, item in self.week.items()}
        self.render()
        if (event := self.grid.selected_event) is not None:
            self.show_event(event)

    def render(self) -> None:
        days = self.range_days()
        first, last = days[0], days[-1]
        self.title.setText(f"{first.year}年{first.month}月" if first.month == last.month else
                           f"{first.year}年{first.month}月 – {last.month}月")
        events = self.week_events()
        plans = self.plan_source(days) if self.plan_source else []
        self.grid.set_range(days, [*events, *plans], self.zone, datetime.now(self.zone), self.color_map())
        if not self.reader.token.is_file():
            message = "尚未连接 Google 日历。\n请到“设置”连接。"
        elif self.worker is not None:
            message = "正在载入行程…"
        elif self.last_error:
            message = f"载入失败：{self.last_error}"
        elif self.week and not events:
            message = "这段时间没有行程"
        else:
            message = ""
        self.grid.set_message(message)
        if self.grid.selected_event is None:
            self.detail.hide()

    def request_mask(self) -> None:
        if self.detail_event is not None and not self.detail_event.local:
            self.mask_requested.emit(self.detail_event, not self.detail_event.masked)

    def show_event(self, event: Event | None) -> None:
        self.detail_event = event
        if event is None:
            self.detail.hide()
            return
        duration = int((event.end.astimezone(UTC) - event.start.astimezone(UTC)).total_seconds() // 60)
        span = (f"{event.start:%Y-%m-%d} 全天" if event.all_day else
                f"{event.start:%Y-%m-%d %H:%M}–{event.end:%Y-%m-%d %H:%M}")
        fields = format_fields(event.title, category="本机安排" if event.local else "日历行程",
                               minutes=duration, scheduled=span)
        labels = ("名称", "类别", "预估时长", "已安排时段", "截止日期", "完成状态")
        parts = [f"{label}：{value}" for label, value in zip(labels, fields, strict=True)]
        if event.local:
            source = "来源：本机待办（只在这台电脑上，不会写入 Google 日历；请到“待办”页修改）"
        else:
            source = f"来源：{event.calendar}（{'忙碌' if event.busy else '有空'}）"
        text = f"{parts[0]}\n{'  ·  '.join(parts[1:4])}\n{'  ·  '.join(parts[4:])}  ·  {source}"
        if event.masked:
            text += "\n已在本机取消：规划时当作空档，Google 日历没有改动，刷新或重新连接也不会恢复它。"
        self.detail_text.setText(text)
        self.mask_button.setVisible(not event.local)
        self.mask_button.setText("恢复这个行程" if event.masked else "在本机取消这个行程")
        self.mask_button.setToolTip("只在这台电脑上隐藏，不会修改 Google 日历。" if not event.masked
                                    else "让这个行程重新计入忙碌时间。")
        self.detail.show()

    def save_selection(self, changed: QListWidgetItem, origin: QListWidget) -> None:
        ids = tuple(item.data(Qt.ItemDataRole.UserRole) for i in range(origin.count())
                    if (item := origin.item(i)).checkState() == Qt.CheckState.Checked)
        try:
            self.reader.save_selection(ids)
        except CalendarError as error:
            with QSignalBlocker(origin):
                changed.setCheckState(Qt.CheckState.Unchecked if changed.checkState() == Qt.CheckState.Checked
                                      else Qt.CheckState.Checked)
            self.status.setText(str(error))
            return
        other = self.side_calendars if origin is self.calendars else self.calendars
        with QSignalBlocker(other):
            for index in range(other.count()):
                item = other.item(index)
                item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in ids
                                   else Qt.CheckState.Unchecked)
        self.refresh()
