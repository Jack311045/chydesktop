from datetime import date, datetime
from typing import Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from tasks import Task, TaskError, TaskStore, is_due, is_today, minutes_value, plan_text, plan_today, task_line
from availability import availability_text, openings, place_tasks, window_end
from calendar_chat import planning_time, validate_window
from calendar_view import CalendarView


class TaskView(QWidget):
    changed = Signal()

    def __init__(self, store: TaskStore | None, error: str) -> None:
        super().__init__()
        self.store = store
        self.error = error
        self.records: dict[int, Task] = {}
        self.list = QListWidget()
        self.list.setAccessibleName("Open tasks")
        self.title = QLineEdit()
        self.deadline = QLineEdit(placeholderText="YYYY-MM-DD [HH:MM] or Friday")
        self.minutes = QLineEdit(placeholderText="Optional")
        self.day = QLineEdit(placeholderText="YYYY-MM-DD or tomorrow")
        form = QFormLayout()
        for name, control in (("Task title", self.title), ("Deadline", self.deadline),
                              ("Estimated minutes", self.minutes), ("Planned day", self.day)):
            control.setAccessibleName(name)
            form.addRow(name, control)
        self.add = QPushButton("Add task")
        self.save = QPushButton("Save changes")
        self.complete = QPushButton("Complete task")
        self.new = QPushButton("Clear selection")
        self.buttons = QHBoxLayout()
        for button in (self.add, self.save, self.complete, self.new):
            self.buttons.addWidget(button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Task status")
        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addLayout(form)
        layout.addLayout(self.buttons)
        layout.addWidget(self.status)
        self.add.clicked.connect(lambda: self.write("add"))
        self.save.clicked.connect(lambda: self.write("edit"))
        self.complete.clicked.connect(lambda: self.write("complete"))
        self.new.clicked.connect(lambda: self.list.setCurrentRow(-1))
        self.list.currentItemChanged.connect(self.load_selection)
        self.load_selection()
        self.refresh()

    def refresh(self) -> None:
        if self.store is None:
            self.status.setText(self.error)
            return
        try:
            tasks = self.store.open_tasks()
        except TaskError as error:
            self.status.setText(str(error))
            return
        selected = self.selected_id()
        self.records = {task.id: task for task in tasks}
        self.list.clear()
        for task in tasks:
            item = QListWidgetItem(task_line(task), self.list)
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            if task.id == selected:
                self.list.setCurrentItem(item)

    def selected_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def load_selection(self) -> None:
        task = self.records.get(self.selected_id() or 0)
        self.title.setText(task.title if task else "")
        self.deadline.setText((task.deadline or "") if task else "")
        self.minutes.setText(str(task.minutes) if task and task.minutes is not None else "")
        self.day.setText((task.planned_day or "") if task else "")
        self.title.setReadOnly(task is not None)
        self.add.setEnabled(self.store is not None and task is None)
        self.save.setEnabled(self.store is not None and task is not None)
        self.complete.setEnabled(self.save.isEnabled())
        self.new.setEnabled(task is not None)

    def write(self, action: Literal["add", "edit", "complete"]) -> None:
        store = self.store
        if store is None:
            self.status.setText(self.error)
            return
        task_id = self.selected_id()
        fields = (self.deadline.text(), self.minutes.text(), self.day.text())
        today = date.today()
        try:
            if action == "add":
                store.add(self.title.text(), *fields, today=today)
                self.load_selection()
            elif task_id is None:
                raise TaskError("Select an open task first.")
            elif action == "edit":
                store.edit(task_id, *fields, today=today)
            else:
                store.complete(task_id)
        except TaskError as error:
            self.status.setText(str(error))
            return
        self.status.setText("Task completed." if action == "complete" else "Task saved.")
        self.changed.emit()


class TodayView(QWidget):
    def __init__(self, store: TaskStore | None, error: str, calendar: CalendarView) -> None:
        super().__init__()
        self.store = store
        self.error = error
        self.calendar = calendar
        self.list = QListWidget()
        self.list.setAccessibleName("Tasks due or planned today")
        self.budget = QLineEdit(placeholderText="Available minutes")
        self.budget.setAccessibleName("Available minutes")
        self.suggest = QPushButton("Suggest order")
        self.output = QPlainTextEdit(readOnly=True)
        self.output.setAccessibleName("Suggested plan")
        self.start = QLineEdit("08:00")
        self.end = QLineEdit("18:00")
        self.start.setAccessibleName("Planning window start")
        self.end.setAccessibleName("Planning window end")
        self.calendar_plan = QPushButton("Plan with openings")
        window = QHBoxLayout()
        for widget in (QLabel("Local window (HH:MM)"), self.start, self.end, self.calendar_plan):
            window.addWidget(widget)
        row = QHBoxLayout()
        row.addWidget(QLabel("Available minutes:"))
        row.addWidget(self.budget)
        row.addWidget(self.suggest)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        self.scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)
        layout = QVBoxLayout(content)
        self.briefing_enabled = QCheckBox("Show Today briefing")
        self.briefing = QLabel()
        self.briefing.setTextFormat(Qt.TextFormat.PlainText)
        self.briefing.setWordWrap(True)
        self.briefing.setAccessibleName("Today briefing")
        self.reminders_enabled = QCheckBox("Task reminders")
        self.close_to_tray = QCheckBox("Close to tray")
        self.reminders_button = QPushButton("Reminder history")
        self.reminder_status = QLabel()
        self.reminder_status.setWordWrap(True)
        settings = QHBoxLayout()
        for widget in (self.briefing_enabled, self.reminders_enabled, self.close_to_tray, self.reminders_button):
            settings.addWidget(widget)
        layout.addLayout(settings)
        layout.addWidget(self.briefing)
        self.briefing_layout = layout
        layout.addWidget(self.reminder_status)
        layout.addWidget(QLabel("今日到期及逾期"))
        self.due_list = QListWidget()
        self.due_list.setAccessibleName("今日到期及逾期")
        self.due_list.setMaximumHeight(92)
        layout.addWidget(self.due_list)
        layout.addWidget(QLabel("Open tasks due or planned today"))
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        layout.addWidget(self.output, 2)
        layout.addLayout(window)
        layout.addWidget(calendar, 3)
        self.suggest.clicked.connect(self.show_plan)
        self.budget.returnPressed.connect(self.show_plan)
        self.calendar_plan.clicked.connect(self.show_calendar_plan)

    def add_briefing_section(self, section: QWidget) -> None:
        self.briefing_layout.insertWidget(self.briefing_layout.indexOf(self.briefing) + 1, section)

    def show_calendar_plan(self) -> None:
        snapshot = self.calendar.snapshot
        if snapshot is None or snapshot.failures:
            self.output.setPlainText("Calendar availability is unavailable. Refresh or use Suggest order with a manual minute budget.")
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
            budget = minutes_value(self.budget.text(), "Budget", optional=True, minimum=0)
            now = datetime.now(snapshot.zone)
            free = openings(snapshot, start, end, now)
            schedule = place_tasks(snapshot, free, self.store.open_tasks(), now, budget)
            ended = now.timestamp() >= window_end(snapshot.day, end, snapshot.zone).timestamp()
            self.output.setPlainText(availability_text(snapshot, free, schedule, window_ended=ended))
        except TaskError as error:
            self.output.setPlainText(str(error))

    def refresh(self) -> None:
        self.list.clear()
        self.due_list.clear()
        self.output.clear()
        try:
            if self.store is None:
                raise TaskError(self.error)
            today = datetime.now(self.calendar.zone).date()
            for task in self.store.open_tasks():
                if is_today(task, today):
                    self.list.addItem(task_line(task))
                if is_due(task, today):
                    item = QListWidgetItem(task_line(task), self.due_list)
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
        except TaskError as error:
            self.due_list.addItem(str(error))
            self.output.setPlainText(str(error))

    def show_plan(self) -> None:
        try:
            if self.store is None:
                raise TaskError(self.error)
            budget = minutes_value(self.budget.text(), "Budget", minimum=0)
            assert budget is not None
            self.output.setPlainText(plan_text(plan_today(self.store.open_tasks(), budget, datetime.now())))
        except TaskError as error:
            self.output.setPlainText(str(error))
