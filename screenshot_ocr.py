"""Local screenshot review for a PySide6 composer.

Replace the composer with ``ScreenshotOcrLineEdit(ocr=OcrConfig(...),
stage_task=stage_task_title, placeholderText="…")`` and connect a visible
"Open screenshot" action to ``entry.choose_image`` if desired. Ctrl+V accepts
clipboard images; ordinary text paste remains native Qt behavior.

The optional ``stage_task(title)`` callback runs once after the user edits and
explicitly stages one title. In Chihaya, use it to set a ``PendingAction("add",
title)`` and display the existing confirmation prompt (handle any already
pending action first). Do not call ``send()`` or commit from the callback.
"Insert reviewed text" only edits the composer, joining lines with spaces.

Provide a trusted local Tesseract executable (default: ``tesseract`` on PATH)
and install the desired traineddata separately. Set ``language`` to e.g.
``"eng+chi_tra"`` and optionally ``tessdata_dir`` to a local tessdata directory.
Images are piped to the executable as PNG, with no temp files, network requests
or application data access. The timeout applies to the OCR subprocess.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QProcess, Qt, QTimer
from PySide6.QtGui import (
    QContextMenuEvent, QImage, QImageReader, QImageWriter, QKeyEvent,
    QKeySequence, QPixmap, QTransform,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

__all__ = ["OcrConfig", "ScreenshotOcrLineEdit"]

_MAX_PIXELS = 20_000_000
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_TEXT_BYTES = 256 * 1024
_FORMATS = {b"png", b"jpeg", b"bmp", b"webp"}


@dataclass(frozen=True)
class OcrConfig:
    executable: str = "tesseract"
    language: str = "eng"
    tessdata_dir: str | None = None
    timeout_ms: int = 15_000

    def __post_init__(self) -> None:
        if not 0 < self.timeout_ms <= 2_147_483_647:
            raise ValueError("OCR timeout must be a positive Qt timer interval.")

    def _command(self) -> tuple[str, list[str]]:
        args = ["stdin", "stdout", "-l", self.language]
        if self.tessdata_dir is not None:
            args.extend(["--tessdata-dir", self.tessdata_dir])
        return self.executable, args


def _read_image(path: Path) -> QImage:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("请选择本机的截图文件。")
    try:
        path = path.resolve(strict=True)
        if str(path).startswith(("\\\\", "//")) or not path.is_file():
            raise ValueError("请选择本机的截图文件。")
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("截图文件太大（上限 32 MiB）。")
    except (OSError, RuntimeError) as error:
        raise ValueError("无法打开截图文件。") from error

    reader = QImageReader(str(path))
    reader.setDecideFormatFromContent(True)
    if bytes(reader.format()) not in _FORMATS:
        raise ValueError("请选择可读取的 PNG、JPEG、BMP 或 WebP 截图。")
    size = reader.size()
    if not size.isValid() or size.isEmpty():
        raise ValueError("无法读取截图尺寸。")
    if size.width() * size.height() > _MAX_PIXELS:
        raise ValueError("截图太大（上限 2000 万像素）。")
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError("无法解码截图。")
    return image


class _ReviewDialog(QDialog):
    def __init__(
        self, image: QImage, ocr: OcrConfig, can_stage: bool, parent: QLineEdit,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("校对截图文字")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._ocr = ocr
        self._original = image
        self._rotation = 0
        self._input = QBuffer(self)
        self._output = bytearray()
        self._error = ""
        self._closing: int | None = None
        self.result_action: tuple[str, str] | None = None
        # QLineEdit can clear its selection when review takes focus.
        self._entry_text = parent.text()
        self._entry_start = (parent.selectionStart() if parent.hasSelectedText()
                             else parent.cursorPosition())
        self._entry_length = len(parent.selectedText().encode("utf-16-le")) // 2

        self._process = QProcess(self)
        self._process.started.connect(self._write_input)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.readyReadStandardError.connect(self._process.readAllStandardError)
        self._process.errorOccurred.connect(self._process_error)
        self._process.finished.connect(self._finished)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._stop("本机识别超时。"))

        self.preview = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName("截图预览")
        self.editor = QPlainTextEdit(placeholderText="校对识别结果，或手动输入文字。")
        self.editor.setAccessibleName("校对后的截图文字")
        self.editor.textChanged.connect(self._update_actions)
        self.status = QLabel(wordWrap=True)
        self._run = QPushButton("重新识别", clicked=self.run_ocr)
        self._rotate = QPushButton("向右旋转", clicked=self._rotate_image)
        self._reset = QPushButton("还原原图", clicked=self._reset_image)
        self._insert = QPushButton("带入对话", clicked=self._insert_text)
        self._insert.setProperty("primary", True)
        self._stage = QPushButton("编辑待办草稿…", clicked=self._edit_task)
        self._stage.setVisible(can_stage)
        cancel = QPushButton("取消", clicked=self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.preview)
        row = QHBoxLayout()
        for button in (self._rotate, self._reset, self._run):
            button.setAutoDefault(False)
            row.addWidget(button)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(self.editor)
        row = QHBoxLayout()
        for button in (self._insert, self._stage, cancel):
            button.setAutoDefault(False)
            row.addWidget(button)
        layout.addLayout(row)
        self._update_preview()
        self._update_actions()

    def _image(self) -> QImage:
        return (self._original.transformed(QTransform().rotate(self._rotation))
                if self._rotation else self._original)

    def _update_preview(self) -> None:
        image = self._image().scaled(
            540, 300, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(QPixmap.fromImage(image))
        self._reset.setEnabled(self._rotation != 0)

    def _rotate_image(self) -> None:
        self._rotation = (self._rotation + 90) % 360
        self._image_changed()

    def _reset_image(self) -> None:
        self._rotation = 0
        self._image_changed()

    def _image_changed(self) -> None:
        self._update_preview()
        self.status.setText("预览已更改。请重新识别，或手动编辑文字。")

    def _update_actions(self) -> None:
        running = self._process.state() != QProcess.ProcessState.NotRunning
        has_text = bool(self.editor.toPlainText().strip())
        self.editor.setReadOnly(running)
        self._run.setEnabled(not running)
        self._rotate.setEnabled(not running)
        self._reset.setEnabled(not running and self._rotation != 0)
        self._insert.setEnabled(not running and has_text)
        self._stage.setEnabled(not running and has_text)

    def run_ocr(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return
        self._input.setData(b"")
        self._input.open(QIODevice.OpenModeFlag.WriteOnly)
        writer = QImageWriter(self._input, b"png")
        writer.setCompression(0)
        encoded = writer.write(self._image())
        self._input.close()
        if not encoded:
            self.status.setText("无法准备截图用于本机识别。")
            return
        self._output.clear()
        self._error = ""
        self.status.setText("正在本机识别…")
        program, args = self._ocr._command()
        self._timer.start(self._ocr.timeout_ms)
        self._process.start(program, args)
        self._update_actions()

    def _write_input(self) -> None:
        if not self._error:
            self._process.write(self._input.data())
            self._process.closeWriteChannel()
        self._input.setData(b"")

    def _read_output(self) -> None:
        chunk = self._process.readAllStandardOutput()
        if self._error:
            return
        if len(self._output) + chunk.size() > _MAX_TEXT_BYTES:
            self._stop("本机识别返回的文字过多。")
        else:
            self._output.extend(bytes(chunk))

    def _stop(self, reason: str) -> None:
        self._error = reason
        self._process.kill()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._error = "无法启动 Tesseract，请检查本机程序路径（安装方法见 README）。"
            self._finished(-1, QProcess.ExitStatus.CrashExit)
        elif error == QProcess.ProcessError.WriteError and not self._error:
            self._stop("无法把截图交给本机识别程序。")

    def _finished(self, code: int, exit_status: QProcess.ExitStatus) -> None:
        self._timer.stop()
        self._input.setData(b"")
        if self._closing is not None:
            super().done(self._closing)
            return
        self._read_output()
        if self._error:
            self.status.setText(self._error)
        elif code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            self.status.setText("本机识别失败。请检查截图和已安装的语言数据。")
        else:
            text = self._output.decode("utf-8", errors="replace").strip()
            if text:
                self.editor.setPlainText(text)
            self.status.setText("带入前请对照原图；日期、时段和截止日若不清楚，请先核对。" if text
                                else "没有识别出文字，可以手动输入。")
        self._output.clear()
        self._update_actions()

    def done(self, result: int) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._closing = result
            self._timer.stop()
            self._stop("已取消本机识别。")
        else:
            super().done(result)

    def _insert_text(self) -> None:
        text = " ".join(line.strip() for line in self.editor.toPlainText().splitlines()
                        if line.strip())
        entry = self.parentWidget()
        if not text or not isinstance(entry, QLineEdit) or entry.isReadOnly():
            return
        if entry.text() != self._entry_text:
            self.status.setText("输入框内容已变化，请重新打开校对窗口后再带入。")
            return
        size = len((entry.text() + text).encode("utf-16-le")) // 2
        if size - self._entry_length > entry.maxLength():
            self.status.setText("校对后的文字太长，输入框放不下，请先缩短。")
            return
        self.result_action = ("insert", text)
        self.accept()

    def insert_into(self, entry: QLineEdit, text: str) -> None:
        if not entry.isReadOnly() and entry.text() == self._entry_text:
            entry.setSelection(self._entry_start, self._entry_length)
            entry.insert(text)

    def _edit_task(self) -> None:
        lines = self.editor.toPlainText().strip().splitlines()
        if not lines:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle("编辑待办草稿")
        dialog.setLabelText("一条待办的名称（回到对话后仍需确认）：")
        dialog.setTextValue(lines[0].strip())
        dialog.setOkButtonText("送去确认")
        dialog.setCancelButtonText("取消")
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                title = dialog.textValue().strip()
                if title:
                    self.result_action = ("task", title)
                    self.accept()
        finally:
            dialog.deleteLater()


class ScreenshotOcrLineEdit(QLineEdit):
    """Image-aware composer. ``review_image`` and ``choose_image`` open review."""

    def __init__(
        self, parent: QWidget | None = None, *, ocr: OcrConfig | None = None,
        stage_task: Callable[[str], None] | None = None, **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._ocr = ocr or OcrConfig()
        self._stage_task = stage_task
        self._review: _ReviewDialog | None = None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Paste) and self._paste_image():
            event.accept()
        else:
            super().keyPressEvent(event)

    def paste(self) -> None:
        if not self._paste_image():
            super().paste()

    def _paste_image(self) -> bool:
        if self.isReadOnly():
            return False
        clipboard = QApplication.clipboard()
        data = clipboard.mimeData()
        if data is None or not data.hasImage():
            return False
        image = clipboard.image()
        if image.isNull():
            return False
        self.review_image(image)
        return True

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        menu = self.createStandardContextMenu()
        if not self.isReadOnly():
            menu.addSeparator()
            menu.addAction("选择截图…", self.choose_image)
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()

    def choose_image(self) -> None:
        if self.isReadOnly() or self._review is not None:
            return
        # The native Windows dialog follows the system theme, so it stays readable with the dark app theme.
        url, _ = QFileDialog.getOpenFileUrl(
            self, "选择截图", filter="截图 (*.png *.jpg *.jpeg *.bmp *.webp)", supportedSchemes=["file"],
        )
        if url.isEmpty():
            return
        try:
            if not url.isLocalFile() or url.host():
                raise ValueError("请选择本机的截图文件。")
            image = _read_image(Path(url.toLocalFile()))
        except ValueError as error:
            QMessageBox.warning(self, "截图识别", str(error))
            return
        self.review_image(image)

    def review_image(self, image: QImage) -> None:
        """Review one QImage asynchronously, leaving it unmodified."""
        if self.isReadOnly() or self._review is not None:
            return
        if image.isNull() or image.width() * image.height() > _MAX_PIXELS:
            QMessageBox.warning(
                self, "截图识别", "请选择不超过 2000 万像素的非空图片。",
            )
            return
        dialog = _ReviewDialog(QImage(image), self._ocr, self._stage_task is not None, self)
        self._review = dialog
        dialog.finished.connect(self._review_finished)
        dialog.open()
        dialog.run_ocr()

    def _review_finished(self, result: int) -> None:
        dialog, self._review = self._review, None
        if (result != QDialog.DialogCode.Accepted or dialog is None
                or dialog.result_action is None):
            return
        action, value = dialog.result_action
        if action == "insert":
            dialog.insert_into(self, value)
        elif self._stage_task is not None:
            self._stage_task(value)
