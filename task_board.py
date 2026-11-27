"""Google-Tasks-style board for the 待办 page: category columns, a category sidebar and a create/edit dialog."""
import re
from datetime import date
from functools import partial
from html import escape
from typing import NamedTuple

from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from desktop_style import COLORS
from tasks import Task, TaskError, TaskStore, deadline_order, task_fields

UNCATEGORIZED = "未分类"
EMPTY_TEXT = "还没有待办。点右上角“＋ 新建待办”，或直接告诉千早。"
MODES = (("open", "未完成"), ("complete", "已完成"), ("all", "全部"))
COLUMN_MIN, COLUMN_MAX, SIDEBAR_WIDTH = 280, 360, 210
OVERDUE, DANGER = "#ff8f8f", "#ff9b9b"
Key = str | None

_CHIP = """
QLabel#mailChip { background: #17324d; color: #9fd0f5; border: 1px solid #2f5f8f; border-radius: 8px;
                  padding: 1px 8px; font-size: 11px; }
"""

_STYLE = """
QWidget#taskBoardRoot { background: @BG; }
QLabel#taskPageTitle { font-size: 18px; font-weight: 700; color: #f2f7fc; }
QFrame#segment { background: transparent; border: 0; }
QPushButton[segment] { background: #1a2b3f; border: 1px solid @BORDER; border-radius: 0; padding: 6px 16px; }
QPushButton[segment="first"] { border-top-left-radius: 9px; border-bottom-left-radius: 9px; }
QPushButton[segment="mid"] { border-left-width: 0; }
QPushButton[segment="last"] { border-left-width: 0; border-top-right-radius: 9px; border-bottom-right-radius: 9px; }
QPushButton[primary="true"]:disabled { background: #152234; color: #70869b; border-color: #24364b; }
QPushButton[segment]:hover { background: #26405c; }
QPushButton[segment]:checked { background: @ACCENT_DARK; color: #ffffff; border-color: #8cc3f0; }
QPushButton[segment]:focus { border: 1px solid #8cc9f5; padding: 6px 16px; }

QFrame#sidebar { background: transparent; border: 0; }
QPushButton#allRow { text-align: left; background: transparent; border: 1px solid transparent;
                     border-radius: 9px; padding: 7px 10px; font-weight: 600; }
QPushButton#allRow:hover { background: #1c2f45; }
QPushButton#allRow:checked { background: #1d3f63; color: #ffffff; border-color: #2f5f8f; }
QPushButton#allRow:focus { border: 1px solid @ACCENT; padding: 7px 10px; }
QLabel#sideHeading { color: @MUTED; font-size: 12px; font-weight: 700; padding: 10px 10px 2px 10px; }
QScrollArea#sideScroll, QScrollArea#sideScroll > QWidget > QWidget { background: transparent; border: 0; }
QFrame#categoryRow { background: transparent; border: 0; border-radius: 9px; }
QFrame#categoryRow:hover { background: #172a40; }
QLabel#categoryCount { color: @MUTED; font-size: 12px; }
QToolButton#menuButton { color: @MUTED; border: 0; border-radius: 8px; padding: 0 6px; font-weight: 700; }
QToolButton#menuButton:hover, QToolButton#menuButton:focus { background: #26405c; color: #ffffff; }
QToolButton#menuButton::menu-indicator { image: none; width: 0; }
QPushButton#newCategory { text-align: left; background: transparent; border: 1px solid transparent;
                          border-radius: 9px; padding: 7px 10px; color: #8cc9f5; }
QPushButton#newCategory:hover { background: #1c2f45; }
QPushButton#newCategory:focus { border: 1px solid @ACCENT; padding: 7px 10px; }

QScrollArea#board, QScrollArea#board > QWidget > QWidget { background: transparent; border: 0; }
QFrame#taskColumn { background: @PANEL; border: 1px solid #2a4560; border-radius: 14px; }
QLabel#columnTitle { font-size: 14px; font-weight: 700; color: #f2f7fc; }
QLabel#columnCount { color: @MUTED; font-size: 12px; padding-top: 1px; }
QPushButton#addRow { text-align: left; background: transparent; border: 1px solid transparent;
                     border-radius: 10px; padding: 7px 10px; color: #8cc9f5; }
QPushButton#addRow:hover { background: #1f3550; }
QPushButton#addRow:focus { border: 1px solid @ACCENT; padding: 7px 10px; }
QScrollArea#columnScroll, QScrollArea#columnScroll > QWidget > QWidget { background: transparent; border: 0; }
QLabel#columnEmpty { color: #7f96ad; font-size: 12px; padding: 6px 4px; }
QPushButton#doneToggle { text-align: left; background: transparent; border: 1px solid transparent;
                         border-radius: 8px; padding: 6px 4px; color: @MUTED; font-size: 12px; font-weight: 700; }
QPushButton#doneToggle:hover { color: #ffffff; }
QPushButton#doneToggle:focus { border: 1px solid @ACCENT; padding: 6px 4px; }

QFrame#taskCard { background: @CARD; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#taskCard:hover { background: #203248; border-color: @ACCENT; }
QFrame#taskCard[done="true"] { background: #172536; }
QFrame#taskCard[selected="true"] { background: #21405f; border: 2px solid @ACCENT; }
QFrame#taskCard:focus { border: 2px solid #8cc9f5; }
QLabel#cardTitle { font-size: 13px; font-weight: 600; color: @TEXT; }
QLabel#cardTitle[done="true"] { color: @MUTED; text-decoration: line-through; }
QLabel#cardCaption { font-size: 12px; color: @MUTED; }
QAbstractButton#checkCircle { background: transparent; border: 0; }

QLabel#taskEmpty { color: @MUTED; font-size: 13px; padding: 24px; }
QLabel#taskStatus { color: @MUTED; font-size: 12px; }
QLabel#taskStatus[error="true"] { color: #ff9b9b; }
""" + _CHIP

_EDITOR_STYLE = """
QLabel#editorLabel { color: @MUTED; }
QLabel#editorStatus { color: #ff9b9b; }
QPushButton[danger="true"] { color: #ff9b9b; border-color: #7a3b44; }
QPushButton[danger="true"]:hover { background: #3a1f28; border-color: #b0525e; }
QPushButton[primary="true"]:disabled { background: #152234; color: #70869b; border-color: #24364b; }
QComboBox QLineEdit, QComboBox QLineEdit:focus { border: 0; padding: 0; background: transparent; }
""" + _CHIP


