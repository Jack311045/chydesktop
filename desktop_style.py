from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {
    "BG": "#0f1722", "PANEL": "#162231", "CARD": "#1b2b3d", "BORDER": "#35506e", "TEXT": "#e8f0f8",
    "MUTED": "#a4b8cc", "ACCENT": "#5aa9e6", "ACCENT_DARK": "#2a6fb0", "INPUT": "#0b131e",
}

_STYLE = """
QMainWindow, QDialog, QMessageBox, QInputDialog, QFileDialog { background: @BG; }
QWidget { background: @BG; color: @TEXT; }
QLabel, QCheckBox, QRadioButton, QToolButton { background: transparent; }
QToolTip { background: @CARD; color: @TEXT; border: 1px solid @ACCENT; padding: 4px; }
QGroupBox { background: @PANEL; border: 1px solid @BORDER; border-radius: 10px;
            margin-top: 16px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 14px;
                   padding: 0 7px; color: #c4e4ff; font-weight: 700; background: transparent; }
QGroupBox#conversation { background: #0a121d; border: 3px solid @ACCENT; border-radius: 14px; }
QGroupBox#conversation::title { color: #ffffff; background: #22578a; border-radius: 8px;
                                padding: 3px 12px; left: 18px; font-size: 16px; font-weight: 800; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateEdit, QSpinBox {
    background: @INPUT; color: @TEXT; border: 1px solid @BORDER; border-radius: 8px; padding: 5px 8px;
    selection-background-color: @ACCENT_DARK; selection-color: #ffffff; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus {
    border: 2px solid @ACCENT; padding: 4px 7px; }
QLineEdit:read-only { color: @MUTED; }
QComboBox QAbstractItemView, QCalendarWidget QAbstractItemView {
    background: @PANEL; color: @TEXT; selection-background-color: @ACCENT_DARK; selection-color: #ffffff;
    border: 1px solid @BORDER; outline: 0; }
QListWidget, QListView, QTreeView, QTableView {
    background: @INPUT; color: @TEXT; border: 1px solid @BORDER; border-radius: 9px; outline: 0; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #22344a; }
QListWidget::item:hover { background: #1f3550; }
QListWidget::item:selected { background: @ACCENT_DARK; color: #ffffff; }
QListView::indicator, QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #6d8cae; background: @INPUT; }
QListView::indicator:checked, QCheckBox::indicator:checked {
    border-color: @ACCENT; background: qradialgradient(cx:0.5, cy:0.5, radius:0.55, fx:0.5, fy:0.5,
    stop:0 #ffffff, stop:0.45 #ffffff, stop:0.5 @ACCENT, stop:1 @ACCENT); }
QHeaderView::section { background: @CARD; color: @TEXT; border: 0; padding: 5px; }
QPushButton { background: #223750; color: @TEXT; border: 1px solid @BORDER; border-radius: 9px; padding: 6px 13px; }
QPushButton:hover { background: #2c4968; border-color: #5b83ad; }
QPushButton:pressed { background: #1a2d44; }
QPushButton:focus { border: 2px solid @ACCENT; padding: 5px 12px; }
QPushButton:checked { background: @ACCENT_DARK; color: #ffffff; border-color: #8cc3f0; }
QPushButton:disabled { color: #70869b; background: #152234; border-color: #24364b; }
QPushButton[primary="true"] { background: @ACCENT_DARK; color: #ffffff; border-color: #8cc3f0; font-weight: 700; }
QPushButton[primary="true"]:hover { background: #347fc5; }
QScrollArea { border: 0; background: @BG; }
QScrollBar:vertical { background: transparent; width: 13px; margin: 0; }
QScrollBar::handle:vertical { background: #44627f; border-radius: 5px; min-height: 36px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #6490bb; }
QScrollBar:horizontal { background: transparent; height: 13px; margin: 0; }
QScrollBar::handle:horizontal { background: #44627f; border-radius: 5px; min-width: 36px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #6490bb; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { background: @PANEL; color: @TEXT; border: 1px solid @BORDER; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 6px; }
QMenu::item:selected { background: @ACCENT_DARK; color: #ffffff; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: @CARD; }
QCalendarWidget QToolButton { color: @TEXT; background: transparent; padding: 3px 7px; }
QCalendarWidget QToolButton:hover { background: #2c4968; border-radius: 6px; }
QCalendarWidget QMenu { background: @PANEL; color: @TEXT; }
QCalendarWidget QSpinBox { background: @INPUT; color: @TEXT; }
QWidget#chatComposer { background: #122033; border: 2px solid #35506e; border-radius: 11px; }
QFrame#eventDetail { background: @CARD; border: 2px solid @ACCENT; border-radius: 10px; }
"""


def _themed(css: str) -> str:
    for name in sorted(COLORS, key=len, reverse=True):
        css = css.replace(f"@{name}", COLORS[name])
    return css


PAGE_STYLE = _themed(_STYLE)


def apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, COLORS["BG"]), (QPalette.ColorRole.WindowText, COLORS["TEXT"]),
        (QPalette.ColorRole.Base, COLORS["INPUT"]), (QPalette.ColorRole.AlternateBase, COLORS["PANEL"]),
        (QPalette.ColorRole.Text, COLORS["TEXT"]), (QPalette.ColorRole.Button, COLORS["CARD"]),
        (QPalette.ColorRole.ButtonText, COLORS["TEXT"]), (QPalette.ColorRole.Highlight, COLORS["ACCENT_DARK"]),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, COLORS["CARD"]),
        (QPalette.ColorRole.ToolTipText, COLORS["TEXT"]), (QPalette.ColorRole.PlaceholderText, "#7f96ad"),
        (QPalette.ColorRole.Link, COLORS["ACCENT"]),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#70869b"))
    app.setPalette(palette)
    app.setStyleSheet(PAGE_STYLE)


def install_desktop_font(app: QApplication) -> str:
    """One family for every page: Noto Sans SC Medium covers Latin and Chinese with the same stroke weight.

    Microsoft YaHei UI and Segoe UI are fallbacks on machines without it; bundled Huninn is the last resort.
    """
    asset = Path(__file__).with_name("assets") / "fonts" / "Huninn-Regular.ttf"
    font_id = QFontDatabase.addApplicationFont(str(asset))
    available = QFontDatabase.families()
    families = [name for name in ("Noto Sans SC", "Microsoft YaHei UI", "Segoe UI") if name in available]
    if font_id >= 0:
        families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if families:
        font = QFont()
        font.setFamilies(families)
        font.setPointSizeF(9.5)
        font.setWeight(QFont.Weight.Medium)
        app.setFont(font)
    return families[0] if families else app.font().family()


def tame_list(*lists) -> None:
    """Wrap long rows instead of showing a horizontal scrollbar, and scroll smoothly per pixel."""
    for widget in lists:
        widget.setWordWrap(True)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        widget.setVerticalScrollMode(widget.ScrollMode.ScrollPerPixel)
        widget.setTextElideMode(Qt.TextElideMode.ElideNone)


def install_desktop_theme(app: QApplication) -> str:
    apply_dark_palette(app)
    return install_desktop_font(app)
