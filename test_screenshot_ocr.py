import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from time import monotonic
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QTimer, Qt, QUrl
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from screenshot_ocr import OcrConfig, ScreenshotOcrLineEdit


_RECOGNIZE = (
    "import sys; image = sys.stdin.buffer.read(); "
    "assert image.startswith(b'\\x89PNG\\r\\n\\x1a\\n'); "
    "sys.stdout.buffer.write('First line\\n\\u7b2c\\u4e8c\\u884c\\n'.encode('utf-8'))"
)


def _wait_for(predicate, timeout_ms):
    deadline = monotonic() + timeout_ms / 1000
    while not predicate() and monotonic() < deadline:
        QTest.qWait(10)
    return predicate()


class ScreenshotOcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.staged = []
        self.entry = ScreenshotOcrLineEdit(
            stage_task=self.staged.append, ocr=OcrConfig(timeout_ms=3000),
        )
        self.entry.show()
        self.sent = QSignalSpy(self.entry.returnPressed)
        self.image = QImage(32, 16, QImage.Format.Format_RGB32)
        self.image.fill(Qt.GlobalColor.white)

    def tearDown(self):
        if self.entry._review is not None:
            self.entry._review.reject()
            _wait_for(lambda: self.entry._review is None, 3000)
        self.entry.close()
        self.entry.deleteLater()
        QTest.qWait(1)

    def run_with(self, start, script=_RECOGNIZE):
        with patch.object(OcrConfig, "_command", return_value=(sys.executable, ["-c", script])):
            start()
        review = self.entry._review
        self.assertIsNotNone(review)
        return review

    def wait_for_ocr(self, review, status="带入前请对照原图"):
        self.assertTrue(_wait_for(lambda: status in review.status.text(), 4000),
                        review.status.text())

    def paste_key(self):
        QTest.keyClick(self.entry, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    def test_native_text_paste_and_no_url_paste(self):
        clipboard = QApplication.clipboard()
        self.addCleanup(clipboard.clear)
        clipboard.setText("replacement")
        self.entry.setText("draft")
        self.entry.selectAll()
        self.paste_key()
        self.assertEqual(self.entry.text(), "replacement")
        self.assertIsNone(self.entry._review)
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.invalid/screenshot.png")])
        clipboard.setMimeData(mime)
        self.paste_key()
        self.assertIsNone(self.entry._review)

    def test_clipboard_image_requires_explicit_reviewed_insert(self):
        clipboard = QApplication.clipboard()
        self.addCleanup(clipboard.clear)
        clipboard.setImage(self.image)
        self.entry.setText("before replace after")
        self.entry.setSelection(7, 7)
        review = self.run_with(self.paste_key)
        self.wait_for_ocr(review)
        self.assertEqual(review.editor.toPlainText(), "First line\n第二行")
        self.assertEqual(self.entry.text(), "before replace after")
        self.assertEqual(self.staged, [])
        self.assertGreater(review.preview.pixmap().width(), review.preview.pixmap().height())
        review._rotate.click()
        self.assertLess(review.preview.pixmap().width(), review.preview.pixmap().height())
        review._reset.click()
        self.assertGreater(review.preview.pixmap().width(), review.preview.pixmap().height())
        review.editor.setPlainText("reviewed\n \n第二行")
        review._insert.click()
        self.assertEqual(self.entry.text(), "before reviewed 第二行 after")
        self.assertEqual(self.sent.count(), 0)
        self.assertEqual(self.staged, [])

    def test_local_file_and_only_explicit_edited_task_is_staged(self):
        with NamedTemporaryFile(dir=Path.cwd(), suffix=".png", delete=False) as file:
            path = Path(file.name)
        self.addCleanup(path.unlink)
        self.assertTrue(self.image.save(str(path)))
        self.entry.setText("existing draft")
        with patch("screenshot_ocr.QFileDialog.getOpenFileUrl",
                   return_value=(QUrl.fromLocalFile(str(path)), "")):
            review = self.run_with(self.entry.choose_image)
        self.wait_for_ocr(review)
        self.assertEqual(self.staged, [])

        def finish_task_editor(accept):
            task = review.findChild(QInputDialog)
            if task is not None:
                fields.append(task.textValue())
                task.setTextValue("  One edited task  ")
                task.accept() if accept else task.reject()

        fields = []
        QTimer.singleShot(0, lambda: finish_task_editor(False))
        review._stage.click()
        self.assertEqual(self.staged, [])
        QTest.qWait(1)
        QTimer.singleShot(0, lambda: finish_task_editor(True))
        review._stage.click()
        self.assertEqual(fields, ["First line", "First line"])
        self.assertEqual(self.staged, ["One edited task"])
        self.assertEqual(self.entry.text(), "existing draft")
        self.assertEqual(self.sent.count(), 0)

    def test_file_chooser_rejects_remote_or_non_image_without_starting_ocr(self):
        with NamedTemporaryFile(dir=Path.cwd(), suffix=".png", delete=False) as file:
            file.write(b"not an image")
            path = Path(file.name)
        self.addCleanup(path.unlink)
        for url in (QUrl("https://example.invalid/image.png"), QUrl.fromLocalFile(str(path))):
            with self.subTest(url=url), patch(
                "screenshot_ocr.QFileDialog.getOpenFileUrl", return_value=(url, "")
            ), patch("screenshot_ocr.QMessageBox.warning") as warn:
                self.entry.choose_image()
                warn.assert_called_once()
                self.assertIsNone(self.entry._review)

    def test_timeout_unblocks_manual_review(self):
        self.entry._ocr = OcrConfig(timeout_ms=100)
        review = self.run_with(lambda: self.entry.review_image(self.image),
                               "import time; time.sleep(20)")
        self.wait_for_ocr(review, "超时")
        self.assertFalse(review.editor.isReadOnly())
        review.editor.setPlainText("manual text")
        review._insert.click()
        self.assertEqual(self.entry.text(), "manual text")
        self.assertEqual(self.sent.count(), 0)

    def test_missing_executable_preserves_current_draft(self):
        self.entry._ocr = OcrConfig(executable=str(Path.cwd() / "absent-tesseract.exe"))
        self.entry.setText("keep this")
        self.entry.review_image(self.image)
        review = self.entry._review
        self.assertIsNotNone(review)
        self.wait_for_ocr(review, "无法启动 Tesseract")
        self.assertFalse(review.editor.isReadOnly())
        review.reject()
        self.assertEqual(self.entry.text(), "keep this")
        self.assertEqual(self.staged, [])

    def test_failed_process_does_not_expose_partial_output(self):
        review = self.run_with(
            lambda: self.entry.review_image(self.image),
            "import sys; sys.stdin.buffer.read(); print('partial text'); sys.exit(1)",
        )
        self.wait_for_ocr(review, "本机识别失败")
        self.assertEqual(review.editor.toPlainText(), "")
        self.assertFalse(review.editor.isReadOnly())
        self.assertFalse(review._insert.isEnabled())
        self.assertFalse(review._stage.isEnabled())

    def test_cancel_during_ocr_discards_results(self):
        self.entry.setText("keep this")
        review = self.run_with(lambda: self.entry.review_image(self.image),
                               "import time; time.sleep(20)")
        review.reject()
        self.assertTrue(_wait_for(lambda: self.entry._review is None, 2000))
        self.assertEqual(self.entry.text(), "keep this")
        self.assertEqual(self.staged, [])
        self.assertEqual(self.sent.count(), 0)


if __name__ == "__main__":
    unittest.main()