def _themed(css: str) -> str:
    for name in sorted(COLORS, key=len, reverse=True):
        css = css.replace(f"@{name}", COLORS[name])
    return css


BOARD_STYLE, EDITOR_STYLE = _themed(_STYLE), _themed(_EDITOR_STYLE)


def soft_wrap(text: str) -> str:
    """Allow a line break inside very long unbroken words so labels never need a horizontal scrollbar."""
    return re.sub(r"(\S{16})(?=\S)", "\\1\u200b", text)


def keep_together(text: str) -> str:
    """Stop a short field (e.g. 无截止日期) from wrapping in the middle: bind CJK runs and spaces with joiners."""
    return re.sub(r"(?<=[\u3400-\u9fff])(?=[\u3400-\u9fff])", "\u2060", text).replace(" ", "\u00a0").replace(
        "–", "\u2060–\u2060")


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def is_overdue(task: Task, today: date) -> bool:
    return task.status == "open" and bool(task.deadline) and task.deadline[:10] < today.isoformat()


def discard(widget: QWidget) -> None:
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


class CheckButton(QAbstractButton):
    """A round check mark: outlined for open tasks, filled for completed ones."""

    def __init__(self, done: bool, name: str) -> None:
        super().__init__()
        self.done = done
        self.setObjectName("checkCircle")
        self.setFixedSize(26, 26)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(name)
        self.setToolTip("重新打开" if done else "标为完成")

    def sizeHint(self) -> QSize:
        return QSize(26, 26)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(COLORS["ACCENT"])
        if self.hasFocus():
            painter.setPen(QPen(QColor("#8cc9f5"), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(1.5, 1.5, 23, 23))
        circle = QRectF(4, 4, 18, 18)
        hover = self.underMouse()
        if self.done:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(circle)
        else:
            painter.setPen(QPen(accent if hover else QColor("#7d97b3"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(circle)
        if self.done or hover:
            mark = QPainterPath()
            mark.moveTo(8.6, 13.4)
            mark.lineTo(11.6, 16.4)
            mark.lineTo(17.6, 9.8)
            color = QColor("#ffffff") if self.done else QColor(accent)
            painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(mark)


class TaskCard(QFrame):
    opened = Signal(int)
    toggled = Signal(int)

    def __init__(self, task: Task, today: date) -> None:
        super().__init__()
        self.task = task
        done = task.status == "complete"
        fields = task_fields(task)
        self.setObjectName("taskCard")
        self.setProperty("done", done)
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(fields.line())
        self.setToolTip(escape(fields.details()).replace("\n", "<br>"))
        self._pressed = False

        self.check = CheckButton(done, f"{'重新打开' if done else '标为完成'}：{task.title}")
        self.check.clicked.connect(lambda: self.toggled.emit(task.id))
        self.title = QLabel(soft_wrap(task.title))
        self.title.setObjectName("cardTitle")
        self.title.setProperty("done", done)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        parts = [escape(soft_wrap(fields.category))]
        # Each field stays in one piece when the caption wraps; the bar is glued to the field before it.
        parts += [escape(keep_together(part)) for part in fields[2:]]
        if is_overdue(task, today):
            parts[3] = f'<span style="color:{OVERDUE}; font-weight:700">{parts[3]}</span>'
        self.caption = QLabel("\u2060｜".join(parts))
        self.caption.setObjectName("cardCaption")
        self.caption.setTextFormat(Qt.TextFormat.RichText)
        labels = [self.title, self.caption]
        self.chip: QLabel | None = None
        if task.mail_linked:
            self.chip = QLabel("来自邮件")
            self.chip.setObjectName("mailChip")
        body = QVBoxLayout()
        body.setSpacing(3)
        for label in labels:
            label.setWordWrap(True)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            body.addWidget(label)
        if self.chip is not None:
            self.chip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            body.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignLeft)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 10, 9)
        layout.setSpacing(6)
        layout.addWidget(self.check, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(body, 1)

    def set_selected(self, selected: bool) -> None:
        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            repolish(self)

    def mousePressEvent(self, event) -> None:
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self._pressed = False
            self.opened.emit(self.task.id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.opened.emit(self.task.id)
            return
        super().keyPressEvent(event)


class AddLine(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        leaving = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                     Qt.FocusReason.MouseFocusReason)
        if leaving and not self.text().strip():
            QTimer.singleShot(0, lambda: isValid(self) and self.cancelled.emit())


def menu_button(name: str, menu: QMenu) -> QToolButton:
    button = QToolButton()
    button.setObjectName("menuButton")
    button.setText("⋯")
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setMenu(menu)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAccessibleName(f"类别“{name}”的更多操作")
    button.setToolTip("重命名或删除类别")
    button.setFixedSize(28, 24)
    return button


class CategoryRow(QFrame):
    """Sidebar row: a checkbox that shows or hides the category's column, its open count and a ⋯ menu."""

    def __init__(self, name: str, menu: QMenu | None) -> None:
        super().__init__()
        self.setObjectName("categoryRow")
        self.check = QCheckBox()
        self.check.setAccessibleName(f"显示类别：{name}")
        self.label = QLabel(soft_wrap(name))
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.count = QLabel("0")
        self.count.setObjectName("categoryCount")
        self.count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.menu_button = menu_button(name, menu) if menu is not None else None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(self.check)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.count)
        if self.menu_button is not None:
            layout.addWidget(self.menu_button)
        else:
            layout.addSpacing(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_count(self, count: int) -> None:
        self.count.setText(str(count))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.check.click()
        super().mouseReleaseEvent(event)


class TaskColumn(QFrame):
    submitted = Signal(object, str)
    started = Signal(object)
    typed = Signal(object, str)
    cancelled = Signal(object)
    expansion = Signal(object, bool)

    def __init__(self, key: Key, count_text: str, menu: QMenu | None, *, can_add: bool) -> None:
        super().__init__()
        self.key = key
        name = key or UNCATEGORIZED
        self.cards: dict[int, TaskCard] = {}
        self.order: list[QWidget] = []
        self.done_toggle: QPushButton | None = None
        self.setObjectName("taskColumn")
        self.title = QLabel(soft_wrap(name))
        self.title.setObjectName("columnTitle")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.count = QLabel(count_text)
        self.count.setObjectName("columnCount")
        self.menu_button = menu_button(name, menu) if menu is not None else None
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.count, 0, Qt.AlignmentFlag.AlignVCenter)
        if self.menu_button is not None:
            head.addWidget(self.menu_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.add_button = QPushButton("＋ 添加待办")
        self.add_button.setObjectName("addRow")
        self.add_button.setAccessibleName(f"在“{name}”中添加待办")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_edit = AddLine()
        self.add_edit.setPlaceholderText("输入标题，Enter 添加，Esc 取消")
        self.add_edit.setAccessibleName(f"“{name}”的新待办标题")
        self.add_edit.hide()
        self.add_button.setVisible(can_add)
        self.add_button.clicked.connect(self._start_adding)
        self.add_edit.returnPressed.connect(lambda: self.submitted.emit(self.key, self.add_edit.text()))
        self.add_edit.textChanged.connect(lambda text: self.typed.emit(self.key, text))
        self.add_edit.cancelled.connect(lambda: self.cancelled.emit(self.key))

        self.host = QWidget()
        self.card_layout = QVBoxLayout(self.host)
        self.card_layout.setContentsMargins(0, 2, 4, 2)
        self.card_layout.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("columnScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.host)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 8, 10)
        outer.setSpacing(6)
        outer.addLayout(head)
        outer.addWidget(self.add_button)
        outer.addWidget(self.add_edit)
        outer.addWidget(self.scroll, 1)
        self.order = [w for w in (self.menu_button, self.add_button, self.add_edit) if w is not None]

    def _start_adding(self) -> None:
        self.set_adding(True, focus=True)
        self.started.emit(self.key)

    def set_adding(self, adding: bool, text: str = "", *, focus: bool = False) -> None:
        if adding:
            with QSignalBlocker(self.add_edit):
                self.add_edit.setText(text)
        self.add_button.setVisible(not adding)
        self.add_edit.setVisible(adding)
        if adding and focus:
            self.add_edit.setFocus()
        elif not adding and focus:
            self.add_button.setFocus()

    def _append(self, widget: QWidget) -> None:
        self.card_layout.addWidget(widget)

    def fill(self, open_cards: list[TaskCard], done_cards: list[TaskCard], *, done_total: int, expanded: bool,
             collapsible: bool, hint: str) -> None:
        for card in open_cards:
            self._append_card(card)
        if hint:
            label = QLabel(hint)
            label.setObjectName("columnEmpty")
            label.setWordWrap(True)
            self._append(label)
        if collapsible and done_total:
            toggle = QPushButton(f"{'▾' if expanded else '▸'} 已完成 ({done_total})")
            toggle.setObjectName("doneToggle")
            toggle.setAccessibleName(f"已完成 ({done_total})：{'收起' if expanded else '展开'}")
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            toggle.clicked.connect(lambda: self.expansion.emit(self.key, not expanded))
            self.done_toggle = toggle
            self.order.append(toggle)
            self._append(toggle)
        for card in done_cards:
            self._append_card(card)
        self.card_layout.addStretch(1)

    def _append_card(self, card: TaskCard) -> None:
        self.cards[card.task.id] = card
        self.order.extend((card, card.check))
        self._append(card)

    def shown_tasks(self) -> list[Task]:
        return [card.task for card in self.cards.values()]


class EditorValues(NamedTuple):
    title: str
    notes: str
    category: str
    minutes: str
    day: str
    start: str
    end: str
    deadline: str


class TaskEditor(QDialog):
    """Create/edit popup. One instance lives for the lifetime of the view; `accept()` saves and stays open on error."""

    def __init__(self, view: "TaskView") -> None:
        super().__init__(view)
        self.view = view
        self.task_id: int | None = None
        self.setWindowTitle("新建待办")
        self.setMinimumWidth(460)
        self.setStyleSheet(EDITOR_STYLE)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self.chip = QLabel("来自邮件")
        self.chip.setObjectName("mailChip")
        self.title = QLineEdit(placeholderText="必填")
        self.notes = QPlainTextEdit(placeholderText="补充说明（选填）")
        self.notes.setTabChangesFocus(True)
        self.notes.setFixedHeight(self.notes.fontMetrics().lineSpacing() * 3 + 22)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.category.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.category.lineEdit().setPlaceholderText("选择或输入新类别")
        self.minutes = QLineEdit(placeholderText="选填，例如 30")
        self.day = QLineEdit(placeholderText="YYYY-MM-DD / 今天 / 明天 / 周五")
        self.start = QLineEdit(placeholderText="HH:MM")
        self.end = QLineEdit(placeholderText="HH:MM")
        self.deadline = QLineEdit(placeholderText="YYYY-MM-DD，可加 HH:MM")
        names = (
            (self.title, "标题"), (self.notes, "备注"), (self.category, "类别"), (self.minutes, "预估时长（分钟）"),
            (self.day, "安排日期"), (self.start, "开始时间"), (self.end, "结束时间"), (self.deadline, "截止日期"),
        )
        for widget, name in names:
            widget.setAccessibleName(name)
        self.category.lineEdit().setAccessibleName("类别")

        slot = QHBoxLayout()
        slot.setSpacing(8)
        for text, edit in (("开始", self.start), ("结束", self.end)):
            label = QLabel(text)
            label.setObjectName("editorLabel")
            label.setBuddy(edit)
            slot.addWidget(label)
            slot.addWidget(edit, 1)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        for label, widget in (("标题", self.title), ("备注", self.notes), ("类别", self.category),
                              ("预估时长（分钟）", self.minutes), ("安排日期", self.day)):
            form.addRow(self._label(label, widget), widget)
        form.addRow(self._label("已安排时段", self.start), slot)
        form.addRow(self._label("截止日期", self.deadline), self.deadline)

        self.status = QLabel()
        self.status.setObjectName("editorStatus")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("待办编辑状态")
        self.delete = QPushButton("删除")
        self.delete.setProperty("danger", True)
        self.complete = QPushButton("标为完成")
        self.reopen = QPushButton("重新打开")
        self.cancel = QPushButton("取消")
        self.save = QPushButton("保存")
        self.save.setProperty("primary", True)
        self.save.setDefault(True)
        for button in (self.delete, self.complete, self.reopen, self.cancel):
            button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.delete)
        buttons.addStretch(1)
        for button in (self.reopen, self.complete, self.cancel, self.save):
            buttons.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self.save.clicked.connect(self.accept)
        self.cancel.clicked.connect(self.reject)
        self.complete.clicked.connect(view.complete_from_editor)
        self.reopen.clicked.connect(view.reopen_from_editor)
        self.delete.clicked.connect(view.delete_from_editor)
        self.setTabOrder(self.title, self.notes)
        self.setTabOrder(self.notes, self.category)
        self.setTabOrder(self.category, self.minutes)
        self.setTabOrder(self.minutes, self.day)
        self.setTabOrder(self.day, self.start)
        self.setTabOrder(self.start, self.end)
        self.setTabOrder(self.end, self.deadline)

    @staticmethod
    def _label(text: str, buddy: QWidget) -> QLabel:
        label = QLabel(text)
        label.setObjectName("editorLabel")
        label.setBuddy(buddy)
        return label

    def load(self, task: Task | None, *, category: str | None = None) -> None:
        self.task_id = task.id if task else None
        self.setWindowTitle("编辑待办" if task else "新建待办")
        names = [UNCATEGORIZED, *self.view.categories]
        wanted = (task.category if task else category) or UNCATEGORIZED
        wanted = self.view.canonical(wanted) or UNCATEGORIZED
        if wanted not in names:
            names.append(wanted)
        self.category.clear()
        self.category.addItems(names)
        self.category.setCurrentIndex(names.index(wanted))
        self.title.setText(task.title if task else "")
        self.notes.setPlainText((task.notes or "") if task else "")
        self.minutes.setText(str(task.minutes) if task and task.minutes is not None else "")
        self.day.setText((task.planned_day or "") if task else "")
        self.start.setText((task.start or "") if task else "")
        self.end.setText((task.end or "") if task else "")
        self.deadline.setText((task.deadline or "") if task else "")
        self.chip.setVisible(bool(task and task.mail_linked))
        self.delete.setVisible(task is not None)
        self.status.clear()
        self.sync_actions(task)

    def sync_actions(self, task: Task | None) -> None:
        """Show 标为完成 for open tasks and 重新打开 for completed ones; both are hidden while creating."""
        self.complete.setVisible(task is not None and task.status == "open")
        self.reopen.setVisible(task is not None and task.status == "complete")

    def values(self) -> EditorValues:
        typed = " ".join(self.category.currentText().split())
        category = "" if typed.casefold() in ("", UNCATEGORIZED) else (self.view.canonical(typed) or typed)
        return EditorValues(
            self.title.text(), self.notes.toPlainText(), category, self.minutes.text(), self.day.text(),
            self.start.text(), self.end.text(), self.deadline.text(),
        )

    def show_error(self, text: str) -> None:
        self.status.setText(text)

    def accept(self) -> None:
        if self.view.save_from_editor(self):
            super().accept()

    def done(self, result: int) -> None:
        super().done(result)
        self.view.editor_closed()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.view.sync_complete()


class TaskView(QWidget):
    """Board of category columns with a sidebar; the public API is documented in the module tests."""

    changed = Signal()

    def __init__(self, store: TaskStore | None, error: str) -> None:
        super().__init__()
        self.store = store
        self.error = error
        self.records: dict[int, Task] = {}
        self.categories: list[str] = []
        self.mode = "open"
        self.columns: dict[Key, TaskColumn] = {}
        self.category_rows: dict[Key, CategoryRow] = {}
        self._canon: dict[str, str] = {}
        self._hidden: set[Key] = set()
        self._expanded: dict[Key, bool] = {}
        self._selected: int | None = None
        self._adding: tuple[Key, str] | None = None
        self._cards: dict[int, TaskCard] = {}
        self._rebuilding = False
        self._load_failed = False
        self._database_error = False

        self.root = QWidget()
        self.root.setObjectName("taskBoardRoot")
        self.root.setStyleSheet(BOARD_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self.page_title = QLabel("待办")
        self.page_title.setObjectName("taskPageTitle")
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QPushButton] = {}
        segment = QFrame()
        segment.setObjectName("segment")
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(0)
        for index, (mode, label) in enumerate(MODES):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(mode == self.mode)
            button.setProperty("segment", "first" if index == 0 else "last" if index == len(MODES) - 1 else "mid")
            button.setAccessibleName(f"显示{label}待办" if mode != "all" else "显示全部待办")
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            segment_layout.addWidget(button)
            button.clicked.connect(partial(self.set_mode, mode))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索待办标题")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索待办")
        self.search.setMinimumWidth(180)
        self.search.setMaximumWidth(280)
        self.new_button = QPushButton("＋ 新建待办")
        self.new_button.setProperty("primary", True)
        self.new_button.setAccessibleName("新建待办")
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(self.page_title)
        header.addSpacing(6)
        header.addWidget(segment)
        header.addStretch(1)
        header.addWidget(self.search)
        header.addWidget(self.new_button)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
        self.all_button = QPushButton("全部待办")
        self.all_button.setObjectName("allRow")
        self.all_button.setCheckable(True)
        self.all_button.setChecked(True)
        self.all_button.setAccessibleName("全部待办：显示所有类别")
        self.side_heading = QLabel("类别")
        self.side_heading.setObjectName("sideHeading")
        self.side_host = QWidget()
        self.side_layout = QVBoxLayout(self.side_host)
        self.side_layout.setContentsMargins(0, 0, 4, 0)
        self.side_layout.setSpacing(1)
        self.side_layout.addStretch(1)
        self.side_scroll = QScrollArea()
        self.side_scroll.setObjectName("sideScroll")
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side_scroll.setWidget(self.side_host)
        self.new_category = QPushButton("＋ 新建类别")
        self.new_category.setObjectName("newCategory")
        self.new_category.setAccessibleName("新建类别")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(2)
        side.addWidget(self.all_button)
        side.addWidget(self.side_heading)
        side.addWidget(self.side_scroll, 1)
        side.addWidget(self.new_category)

        self.board = QScrollArea()
        self.board.setObjectName("board")
        self.board.setWidgetResizable(True)
        self.board.setFrameShape(QFrame.Shape.NoFrame)
        self.board.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.board.setAccessibleName("待办看板")
        self.board_host = QWidget()
        self.board_layout = QHBoxLayout(self.board_host)
        self.board_layout.setContentsMargins(0, 0, 0, 0)
        self.board_layout.setSpacing(12)
        self.board_layout.addStretch(1)
        self.board.setWidget(self.board_host)
        self.board.viewport().installEventFilter(self)
        self.empty = QLabel()
        self.empty.setObjectName("taskEmpty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_stack = QStackedWidget()
        self.main_stack.addWidget(self.board)
        self.main_stack.addWidget(self.empty)

        self.status = QLabel()
        self.status.setObjectName("taskStatus")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("待办操作状态")
        self.status.setMinimumHeight(18)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self.sidebar)
        body.addWidget(self.main_stack, 1)
        layout.addLayout(header)
        layout.addLayout(body, 1)
        layout.addWidget(self.status)

        self.editor_dialog = TaskEditor(self)
        self.complete = self.editor_dialog.complete
        self.complete.setEnabled(False)

        self.new_button.clicked.connect(lambda: self.open_editor(None, category=self._default_category()))
        self.search.textChanged.connect(lambda: self._rebuild(sidebar=False))
        self.all_button.clicked.connect(self.show_all_columns)
        self.new_category.clicked.connect(lambda: self.create_category())
        self.refresh()

    # ---- public API -------------------------------------------------------------------------------------------

    @property
    def editor(self) -> TaskEditor | None:
        return self.editor_dialog if self.editor_dialog.isVisible() else None

    def today(self) -> date:
        return date.today()

    def refresh(self) -> None:
        """Re-read the store (it may have been changed by the chat assistant) and redraw, keeping the view state."""
        self._reload()

    def selected_id(self) -> int | None:
        return self._selected if self._selected in self.records else None

    def visible_tasks(self) -> list[Task]:
        return [task for column in self.columns.values() for task in column.shown_tasks()]

    def visible_columns(self) -> list[Key]:
        return list(self.columns)

    def focus_primary(self) -> None:
        for column in self.columns.values():
            for card in column.cards.values():
                card.setFocus()
                return
        if self.new_button.isEnabled():
            self.new_button.setFocus()
        else:
            self.board.setFocus()

    def select_task(self, task_id: int) -> bool:
        """Reveal a task (clearing only the filters that hide it), select it and open its editor."""
        if self.store is None or not self._reload(rebuild=False) or task_id not in self.records:
            return False
        self._selected = task_id
        self._reveal(task_id)
        self._rebuild()
        self._scroll_to(task_id)
        self.open_editor(task_id)
        return True

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        with QSignalBlocker(self.mode_group):
            self.mode_buttons[mode].setChecked(True)
        self._rebuild(sidebar=False)

    def show_all_columns(self) -> None:
        self._hidden.clear()
        self._rebuild()

    def canonical(self, name: str | None) -> str | None:
        """Spelling of a category as stored (categories are case-insensitive)."""
        text = " ".join((name or "").split())
        return self._canon.get(text.casefold(), text) or None

    def open_editor(self, task_id: int | None = None, *, category: str | None = None) -> TaskEditor | None:
        if self.store is None:
            return None
        task = self.records.get(task_id) if task_id is not None else None
        if task_id is not None and task is None:
            return None
        if task is not None:
            self._set_selected(task_id)
        editor = self.editor_dialog
        editor.load(task, category=category)
        editor.open()
        self.sync_complete()
        editor.title.setFocus()
        return editor

    # ---- confirmations (overridable in tests) -----------------------------------------------------------------

    def confirm(self, title: str, text: str, accept: str, parent: QWidget | None = None) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, parent or self)
        yes = box.addButton(accept, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is yes

    def ask_name(self, title: str, label: str, text: str = "") -> str | None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(text)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        dialog.setMinimumWidth(340)
        return dialog.textValue() if dialog.exec() else None

    # ---- category actions -------------------------------------------------------------------------------------

    def create_category(self, name: str | None = None) -> bool:
        if self.store is None:
            return False
        if name is None:
            name = self.ask_name("新建类别", "类别名称")
        if name is None:
            return False
        if " ".join(name.split()) == UNCATEGORIZED:
            self._say(f"“{UNCATEGORIZED}”是系统保留名称，请换一个类别名称。", error=True)
            return False
        existed = (self.canonical(name) or "") in self.categories
        try:
            stored = self.store.add_category(name)
        except TaskError as error:
            self._fail(error)
            return False
        self._hidden.discard(stored)
        if existed:
            self._reload()
            self._say(f"类别“{stored}”已经存在。")
        else:
            self._after_write(f"已新建类别“{stored}”。")
        self._later(lambda: self._scroll_to_column(stored))
        return True

    def rename_category(self, name: str, new: str | None = None) -> bool:
        if self.store is None:
            return False
        if new is None:
            new = self.ask_name("重命名类别", "新的类别名称", name)
        if new is None or " ".join(new.split()) == name:
            return False
        if " ".join(new.split()) == UNCATEGORIZED:
            self._say(f"“{UNCATEGORIZED}”是系统保留名称，请换一个类别名称。", error=True)
            return False
        try:
            stored = self.store.rename_category(name, new)
        except TaskError as error:
            self._reload()
            self._fail(error)
            return False
        if name in self._hidden:
            self._hidden.discard(name)
            self._hidden.add(stored)
        if name in self._expanded:
            self._expanded[stored] = self._expanded.pop(name)
        if self._adding and self._adding[0] == name:
            self._adding = (stored, self._adding[1])
        self._after_write(f"已把类别“{name}”重命名为“{stored}”。")
        return True

    def delete_category(self, name: str) -> bool:
        if self.store is None:
            return False
        count = sum(1 for task in self.records.values() if self._key_of(task) == name)
        detail = f"该类别下的 {count} 条待办不会被删除，会保留为“{UNCATEGORIZED}”。" if count else (
            "这个类别目前没有待办。")
        if not self.confirm("删除类别", f"确定要删除类别“{name}”吗？\n{detail}", "删除类别"):
            return False
        try:
            self.store.delete_category(name)
        except TaskError as error:
            self._reload()
            self._fail(error)
            return False
        self._hidden.discard(name)
        self._expanded.pop(name, None)
        if self._adding and self._adding[0] == name:
            self._adding = None
        self._after_write(f"已删除类别“{name}”，其中的待办已移到“{UNCATEGORIZED}”。")
        return True

    # ---- task actions -----------------------------------------------------------------------------------------

    def toggle_task(self, task_id: int) -> bool:
        task = self.records.get(task_id)
        if task is None or self.store is None:
            return False
        return self._set_done(task_id, task.status == "open")

    def _set_done(self, task_id: int, done: bool) -> bool:
        task = self.records.get(task_id)
        if task is None or self.store is None:
            return False
        try:
            if done:
                self.store.complete(task_id)
            else:
                self.store.reopen(task_id)
        except TaskError as error:
            self._reload()
            self._fail(error)
            return False
        self._after_write(f"已完成“{task.title}”。" if done else f"已重新打开“{task.title}”。")
        return True

    def add_inline(self, key: Key, title: str) -> bool:
        if self.store is None:
            return False
        try:
            task_id = self.store.add(title, "", "", "", today=self.today(), category=key or "")
        except TaskError as error:
            self._fail(error)
            return False
        self._adding = (key, "")
        self._hidden.discard(key)
        self._after_write(f"已添加待办“{title.strip()}”。")
        self._later(lambda: self._scroll_to(task_id))
        return True

    def save_from_editor(self, editor: TaskEditor) -> bool:
        if self.store is None:
            editor.show_error(self.error or "无法打开本机待办数据库。")
            return False
        values = editor.values()
        fields = (values.deadline, values.minutes, values.day)
        details = dict(category=values.category, start=values.start, end=values.end)
        try:
            if editor.task_id is None:
                task_id = self.store.add(values.title, *fields, today=self.today(), notes=values.notes, **details)
                message = f"已新建待办“{values.title.strip()}”。"
            else:
                task_id = editor.task_id
                self.store.edit_details(task_id, values.title, *fields, today=self.today(), notes=values.notes,
                                        **details)
                message = f"已保存待办“{values.title.strip()}”。"
        except TaskError as error:
            editor.show_error(self._message(error))
            return False
        self._selected = task_id
        if self._reload(rebuild=False):
            self._reveal(task_id)
            self._rebuild()
            self._scroll_to(task_id)
            self._say(message)
        self.changed.emit()
        return True

    def complete_from_editor(self) -> None:
        editor = self.editor_dialog
        if editor.task_id is not None and self._set_done(editor.task_id, True):
            editor.done(QDialog.DialogCode.Accepted)

    def reopen_from_editor(self) -> None:
        editor = self.editor_dialog
        if editor.task_id is not None and self._set_done(editor.task_id, False):
            editor.done(QDialog.DialogCode.Accepted)

    def delete_from_editor(self) -> None:
        editor = self.editor_dialog
        task = self.records.get(editor.task_id) if editor.task_id is not None else None
        if task is None or self.store is None:
            return
        if not self.confirm("删除待办", f"确定要删除“{task.title}”吗？\n删除后无法恢复。", "删除", parent=editor):
            return
        try:
            self.store.delete(task.id)
        except TaskError as error:
            editor.show_error(self._message(error))
            return
        if self._selected == task.id:
            self._selected = None
        editor.done(QDialog.DialogCode.Rejected)
        self._after_write(f"已删除待办“{task.title}”。")

    def sync_complete(self) -> None:
        """Keep `complete` enabled only while the editor shows an open task."""
        editor = self.editor_dialog
        task = self.records.get(editor.task_id) if editor.isVisible() and editor.task_id is not None else None
        if editor.isVisible() and editor.task_id is not None:
            if task is None:
                editor.show_error("这条待办已不存在。")
            editor.sync_actions(task)
        self.complete.setEnabled(self.store is not None and task is not None and task.status == "open")
        editor.reopen.setEnabled(self.store is not None and task is not None)

    def editor_closed(self) -> None:
        self.sync_complete()
        self._later(self._focus_selected)

    # ---- internals --------------------------------------------------------------------------------------------

    def _later(self, callback) -> None:
        QTimer.singleShot(0, lambda: isValid(self) and callback())

    def _message(self, error: TaskError) -> str:
        text = str(error)
        return "无法读取或更新本机待办，请确认数据库权限。" if text.startswith("本机待办数据库") else text

    def _say(self, text: str, *, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setToolTip("")
        self.status.setProperty("error", error)
        self._database_error = False
        repolish(self.status)

    def _fail(self, error: TaskError) -> None:
        text = str(error)
        self._say(self._message(error), error=True)
        if text.startswith("本机待办数据库"):
            self.status.setToolTip(text)
            self._database_error = True

    def _after_write(self, message: str) -> None:
        if self._reload():
            self._say(message)
        self.changed.emit()

    def _default_category(self) -> str | None:
        shown = [key for key in self._column_keys() if key not in self._hidden]
        return shown[0] if len(shown) == 1 else None

    def _key_of(self, task: Task) -> Key:
        if not task.category:
            return None
        return self._canon.get(task.category.casefold(), task.category)

    def _column_keys(self) -> list[Key]:
        keys: list[Key] = list(self.categories)
        if not keys or any(not task.category for task in self.records.values()):
            keys.append(None)
        return keys

    def _reload(self, *, rebuild: bool = True) -> bool:
        if self.store is None:
            self.records, self.categories, self._canon = {}, [], {}
            if rebuild:
                self._rebuild()
            return False
        try:
            tasks = self.store.list_tasks()
            names = self.store.categories()
        except TaskError as error:
            self._load_failed = True
            self._fail(error)
            if rebuild:
                self._rebuild()
            return False
        self._load_failed = False
        if self._database_error:
            self._say("")
        self.records = {task.id: task for task in tasks}
        categories = list(names)
        known = {name.casefold() for name in categories}
        for task in tasks:
            if task.category and task.category.casefold() not in known:
                categories.append(task.category)
                known.add(task.category.casefold())
        self.categories = categories
        self._canon = {name.casefold(): name for name in categories}
        if self._selected not in self.records:
            self._selected = None
        if rebuild:
            self._rebuild()
        self.sync_complete()
        return True

    def _matches(self, task: Task) -> bool:
        needle = self.search.text().strip().casefold()
        return not needle or needle in task.title.casefold()

    def _reveal(self, task_id: int) -> None:
        """Clear whichever filter hides the task: search text, completion mode, hidden column, collapsed section."""
        task = self.records[task_id]
        if not self._matches(task):
            with QSignalBlocker(self.search):
                self.search.clear()
        if self.mode != "all" and self.mode != ("complete" if task.status == "complete" else "open"):
            self.mode = "all"
            with QSignalBlocker(self.mode_group):
                self.mode_buttons["all"].setChecked(True)
        key = self._key_of(task)
        self._hidden.discard(key)
        if task.status == "complete":
            self._expanded[key] = True

    def _set_selected(self, task_id: int | None) -> None:
        self._selected = task_id
        for card_id, card in self._cards.items():
            card.set_selected(card_id == task_id)

    def _expanded_for(self, key: Key) -> bool:
        return self._expanded.get(key, self.mode == "all")

    def _open_counts(self) -> dict[Key, int]:
        counts: dict[Key, int] = {}
        for task in self.records.values():
            if task.status == "open":
                counts[self._key_of(task)] = counts.get(self._key_of(task), 0) + 1
        return counts

    def _capture(self) -> dict:
        focus = QApplication.focusWidget()
        focused = None
        if focus is not None and self.isAncestorOf(focus):
            for key, column in self.columns.items():
                if column.add_edit is focus:
                    focused = ("add", key, None)
                elif column.done_toggle is focus:
                    focused = ("toggle", key, None)
                else:
                    for task_id, card in column.cards.items():
                        if focus is card:
                            focused = ("card", key, task_id)
                        elif focus is card.check:
                            focused = ("check", key, task_id)
            for key, row in self.category_rows.items():
                if focus is row.check:
                    focused = ("row", key, None)
                elif focus is row.menu_button:
                    focused = ("rowmenu", key, None)
        return {
            "horizontal": self.board.horizontalScrollBar().value(),
            "vertical": {key: column.scroll.verticalScrollBar().value() for key, column in self.columns.items()},
            "side": self.side_scroll.verticalScrollBar().value(),
            "focus": focused,
        }

    def _restore(self, state: dict) -> None:
        def scrolls() -> None:
            self.board.horizontalScrollBar().setValue(state["horizontal"])
            self.side_scroll.verticalScrollBar().setValue(state["side"])
            for key, value in state["vertical"].items():
                if key in self.columns:
                    self.columns[key].scroll.verticalScrollBar().setValue(value)

        scrolls()
        self._later(scrolls)
        if state["focus"] is None:
            return
        kind, key, task_id = state["focus"]
        column = self.columns.get(key)
        row = self.category_rows.get(key)
        target: QWidget | None = None
        if kind == "add" and column is not None and self._adding:
            target = column.add_edit
        elif kind == "toggle" and column is not None:
            target = column.done_toggle
        elif kind in ("card", "check") and task_id in self._cards:
            target = self._cards[task_id] if kind == "card" else self._cards[task_id].check
        elif kind in ("card", "check") and column is not None:
            target = next(iter(column.cards.values()), None) or (
                column.add_button if column.add_button.isVisibleTo(column) else column.menu_button)
        elif kind == "row" and row is not None:
            target = row.check
        elif kind == "rowmenu" and row is not None:
            target = row.menu_button
        if target is not None:
            target.setFocus()

    def _focus_selected(self) -> None:
        card = self._cards.get(self._selected) if self._selected is not None else None
        if card is not None and card.isVisible():
            card.setFocus()

    def _rebuild(self, *, sidebar: bool = True) -> None:
        if self._rebuilding:
            return
        self._rebuilding = True
        state = self._capture()
        self.setUpdatesEnabled(False)
        try:
            keys = self._column_keys()
            self._hidden &= set(keys)
            if not set(keys) - self._hidden:
                self._hidden.clear()
            self._sync_sidebar(keys, force=sidebar)
            self._rebuild_board(keys)
            self._apply_tab_order()
        finally:
            self.setUpdatesEnabled(True)
            self._rebuilding = False
        self._restore(state)

    def _sync_sidebar(self, keys: list[Key], *, force: bool) -> None:
        unavailable = self.store is None
        for widget in (self.new_button, self.search, self.new_category, self.all_button, *self.mode_buttons.values()):
            widget.setEnabled(not unavailable)
        self.sidebar.setVisible(not unavailable)
        if list(self.category_rows) != keys:
            for row in self.category_rows.values():
                discard(row)
            self.category_rows.clear()
            for key in keys:
                menu = self._category_menu(key) if key is not None else None
                row = CategoryRow(key or UNCATEGORIZED, menu)
                row.check.toggled.connect(partial(self._column_toggled, key))
                self.side_layout.insertWidget(self.side_layout.count() - 1, row)
                self.category_rows[key] = row
        counts = self._open_counts()
        for key, row in self.category_rows.items():
            row.set_count(counts.get(key, 0))
            with QSignalBlocker(row.check):
                row.check.setChecked(key not in self._hidden)
        with QSignalBlocker(self.all_button):
            self.all_button.setChecked(not self._hidden)

    def _category_menu(self, name: str) -> QMenu:
        menu = QMenu(self)
        menu.addAction("重命名").triggered.connect(lambda: self.rename_category(name))
        menu.addAction("删除类别").triggered.connect(lambda: self.delete_category(name))
        return menu

    def _column_toggled(self, key: Key, checked: bool) -> None:
        if checked:
            self._hidden.discard(key)
        else:
            if not set(self.category_rows) - self._hidden - {key}:
                with QSignalBlocker(self.category_rows[key].check):
                    self.category_rows[key].check.setChecked(True)
                self._say("至少需要显示一个类别。")
                return
            self._hidden.add(key)
        with QSignalBlocker(self.all_button):
            self.all_button.setChecked(not self._hidden)
        self._rebuild(sidebar=False)

    def _plan(self, key: Key) -> tuple[list[Task], list[Task], int]:
        """Open tasks, completed tasks (both filtered by search) and the unfiltered open count of one column."""
        mine = [task for task in self.records.values() if self._key_of(task) == key]
        matching = [task for task in mine if self._matches(task)]
        opened = sorted((t for t in matching if t.status == "open"), key=lambda t: (deadline_order(t), t.id))
        done = sorted((t for t in matching if t.status == "complete"),
                      key=lambda t: (t.completed_at or "", t.id), reverse=True)
        return opened, done, sum(1 for task in mine if task.status == "open")

    def _empty_text(self, plans: dict[Key, tuple[list[Task], list[Task], int]]) -> str | None:
        if self.store is None:
            return self.error or "无法打开本机待办数据库。"
        if self._load_failed and not self.records:
            return self.status.text() or "无法读取待办。"
        if not self.records and not self.categories:
            return EMPTY_TEXT
        shown = sum(len(o) if self.mode == "open" else len(d) if self.mode == "complete" else len(o) + len(d)
                    for o, d, _ in plans.values())
        if shown == 0 and (self.search.text().strip() or self.mode == "complete"):
            label = {"open": "未完成", "complete": "已完成", "all": ""}[self.mode]
            return f"没有符合条件的{label}待办。"
        return None

    def _rebuild_board(self, keys: list[Key]) -> None:
        self.board.setUpdatesEnabled(False)
        for column in self.columns.values():
            discard(column)
        self.columns.clear()
        self._cards.clear()
        shown_keys = [key for key in keys if key not in self._hidden] if self.store is not None else []
        plans = {key: self._plan(key) for key in shown_keys}
        message = self._empty_text(plans)
        if message is not None:
            self.empty.setText(message)
            self.main_stack.setCurrentWidget(self.empty)
            self.board.setUpdatesEnabled(True)
            return
        self.main_stack.setCurrentWidget(self.board)
        today = self.today()
        searching = bool(self.search.text().strip())
        for key in shown_keys:
            opened, done, open_total = plans[key]
            expanded = self._expanded_for(key)
            collapsible = self.mode != "complete"
            count = f"已完成 {len(done)}" if self.mode == "complete" else str(open_total)
            column = TaskColumn(key, count, self._category_menu(key) if key is not None else None,
                                can_add=self.mode != "complete")
            column.submitted.connect(self._inline_submitted)
            column.started.connect(self._inline_started)
            column.typed.connect(self._inline_typed)
            column.cancelled.connect(self._inline_cancelled)
            column.expansion.connect(self._expansion_changed)
            open_cards = [self._card(task, today) for task in (opened if self.mode != "complete" else [])]
            show_done = self.mode == "complete" or expanded
            done_cards = [self._card(task, today) for task in done] if show_done else []
            hint = ""
            if self.mode == "complete":
                hint = "" if done else ("没有匹配的待办" if searching else "没有已完成的待办")
            elif not opened:
                hint = "没有匹配的待办" if searching else "没有未完成的待办"
            column.fill(open_cards, done_cards, done_total=len(done), expanded=expanded,
                        collapsible=collapsible, hint=hint)
            if self._adding and self._adding[0] == key and self.mode != "complete":
                column.set_adding(True, self._adding[1])
            self.board_layout.insertWidget(self.board_layout.count() - 1, column)
            self.columns[key] = column
        self.board.setUpdatesEnabled(True)
        self._fit_columns()

    def _card(self, task: Task, today: date) -> TaskCard:
        card = TaskCard(task, today)
        card.set_selected(task.id == self._selected)
        card.opened.connect(self._card_opened)
        card.toggled.connect(self.toggle_task)
        self._cards[task.id] = card
        return card

    def _card_opened(self, task_id: int) -> None:
        self.open_editor(task_id)

    def _inline_submitted(self, key: Key, text: str) -> None:
        self.add_inline(key, text)

    def _inline_started(self, key: Key) -> None:
        self._adding = (key, "")
        for other, column in self.columns.items():
            if other != key and not column.add_edit.isHidden():
                column.set_adding(False)

    def _inline_typed(self, key: Key, text: str) -> None:
        self._adding = (key, text)

    def _inline_cancelled(self, key: Key) -> None:
        if self._rebuilding or self._adding is None or self._adding[0] != key:
            return
        self._adding = None
        column = self.columns.get(key)
        if column is not None:
            column.set_adding(False, focus=column.add_edit.hasFocus())

    def _expansion_changed(self, key: Key, expanded: bool) -> None:
        self._expanded[key] = expanded
        self._rebuild(sidebar=False)

    def _fit_columns(self) -> None:
        count = len(self.columns)
        if not count:
            return
        spacing = self.board_layout.spacing()
        width = (self.board.viewport().width() - spacing * (count - 1)) // count
        width = max(COLUMN_MIN, min(COLUMN_MAX, width))
        for column in self.columns.values():
            column.setFixedWidth(width)

    def _apply_tab_order(self) -> None:
        chain: list[QWidget] = [*self.mode_buttons.values(), self.search, self.new_button, self.all_button]
        for row in self.category_rows.values():
            chain.append(row.check)
            if row.menu_button is not None:
                chain.append(row.menu_button)
        chain.append(self.new_category)
        for column in self.columns.values():
            chain.extend(column.order)
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def _scroll_to(self, task_id: int) -> None:
        def scroll() -> None:
            card = self._cards.get(task_id)
            if card is None:
                return
            column = self.columns.get(self._key_of(self.records[task_id])) if task_id in self.records else None
            if column is not None:
                self.board.ensureWidgetVisible(column, 0, 0)
                column.scroll.ensureWidgetVisible(card, 0, 12)

        scroll()
        self._later(scroll)

    def _scroll_to_column(self, key: Key) -> None:
        column = self.columns.get(key)
        if column is not None:
            self.board.ensureWidgetVisible(column, 0, 0)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.board.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:
        self.editor_dialog.reject()
        super().closeEvent(event)
