from datetime import datetime

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from tasks import TaskError, TaskStore, is_due, is_today, minutes_value, plan_today, task_fields
from availability import availability_text, openings, place_tasks, slot_intervals, window_end
from calendar_chat import planning_time, validate_window
from calendar_view import CalendarView
from desktop_style import tame_list
from task_board import TaskView

__all__ = ["TaskView", "TodayView"]


class TodayView(QWidget):
    def __init__(self, store: TaskStore | None, error: str, calendar: CalendarView) -> None:
        super().__init__()
        self.store = store
        self.error = error
        self.calendar = calendar
        self.list = QListWidget()
        self.list.setAccessibleName("今日到期或已安排的待办")
        self.budget = QLineEdit(placeholderText="可用分钟")
        self.budget.setAccessibleName("可用分钟")
        self.suggest = QPushButton("建议顺序")
        self.output = QPlainTextEdit(readOnly=True)
        self.output.setAccessibleName("建议的安排")
        self.start = QLineEdit("08:00")
        self.end = QLineEdit("18:00")
        self.start.setAccessibleName("规划开始时间")
        self.end.setAccessibleName("规划结束时间")
        self.calendar_plan = QPushButton("按空档规划")
        window = QHBoxLayout()
        for widget in (QLabel("规划时段"), self.start, self.end, self.calendar_plan):
            window.addWidget(widget)
        row = QHBoxLayout()
        row.addWidget(QLabel("可用分钟"))
        row.addWidget(self.budget)
        row.addWidget(self.suggest)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumWidth(350)
        self.scroll.setMaximumWidth(430)
        panel = QWidget()
        self.scroll.setWidget(panel)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(calendar, 1)
        outer.addWidget(self.scroll)
        self.scroll.hide()
        calendar.panel_toggled.connect(self.show_panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 12, 8)
        self.briefing_enabled = QCheckBox("显示今日简报")
        self.briefing = QLabel()
        self.briefing.setTextFormat(Qt.TextFormat.PlainText)
        self.briefing.setWordWrap(True)
        self.briefing.setAccessibleName("今日简报")
        self.reminders_enabled = QCheckBox("待办提醒")
        self.close_to_tray = QCheckBox("关闭时缩到系统托盘")
        self.reminders_button = QPushButton("提醒记录")
        self.reminder_status = QLabel()
        self.reminder_status.setWordWrap(True)
        layout.addWidget(self.briefing)
        self.briefing_layout = layout
        heading = QLabel("今日到期及逾期")
        heading.setStyleSheet("font-weight: 700; color: #c4e4ff;")
        layout.addWidget(heading)
        self.due_list = QListWidget()
        self.due_list.setAccessibleName("今日到期及逾期")
        self.due_list.setMinimumHeight(110)
        tame_list(self.due_list)
        layout.addWidget(self.due_list)
        heading = QLabel("今日到期或已安排的待办")
        heading.setStyleSheet("font-weight: 700; color: #c4e4ff;")
        layout.addWidget(heading)
        self.list.setMinimumHeight(110)
        tame_list(self.list)
        layout.addWidget(self.list)
        layout.addLayout(row)
        self.output.setMinimumHeight(160)
        layout.addWidget(self.output, 1)
        layout.addLayout(window)
        self.suggest.clicked.connect(self.show_plan)
        self.budget.returnPressed.connect(self.show_plan)
        self.calendar_plan.clicked.connect(self.show_calendar_plan)

    def show_panel(self, visible: bool) -> None:
        self.scroll.setVisible(visible)
        if self.calendar.panel_button.isChecked() != visible:
            blocker = QSignalBlocker(self.calendar.panel_button)
            self.calendar.panel_button.setChecked(visible)
            del blocker

    def add_briefing_section(self, section: QWidget) -> None:
        self.briefing_layout.insertWidget(self.briefing_layout.indexOf(self.briefing) + 1, section)

    def show_calendar_plan(self) -> None:
        snapshot = self.calendar.snapshot
        if snapshot is None or snapshot.failures:
            self.output.setPlainText("目前无法取得日历空档。请刷新，或输入可用分钟后点“建议顺序”。")
            return
        try:
            start, end = (planning_time(value.text()) for value in (self.start, self.end))
            validate_window(start, end)
        except TaskError as error:
            self.output.setPlainText(str(error))
            return
        try:
            if self.store is None:
                raise TaskError(self.error)
            budget = minutes_value(self.budget.text(), "预算", optional=True, minimum=0)
            now = datetime.now(snapshot.zone)
            tasks = self.store.open_tasks()
            free = openings(snapshot, start, end, now, slot_intervals(tasks, snapshot.day, snapshot.zone))
            schedule = place_tasks(snapshot, free, tasks, now, budget)
            ended = now.timestamp() >= window_end(snapshot.day, end, snapshot.zone).timestamp()
            self.output.setPlainText(availability_text(snapshot, free, schedule, window_ended=ended,
                                                       chinese=True, desktop=True))
        except TaskError as error:
            self.output.setPlainText(str(error))

    def refresh(self) -> None:
        self.list.clear()
        self.due_list.clear()
        try:
            if self.store is None:
                raise TaskError(self.error)
            today = datetime.now(self.calendar.zone).date()
            for task in self.store.open_tasks():
                if is_today(task, today):
                    self.list.addItem(task_fields(task).line())
                if is_due(task, today):
                    item = QListWidgetItem(task_fields(task).line(), self.due_list)
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
        except TaskError as error:
            self.due_list.addItem(str(error))
            self.output.setPlainText(str(error))

    def show_plan(self) -> None:
        try:
            if self.store is None:
                raise TaskError(self.error)
            budget = minutes_value(self.budget.text(), "预算", minimum=0)
            assert budget is not None
            plan = plan_today(self.store.open_tasks(), budget, datetime.now())
            lines = [f"建议使用 {plan.used} / {plan.budget} 分钟（未保存）"]
            for heading, group in (("建议顺序", plan.fitted), ("排不下", plan.unscheduled),
                                   ("未估时", plan.unestimated)):
                lines.extend(("", heading, *([task_fields(task).line() for task in group] or ["无"])))
            self.output.setPlainText("\n".join(lines))
        except TaskError as error:
            self.output.setPlainText(str(error))
