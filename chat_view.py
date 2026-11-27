"""Messenger-style transcript for the Chihaya conversation panel."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPalette, QPen,
    QTextCharFormat, QTextCursor, QTextDocument, QTextOption,
)
from PySide6.QtWidgets import QAbstractSlider, QLabel, QMenu, QScrollArea, QWidget

from desktop_style import COLORS

USER = "user"
CHIHAYA = "chihaya"
SPEAKERS = {USER: "你", CHIHAYA: "千早"}

_TRANSCRIPT_BG = "#0a121d"
_RADIUS = 14
_PAD_X, _PAD_Y = 13, 9
_EDGE = 12
_TOP, _BOTTOM = 12, 14
_MAX_WIDTH_RATIO = 0.78
_MIN_TEXT_WIDTH = 24
_GAP_SAME_SPEAKER, _GAP_CHIHAYA_REPLY, _GAP_USER_TURN, _GAP_NAME = 4, 10, 18, 3
_FOLLOW_SLACK = 60

_STYLE = f"""
QScrollArea#chatView {{ background: {_TRANSCRIPT_BG}; border: 2px solid transparent; }}
QScrollArea#chatView:focus {{ border: 2px solid {COLORS["ACCENT"]}; }}
QScrollArea#chatView > QWidget {{ background: {_TRANSCRIPT_BG}; }}
QWidget#chatContent {{ background: transparent; }}
QLabel#chatName {{ background: transparent; color: {COLORS["MUTED"]}; }}
"""


class _Bubble(QLabel):
    """One message. Text is laid out by a QTextDocument because QLabel never breaks a long URL."""

    selection_started = Signal(object)
    drag_moved = Signal(QPoint)

    def __init__(self, role: str, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.role = role
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._doc = QTextDocument(self)
        self._doc.setUndoRedoEnabled(False)
        self._doc.setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._doc.setDefaultTextOption(option)
        self._natural: int | None = None
        self._fitted: tuple[int, QSize] | None = None
        self._anchor = self._cursor = 0
        self._dragging = False
        self.set_message(text)

    def set_message(self, text: str) -> None:
        self._text = text
        super().setText(text)
        self._doc.setPlainText(text)
        self._natural = self._fitted = None
        self._anchor = self._cursor = 0
        self.setAccessibleName(f"{SPEAKERS[self.role]}：{text}")
        self.update()

    def document(self) -> QTextDocument:
        return self._doc

    def fit(self, max_width: int) -> QSize:
        """Lay the text out inside a bubble at most max_width wide and return the bubble's size."""
        font = self.font()
        if font != self._doc.defaultFont():
            self._doc.setDefaultFont(font)
            self._natural = self._fitted = None
        if self._fitted is not None and self._fitted[0] == max_width:
            return self._fitted[1]
        if self._natural is None:
            self._doc.setTextWidth(-1)
            self._natural = math.ceil(self._doc.idealWidth()) + 1
        width = min(self._natural, max(_MIN_TEXT_WIDTH, max_width - 2 * _PAD_X))
        self._doc.setTextWidth(width)
        if width < self._natural:
            height = self._doc.size().height()
            tight = math.ceil(self._doc.idealWidth()) + 1
            if tight < width:
                self._doc.setTextWidth(tight)
                if self._doc.size().height() <= height:
                    width = tight
                else:
                    self._doc.setTextWidth(width)
        size = QSize(width + 2 * _PAD_X, math.ceil(self._doc.size().height()) + 2 * _PAD_Y)
        self._fitted = (max_width, size)
        return size

    def selected_text(self) -> str:
        start, end = sorted((self._anchor, self._cursor))
        cursor = QTextCursor(self._doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        return cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n")

    def has_selection(self) -> bool:
        return self._anchor != self._cursor

    def clear_selection(self) -> None:
        if self.has_selection():
            self._anchor = self._cursor = 0
            self.update()

    def select_all(self) -> None:
        self._anchor, self._cursor = 0, max(0, self._doc.characterCount() - 1)
        self.selection_started.emit(self)
        self.update()

    def copy_selection(self) -> None:
        if self.has_selection():
            QGuiApplication.clipboard().setText(self.selected_text())

    def _hit(self, point: QPointF) -> int:
        position = self._doc.documentLayout().hitTest(
            QPointF(point.x() - _PAD_X, point.y() - _PAD_Y), Qt.HitTestAccuracy.FuzzyHit)
        return self._cursor if position < 0 else position

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self._anchor = self._cursor = self._hit(event.position())
        self._dragging = True
        self.selection_started.emit(self)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._cursor = self._hit(event.position())
            self.update()
            self.drag_moved.emit(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        cursor = QTextCursor(self._doc)
        cursor.setPosition(self._hit(event.position()))
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        self._anchor, self._cursor = cursor.selectionStart(), cursor.selectionEnd()
        self.selection_started.emit(self)
        self.update()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
        elif event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        copy = menu.addAction("复制")
        copy.setEnabled(self.has_selection())
        copy.triggered.connect(self.copy_selection)
        menu.addAction("全选").triggered.connect(self.select_all)
        menu.exec(event.globalPos())

    def paintEvent(self, event) -> None:
        user = self.role == USER
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(COLORS["ACCENT_DARK"] if user else COLORS["CARD"]))
        painter.setPen(Qt.PenStyle.NoPen if user else QPen(QColor(COLORS["BORDER"]), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), _RADIUS, _RADIUS)
        painter.translate(_PAD_X, _PAD_Y)
        context = QAbstractTextDocumentLayout.PaintContext()
        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff" if user else COLORS["TEXT"]))
        context.palette = palette
        if self.has_selection():
            start, end = sorted((self._anchor, self._cursor))
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor = QTextCursor(self._doc)
            selection.cursor.setPosition(start)
            selection.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.format = QTextCharFormat()
            selection.format.setBackground(QColor("#bfe0fb" if user else COLORS["ACCENT_DARK"]))
            selection.format.setForeground(QColor("#0a121d" if user else "#ffffff"))
            context.selections = [selection]
        self._doc.documentLayout().draw(painter, context)


@dataclass
class _Entry:
    role: str
    text: str
    bubble: _Bubble
    name: QLabel | None = None
    bottom: int = 0


class ChatView(QScrollArea):
    """Scrollable transcript: the producer's messages on the right, Chihaya's on the left."""

    message_added = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatView")
        self.setAccessibleName("对话记录")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(_STYLE)
        self._content = QWidget()
        self._content.setObjectName("chatContent")
        self.setWidget(self._content)
        self._entries: list[_Entry] = []
        self._laid_width = -1
        self._follow = True
        self._selected: _Bubble | None = None
        bar = self.verticalScrollBar()
        bar.setSingleStep(24)
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_value_changed)

    def sizeHint(self) -> QSize:
        return QSize(420, 240)

    def messages(self) -> list[tuple[str, str]]:
        return [(entry.role, entry.text) for entry in self._entries]

    def toPlainText(self) -> str:
        lines: list[str] = []
        for index, entry in enumerate(self._entries):
            if entry.role == USER and index:
                lines.append("")
            lines.append(f"{SPEAKERS[entry.role]}：{entry.text}")
        return "\n".join(lines)

    def add_message(self, role: str, text: str) -> None:
        self._append(role, text)
        self._relayout(len(self._entries) - 1)
        self.message_added.emit(role, text)

    def set_messages(self, messages: Sequence[tuple[str, str]]) -> None:
        self.clear()
        self.setUpdatesEnabled(False)
        try:
            for role, text in messages:
                self._append(role, text)
            self._relayout()
        finally:
            self.setUpdatesEnabled(True)

    def clear(self) -> None:
        for entry in self._entries:
            for widget in (entry.bubble, entry.name):
                if widget is not None:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
        self._entries.clear()
        self._selected = None
        self._follow = True
        self._content.resize(max(self.viewport().width(), 0), 0)

    def replace_text(self, old: str, new: str) -> None:
        if not old:
            return
        first: int | None = None
        for index, entry in enumerate(self._entries):
            if old in entry.text:
                entry.text = entry.text.replace(old, new)
                entry.bubble.set_message(entry.text)
                first = index if first is None else first
        if first is not None:
            self._relayout(first)

    def copy_transcript(self) -> None:
        QGuiApplication.clipboard().setText(self.toPlainText())

    def viewportEvent(self, event: QEvent) -> bool:
        handled = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize:
            self._relayout_if_resized()
        return handled

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_if_resized()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and self._entries:
            for entry in self._entries:
                if entry.name is not None:
                    entry.name.setFont(self._name_font())
            self._laid_width = -1
            self._relayout()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            if self._selected is not None and self._selected.has_selection():
                self._selected.copy_selection()
            else:
                self.copy_transcript()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Home, Qt.Key.Key_End):
            action = (QAbstractSlider.SliderAction.SliderToMinimum if event.key() == Qt.Key.Key_Home
                      else QAbstractSlider.SliderAction.SliderToMaximum)
            self.verticalScrollBar().triggerAction(action)
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("复制全部对话").triggered.connect(self.copy_transcript)
        menu.exec(event.globalPos())

    def _append(self, role: str, text: str) -> None:
        if role not in SPEAKERS:
            raise ValueError(f"Unknown chat role: {role!r}")
        bubble = _Bubble(role, text, self._content)
        bubble.selection_started.connect(self._claim_selection)
        bubble.drag_moved.connect(self._scroll_while_dragging)
        name = None
        if role == CHIHAYA and not (self._entries and self._entries[-1].role == CHIHAYA):
            name = QLabel(SPEAKERS[CHIHAYA], self._content)
            name.setObjectName("chatName")
            name.setFont(self._name_font())
            name.show()
        bubble.show()
        self._entries.append(_Entry(role, text, bubble, name))

    def _name_font(self) -> QFont:
        font = QFont(self.font())
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.9)
        return font

    def _gap_before(self, index: int) -> int:
        entry = self._entries[index]
        if index == 0:
            return 0
        if entry.role == USER:
            return _GAP_USER_TURN
        return _GAP_SAME_SPEAKER if self._entries[index - 1].role == CHIHAYA else _GAP_CHIHAYA_REPLY

    def _relayout_if_resized(self) -> None:
        if self.viewport().width() != self._laid_width:
            self._relayout()

    def _relayout(self, start: int = 0) -> None:
        width = self.viewport().width()
        if width != self._laid_width:
            start = 0
        self._laid_width = width
        max_bubble = max(int(width * _MAX_WIDTH_RATIO), 2 * _PAD_X + _MIN_TEXT_WIDTH)
        y = self._entries[start - 1].bottom if start > 0 else _TOP
        for index in range(start, len(self._entries)):
            entry = self._entries[index]
            y += self._gap_before(index)
            if entry.name is not None:
                hint = entry.name.sizeHint()
                entry.name.setGeometry(_EDGE + _PAD_X // 2, y, hint.width(), hint.height())
                y += hint.height() + _GAP_NAME
            size = entry.bubble.fit(max_bubble)
            x = width - _EDGE - size.width() if entry.role == USER else _EDGE
            entry.bubble.setGeometry(x, y, size.width(), size.height())
            y += size.height()
            entry.bottom = y
        total = self._entries[-1].bottom + _BOTTOM if self._entries else 0
        self._content.resize(width, total)

    def _claim_selection(self, bubble: _Bubble) -> None:
        if self._selected is not None and self._selected is not bubble:
            self._selected.clear_selection()
        self._selected = bubble

    def _scroll_while_dragging(self, global_pos: QPoint) -> None:
        y = self.viewport().mapFromGlobal(global_pos).y()
        overshoot = y if y < 0 else max(0, y - self.viewport().height())
        if overshoot:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + overshoot)

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self.verticalScrollBar().setValue(maximum)

    def _on_value_changed(self, value: int) -> None:
        self._follow = self.verticalScrollBar().maximum() - value <= _FOLLOW_SLACK
