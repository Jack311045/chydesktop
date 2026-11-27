import time
import unittest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from chat_view import ChatView
from desktop_style import install_desktop_theme

LONG_ZH = "已经帮你把会议改到明天下午，并整理好了摘要：先完成接口评审，周五前交付测试报告，下周一开始联调。" * 3


class ChatViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.saved = (cls.app.styleSheet(), cls.app.palette(), cls.app.font(), cls.app.style().objectName())
        install_desktop_theme(cls.app)

    @classmethod
    def tearDownClass(cls):
        sheet, palette, font, style = cls.saved
        cls.app.setStyle(style)
        cls.app.setPalette(palette)
        cls.app.setFont(font)
        cls.app.setStyleSheet(sheet)

    def make_view(self, width=480, height=420, messages=(), show=True):
        view = ChatView()
        self.addCleanup(view.deleteLater)
        self.addCleanup(view.close)
        view.resize(width, height)
        if show:
            view.show()
        for role, text in messages:
            view.add_message(role, text)
        QTest.qWait(20)
        return view

    @staticmethod
    def bubbles(view):
        return [label for label in view.findChildren(QLabel) if label.accessibleName().startswith(("你：", "千早："))]

    @staticmethod
    def rect_in_viewport(view, widget):
        top_left = widget.mapTo(view.viewport(), QPoint(0, 0))
        return top_left.x(), top_left.x() + widget.width(), top_left.y()

    def test_plain_text_format_keeps_last_reply_readable(self):
        view = self.make_view()
        self.assertEqual(view.toPlainText(), "")
        view.add_message("chihaya", "A")
        self.assertEqual(view.toPlainText(), "千早：A")
        view.add_message("user", "B")
        view.add_message("chihaya", "C")
        self.assertEqual(view.toPlainText(), "千早：A\n\n你：B\n千早：C")
        self.assertEqual(view.toPlainText().rsplit("\n千早：", 1)[-1], "C")
        view.add_message("user", "再来一个")
        view.add_message("chihaya", "第一行\n第二行")
        self.assertEqual(view.toPlainText().rsplit("\n千早：", 1)[-1], "第一行\n第二行")
        self.assertEqual(view.toPlainText().count("\n\n你："), 2)

    def test_messages_replace_text_clear_and_signal(self):
        view = self.make_view()
        added = []
        view.message_added.connect(lambda role, text: added.append((role, text)))
        view.set_messages([("chihaya", "token abc"), ("user", "my abc is abc")])
        self.assertEqual(added, [])
        view.add_message("chihaya", "no secret")
        self.assertEqual(added, [("chihaya", "no secret")])
        view.replace_text("abc", "***")
        view.replace_text("", "x")
        self.assertEqual(view.messages(), [("chihaya", "token ***"), ("user", "my *** is ***"), ("chihaya", "no secret")])
        bubble = self.bubbles(view)[1]
        self.assertEqual(bubble.text(), "my *** is ***")
        self.assertEqual(bubble.accessibleName(), "你：my *** is ***")
        self.assertNotIn("abc", bubble.document().toPlainText())
        view.set_messages([("user", "only")])
        self.assertEqual(view.messages(), [("user", "only")])
        view.clear()
        QTest.qWait(20)
        self.assertEqual(view.messages(), [])
        self.assertEqual(view.toPlainText(), "")
        self.assertEqual(self.bubbles(view), [])
        self.assertEqual(view.verticalScrollBar().maximum(), 0)

    def test_unknown_role_is_rejected(self):
        view = self.make_view()
        with self.assertRaises(ValueError):
            view.add_message("bot", "hi")
        self.assertEqual(view.messages(), [])

    def test_speakers_are_aligned_to_opposite_sides(self):
        view = self.make_view(480, 400, [("chihaya", "早上好"), ("user", "你好"), ("chihaya", "OK"), ("user", LONG_ZH)])
        viewport_width = view.viewport().width()
        for label, role in zip(self.bubbles(view), ("chihaya", "user", "chihaya", "user")):
            left, right, _ = self.rect_in_viewport(view, label)
            if role == "user":
                self.assertTrue(0 <= viewport_width - right <= 24, (left, right, viewport_width))
            else:
                self.assertTrue(0 <= left <= 24, (left, right, viewport_width))
        names = [label for label in view.findChildren(QLabel) if label.text() == "千早" and not label.accessibleName()]
        self.assertEqual(len(names), 2)

    def test_bubbles_never_exceed_78_percent_of_viewport(self):
        for width in (360, 900):
            view = self.make_view(width, 500, [("chihaya", LONG_ZH), ("user", LONG_ZH), ("chihaya", "短")])
            viewport_width = view.viewport().width()
            widths = [label.width() for label in self.bubbles(view)]
            self.assertTrue(all(w <= viewport_width * 0.78 + 1 for w in widths), (width, widths, viewport_width))
            self.assertGreater(widths[0], viewport_width * 0.6)
            self.assertLess(widths[2], viewport_width * 0.3)

    def test_bubbles_rewrap_when_viewport_shrinks_and_grows(self):
        view = self.make_view(900, 600, [("chihaya", LONG_ZH), ("user", LONG_ZH)])
        wide = [label.height() for label in self.bubbles(view)]
        view.resize(380, 600)
        QTest.qWait(20)
        narrow = [label.height() for label in self.bubbles(view)]
        self.assertTrue(all(n > w for n, w in zip(narrow, wide)), (wide, narrow))
        self.assertTrue(all(label.width() <= view.viewport().width() * 0.78 + 1 for label in self.bubbles(view)))
        view.resize(900, 600)
        QTest.qWait(20)
        self.assertEqual([label.height() for label in self.bubbles(view)], wide)
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)

    def test_scrollbar_appearing_narrows_bubbles_without_horizontal_scroll(self):
        view = self.make_view(420, 260, [("chihaya", "短"), ("user", "你好")])
        self.assertEqual(view.verticalScrollBar().maximum(), 0)
        for _ in range(6):
            view.add_message("chihaya", LONG_ZH)
        QTest.qWait(20)
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        viewport_width = view.viewport().width()
        self.assertEqual(view.widget().width(), viewport_width)
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)
        for label in self.bubbles(view):
            self.assertLessEqual(label.width(), viewport_width * 0.78 + 1)
            self.assertLessEqual(label.geometry().right(), viewport_width)

    def test_long_unbroken_text_wraps_instead_of_scrolling_sideways(self):
        url = "https://example.com/" + "a" * 300
        view = self.make_view(360, 400, [("chihaya", url), ("user", "Z" * 200)])
        single_line = self.bubbles(self.make_view(360, 400, [("chihaya", "short")]))[0].height()
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)
        for label in self.bubbles(view):
            self.assertGreater(label.height(), single_line * 2)
            self.assertLessEqual(label.width(), view.viewport().width() * 0.78 + 1)
            self.assertLessEqual(label.document().size().width(), label.width())

    def test_markup_is_displayed_literally(self):
        view = self.make_view(480, 300, [("chihaya", "<b>x</b>"), ("user", '<img src="x"> &amp; <a href="y">z</a>')])
        first, second = self.bubbles(view)
        self.assertEqual(first.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(first.text(), "<b>x</b>")
        self.assertEqual(first.document().toPlainText(), "<b>x</b>")
        self.assertEqual(second.document().toPlainText(), '<img src="x"> &amp; <a href="y">z</a>')
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", first.document().toHtml())
        self.assertEqual(view.toPlainText(), '千早：<b>x</b>\n\n你：<img src="x"> &amp; <a href="y">z</a>')

    def test_newlines_are_preserved(self):
        view = self.make_view(480, 300, [("chihaya", "one"), ("chihaya", "one\ntwo\n\nfour")])
        single, multi = self.bubbles(view)
        self.assertEqual(multi.document().blockCount(), 4)
        self.assertGreater(multi.height(), single.height() * 2)

    def test_follows_new_messages_unless_reading_history(self):
        view = self.make_view(400, 200, [("chihaya", LONG_ZH) for _ in range(6)])
        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())
        view.add_message("user", "newest")
        QTest.qWait(20)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(self.rect_in_viewport(view, self.bubbles(view)[-1])[2] < view.viewport().height())

        bar.setValue(bar.maximum() - 30)
        view.add_message("chihaya", LONG_ZH)
        QTest.qWait(20)
        self.assertEqual(bar.value(), bar.maximum())

        bar.setValue(bar.maximum() - 200)
        reading = bar.value()
        view.add_message("chihaya", LONG_ZH)
        QTest.qWait(20)
        self.assertEqual(bar.value(), reading)
        bar.setValue(bar.maximum())
        view.add_message("user", "back at the bottom")
        QTest.qWait(20)
        self.assertEqual(bar.value(), bar.maximum())

    def test_stays_at_bottom_when_resized_and_when_shown_late(self):
        view = self.make_view(400, 200, [("chihaya", LONG_ZH) for _ in range(6)])
        bar = view.verticalScrollBar()
        for size in ((300, 200), (500, 320), (400, 200)):
            view.resize(*size)
            QTest.qWait(20)
            self.assertEqual(bar.value(), bar.maximum(), size)
        hidden = self.make_view(400, 200, [("chihaya", LONG_ZH) for _ in range(6)], show=False)
        hidden.show()
        QTest.qWait(20)
        self.assertGreater(hidden.verticalScrollBar().maximum(), 0)
        self.assertEqual(hidden.verticalScrollBar().value(), hidden.verticalScrollBar().maximum())

    def test_clear_resumes_following(self):
        view = self.make_view(400, 200, [("chihaya", LONG_ZH) for _ in range(6)])
        view.verticalScrollBar().setValue(0)
        view.clear()
        for _ in range(6):
            view.add_message("chihaya", LONG_ZH)
        QTest.qWait(20)
        self.assertEqual(view.verticalScrollBar().value(), view.verticalScrollBar().maximum())

    def test_keyboard_scrolling_and_accessibility(self):
        view = self.make_view(400, 200, [("chihaya", "第一条")] + [("user", LONG_ZH)] * 5 + [("chihaya", "最后一条")])
        self.assertEqual(view.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertEqual(view.accessibleName(), "对话记录")
        self.assertEqual([label.accessibleName() for label in self.bubbles(view)][:2], ["千早：第一条", f"你：{LONG_ZH}"])
        bar = view.verticalScrollBar()
        QTest.keyClick(view, Qt.Key.Key_Home)
        self.assertEqual(bar.value(), 0)
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(bar.value(), bar.singleStep())
        QTest.keyClick(view, Qt.Key.Key_PageDown)
        self.assertGreater(bar.value(), bar.singleStep())
        QTest.keyClick(view, Qt.Key.Key_PageUp)
        self.assertEqual(bar.value(), bar.singleStep())
        QTest.keyClick(view, Qt.Key.Key_End)
        self.assertEqual(bar.value(), bar.maximum())
        QTest.keyClick(view, Qt.Key.Key_Home)
        QTest.keyClick(self.bubbles(view)[0], Qt.Key.Key_PageDown)
        self.assertGreater(bar.value(), 0)

    def send_mouse(self, label, kind, point, buttons):
        event = QMouseEvent(kind, QPointF(point), QPointF(label.mapToGlobal(point)),
                            Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(label, event)

    def test_text_can_be_selected_with_the_mouse_and_copied(self):
        clipboard = QGuiApplication.clipboard()
        previous = clipboard.text()
        self.addCleanup(clipboard.setText, previous)
        view = self.make_view(480, 300, [("chihaya", "hello world\nsecond line"), ("user", "other bubble")])
        first, second = self.bubbles(view)
        y = 14
        self.send_mouse(first, QEvent.Type.MouseButtonPress, QPoint(14, y), Qt.MouseButton.LeftButton)
        self.send_mouse(first, QEvent.Type.MouseMove, QPoint(first.width() - 4, y), Qt.MouseButton.LeftButton)
        self.send_mouse(first, QEvent.Type.MouseButtonRelease, QPoint(first.width() - 4, y), Qt.MouseButton.NoButton)
        self.assertEqual(first.selected_text(), "hello world")
        QTest.keyClick(first, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(clipboard.text(), "hello world")
        QTest.keyClick(first, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(first, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(clipboard.text(), "hello world\nsecond line")
        self.send_mouse(second, QEvent.Type.MouseButtonDblClick, QPoint(second.width() - 20, second.height() // 2),
                        Qt.MouseButton.LeftButton)
        self.assertEqual(second.selected_text(), "bubble")
        self.assertFalse(first.has_selection())
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(clipboard.text(), "bubble")

    def test_dragging_a_selection_past_the_edge_scrolls(self):
        view = self.make_view(400, 200, [("chihaya", LONG_ZH) for _ in range(6)])
        bar = view.verticalScrollBar()
        bar.setValue(0)
        first = self.bubbles(view)[0]
        self.send_mouse(first, QEvent.Type.MouseButtonPress, QPoint(14, 14), Qt.MouseButton.LeftButton)
        self.send_mouse(first, QEvent.Type.MouseMove, QPoint(60, first.height() + 150), Qt.MouseButton.LeftButton)
        self.send_mouse(first, QEvent.Type.MouseButtonRelease, QPoint(60, first.height() + 150), Qt.MouseButton.NoButton)
        self.assertGreater(bar.value(), 0)

    def test_empty_view_uses_the_conversation_background(self):
        view = self.make_view(300, 200)
        pixel = view.grab().toImage().pixelColor(40, 40)
        self.assertEqual(pixel.name(), "#0a121d")

    def test_view_copies_whole_transcript_when_nothing_is_selected(self):
        clipboard = QGuiApplication.clipboard()
        previous = clipboard.text()
        self.addCleanup(clipboard.setText, previous)
        view = self.make_view(480, 300, [("chihaya", "A"), ("user", "B")])
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(clipboard.text(), "千早：A\n\n你：B")

    def test_hundreds_of_messages_stay_responsive(self):
        started = time.perf_counter()
        view = self.make_view(520, 500, [("user" if i % 2 else "chihaya", LONG_ZH if i % 3 == 0 else "好的") for i in range(320)])
        for width in (380, 800, 520):
            view.resize(width, 500)
            QTest.qWait(1)
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual(len(self.bubbles(view)), 320)
        self.assertEqual(view.verticalScrollBar().value(), view.verticalScrollBar().maximum())
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
