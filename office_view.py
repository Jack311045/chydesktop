from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter, QWidget

WIDTH, HEIGHT = 1080, 590
BLUE = "#1c5382"


class SceneButton(QPushButton):
    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        p = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text, option.icon = "", QIcon()
        p.drawControl(QStyle.ControlElement.CE_PushButton, option)
        if not self.text():
            return
        lines = self.text().splitlines()
        icon = self.icon()
        size = self.iconSize()
        tile = self.property("tile")
        text_left = 6
        line_height = p.fontMetrics().height()
        text_top = (self.height() - len(lines) * line_height) // 2
        if not icon.isNull():
            if tile:
                top = (self.height() - size.height() - line_height - 3) // 2
                icon.paint(p, (self.width() - size.width()) // 2, top, size.width(), size.height())
                text_top = top + size.height() + 3
            else:
                left = 8
                if self.property("navigation"):
                    left = max(8, (self.width() - size.width() - 6 - p.fontMetrics().horizontalAdvance(lines[0])) // 2)
                icon.paint(p, left, (self.height() - size.height()) // 2, size.width(), size.height())
                text_left = left + size.width() + 6
        p.setPen(QColor("#ffffff" if self.property("tone") == "blue" else "#173754"))
        alignment = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if text_left > 6
                                                       else Qt.AlignmentFlag.AlignHCenter)
        available = max(0, self.width() - text_left - 6)
        for i, line in enumerate(lines):
            shown = p.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, available)
            p.drawText(text_left, text_top + i * line_height, available, line_height, alignment, shown)


def office_icon(kind: str, color: str = BLUE) -> QIcon:
    image = QPixmap(32, 32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    if kind == "office":
        p.drawPolyline(QPolygonF([QPointF(*point) for point in [(4, 14), (16, 5), (28, 14), (25, 14),
                                                               (25, 27), (7, 27), (7, 14)]]))
        p.drawRect(13, 19, 6, 8)
    elif kind in ("today", "calendar"):
        p.drawRoundedRect(QRectF(5, 8, 22, 19), 3, 3)
        p.drawLine(5, 14, 27, 14)
        p.drawLine(11, 5, 11, 11)
        p.drawLine(21, 5, 21, 11)
        p.drawLine(11, 20, 21, 20)
    elif kind == "chat":
        p.drawRoundedRect(QRectF(4, 6, 24, 17), 5, 5)
        p.drawPolyline(QPolygonF([QPointF(10, 23), QPointF(10, 28), QPointF(16, 23)]))
        p.drawLine(10, 13, 22, 13)
        p.drawLine(10, 17, 18, 17)
    elif kind in ("mail", "follow"):
        p.drawRoundedRect(QRectF(4, 8, 24, 17), 3, 3)
        p.drawPolyline(QPolygonF([QPointF(5, 10), QPointF(16, 18), QPointF(27, 10)]))
    else:
        p.drawRoundedRect(QRectF(6, 5, 20, 23), 3, 3)
        for y in (12, 18, 23):
            p.drawLine(11, y, 14, y)
            p.drawLine(18, y, 22, y)
    p.end()
    return QIcon(image)


class OfficeView(QWidget):
    opened = Signal(str, QWidget)

    def __init__(self, paint_portrait: Callable[[QPainter, QRectF], None]) -> None:
        super().__init__()
        self.paint_portrait = paint_portrait
        self.buttons: dict[str, QPushButton] = {}
        self.regions: dict[str, QRectF] = {}
        self.setAccessibleName("事務所")
        self.setMinimumSize(680, 400)
        self.setStyleSheet("""
            QPushButton { background: #f4fafc; color: #173754; border: 1px solid #a4bdce;
                border-radius: 10px; padding: 3px 7px; font-weight: 600; }
            QPushButton:hover { background: #e0f3fc; border-color: #247ab2; }
            QPushButton:focus { border: 2px solid #126ba3; }
            QPushButton:pressed { background: #c7e6f7; }
            QPushButton[hotspot="true"] { background: transparent; border: 1px solid transparent; }
            QPushButton[hotspot="true"]:hover, QPushButton[hotspot="true"]:focus {
                background: rgba(220, 246, 255, 44); border: 2px solid #427eab; }
            QPushButton[tone="blue"] { background: #1b507c; border-color: #8dbbd8; color: white; }
            QPushButton[tone="blue"]:hover, QPushButton[tone="blue"]:focus { background: #286a9d; }
        """)
        for key, label, route, bounds, kind, tone in (
            ("clock", "千早桌面助手", "calendar", (20, 12, 304, 47), "calendar", "blue"),
            ("calendar_status", "行事曆", "calendar", (334, 12, 150, 47), "", ""),
            ("mail_status", "Gmail", "mail", (494, 12, 145, 47), "", ""),
            ("today_status", "今日狀態", "brief", (803, 12, 257, 47), "tasks", "blue"),
            ("board", "牆上白板", "calendar", (248, 91, 193, 175), "", "hit"),
            ("board_hint", "查看行程", "calendar", (268, 267, 126, 34), "calendar", ""),
            ("character", "與千早對話", "chat", (453, 166, 227, 231), "", "hit"),
            ("chat_hint", "和千早說話", "chat", (552, 126, 142, 36), "chat", ""),
            ("monitor", "工作站螢幕", "mail", (824, 285, 148, 92), "", "hit"),
            ("mail_hint", "打開信箱", "mail", (848, 425, 132, 37), "mail", ""),
            ("event", "下一個行程", "event", (782, 78, 278, 79), "calendar", ""),
            ("task", "空檔可安排", "task", (782, 165, 278, 79), "tasks", ""),
            ("opening", "下一段空檔", "brief", (20, 450, 319, 116), "calendar", "blue"),
        ):
            self.add_button(key, label, route, bounds, kind, tone)
        shortcuts = (
            ("brief", "今日簡報", "brief", "tasks"), ("due", "今日到期", "due", "today"),
            ("unread", "未讀郵件", "mail", "mail"), ("follow", "待回覆", "follow", "follow"),
            ("resume", "繼續對話", "chat", "chat"), ("schedule", "完整行程", "calendar", "calendar"),
        )
        for i, (key, label, route, kind) in enumerate(shortcuts):
            self.add_button(key, label, route, (356 + 117 * i, 491, 106, 75), kind, "", tile=True)

    def add_button(self, key: str, label: str, route: str, bounds: tuple[int, int, int, int],
                   kind: str, tone: str, *, tile: bool = False) -> None:
        button = SceneButton("" if tone == "hit" else label, self)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setProperty("hotspot", tone == "hit")
        button.setProperty("tone", tone)
        button.setProperty("tile", tile)
        if kind:
            button.setIcon(office_icon(kind, "#eff9ff" if tone == "blue" else BLUE))
            button.setIconSize(QSize(20, 20))
        button.clicked.connect(lambda: self.opened.emit(route, button))
        self.buttons[key] = button
        self.regions[key] = QRectF(*bounds)

    def set_copy(self, key: str, text: str) -> None:
        button = self.buttons[key]
        if button.text() != text:
            button.setText(text)
            button.setAccessibleName(text.replace("\n", " — "))
            button.setToolTip(text.replace("\n", " — "))

    def scene_transform(self) -> tuple[float, float, float]:
        scale = min(self.width() / WIDTH, self.height() / HEIGHT)
        return scale, (self.width() - WIDTH * scale) / 2, (self.height() - HEIGHT * scale) / 2

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        scale, x, y = self.scene_transform()
        for key, bounds in self.regions.items():
            button = self.buttons[key]
            font = button.font()
            font.setPixelSize(max(10, min(18, round(15 * scale))))
            button.setFont(font)
            button.setIconSize(QSize(max(12, round(22 * scale)), max(12, round(22 * scale))))
            button.setGeometry(QRectF(x + bounds.x() * scale, y + bounds.y() * scale,
                                      bounds.width() * scale, bounds.height() * scale).toAlignedRect())

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#274760"))
        scale, x, y = self.scene_transform()
        p.translate(x, y)
        p.scale(scale, scale)
        self.draw_room(p)
        p.save()
        p.setClipRect(QRectF(0, 0, WIDTH, 460))
        self.paint_portrait(p, QRectF(418, 122, 310, 414))
        p.restore()
        self.draw_front_desk(p)
        p.end()

    @staticmethod
    def draw_room(p: QPainter) -> None:
        def rect(x, y, w, h, color, radius=0):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(x, y, w, h), radius, radius)

        def line(x, y, xx, yy, color, width=1):
            p.setPen(QPen(QColor(color), width))
            p.drawLine(QPointF(x, y), QPointF(xx, yy))

        def polygon(points, color):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawPolygon(QPolygonF([QPointF(*point) for point in points]))

        wall = QLinearGradient(0, 0, WIDTH, 360)
        wall.setColorAt(0, QColor("#a9c5d4"))
        wall.setColorAt(0.42, QColor("#d3e2e6"))
        wall.setColorAt(1, QColor("#79a2b6"))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), wall)
        polygon([(0, 0), (232, 62), (232, 347), (0, 442)], "#a1bfce")
        rect(224, 58, 12, 305, "#7394a6")
        rect(231, 339, 849, 12, "#66899d")
        polygon([(0, 422), (233, 349), (1080, 349), (1080, 590), (0, 590)], "#6f899a")
        for y in (369, 399, 440, 499, 571):
            line(0, y, 1080, y, "#809aa8")
        for x in range(-350, 1550, 143):
            line(576 + (x - 576) * .36, 350, x, 590, "#5c798b")
        rect(454, 72, 311, 252, "#5b8199", 8)
        sky = QLinearGradient(452, 80, 740, 321)
        sky.setColorAt(0, QColor("#b4e3ef"))
        sky.setColorAt(1, QColor("#eef7eb"))
        p.fillRect(QRectF(463, 80, 293, 235), sky)
        for x, y, w, h in ((474, 281, 30, 34), (508, 269, 20, 46), (654, 275, 45, 40), (719, 252, 27, 63)):
            rect(x, y, w, h, "#9abdc6")
        for x in (557, 659):
            rect(x, 80, 6, 235, "#769dab")
        for y in range(91, 251, 17):
            rect(463, y, 293, 2, "#eff8f5")
        rect(448, 70, 324, 12, "#dceaf0", 3)
        rect(447, 313, 326, 9, "#ecf1ea", 3)

        rect(244, 91, 203, 174, "#69889a", 5)
        rect(250, 96, 191, 161, "#f4f5ed", 3)
        p.setPen(QColor("#3b617a"))
        font = p.font()
        font.setPixelSize(14)
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRectF(264, 97, 163, 34), Qt.AlignmentFlag.AlignCenter, "PRODUCER'S BOARD")
        for y in range(136, 240, 20):
            line(263, y, 428, y, "#c1d2d7")
        line(315, 132, 315, 242, "#c1d2d7")
        for x, y, color in ((275, 144, "#70a0be"), (333, 166, "#e4aa7a"), (369, 206, "#83b4af")):
            rect(x, y, 33, 7, color, 3)
        rect(245, 255, 203, 8, "#7b98a7", 2)
        rect(384, 253, 27, 4, "#267aa2", 2)
        rect(25, 100, 65, 104, "#6c8fa3", 3)
        rect(30, 105, 55, 95, "#f4e7c5", 2)
        rect(30, 105, 55, 19, "#789db0", 2)
        for y in range(137, 190, 10):
            line(36, y, 79, y, "#c5aa86")
        rect(97, 236, 121, 130, "#8a857b", 4)
        rect(103, 243, 110, 117, "#d5c5a4", 2)
        for y in (275, 318, 360):
            rect(101, y, 114, 6, "#9a846c")
        for x, w, h, color in ((112, 17, 25, "#5c8ba9"), (132, 18, 28, "#93a69b"),
                               (153, 20, 22, "#e8d3aa"), (177, 15, 26, "#a0756e")):
            rect(x, 275 - h, w, h, color, 2)
        for y in (286, 296, 306):
            rect(110, y, 66, 6, "#f3eee2", 1)
        rect(115, 327, 45, 27, "#a6b8aa", 2)
        rect(169, 324, 34, 32, "#c39e78", 2)
        rect(192, 220, 23, 19, "#7d9ca5", 2)
        for i in range(4):
            line(195 + i * 5, 221, 192 + i * 6, 208, ("#244a69", "#bd886b")[i % 2], 2)

        # The chair is behind the work surface; its feet remain on the floor.
        rect(730, 323, 76, 74, "#476f88", 19)
        rect(739, 331, 57, 49, "#7199ac", 15)
        line(768, 387, 768, 484, "#496776", 8)
        for x in (740, 795):
            line(768, 474, x, 487, "#496776", 5)
        rect(766, 398, 279, 24, "#738895", 5)
        rect(776, 419, 12, 125, "#596f7e", 2)
        rect(1017, 416, 12, 125, "#596f7e", 2)
        rect(970, 424, 42, 88, "#a4afab", 3)
        for y in (451, 479):
            line(974, y, 1008, y, "#778f98")
        polygon([(765, 399), (1047, 399), (1007, 370), (796, 370)], "#d1c4ad")
        rect(820, 281, 156, 93, "#304b5d", 7)
        rect(826, 287, 144, 80, "#42667c", 5)
        rect(838, 298, 120, 55, "#a7cdd6", 4)
        rect(848, 309, 39, 34, "#dfedf0", 3)
        for y in (313, 323, 333):
            rect(900, y, 45, 4, "#dbeaf0", 2)
        rect(886, 374, 21, 9, "#304b5d", 2)
        rect(870, 381, 53, 4, "#304b5d", 2)
        polygon([(862, 385), (921, 385), (928, 394), (854, 394)], "#7d939a")
        for y in (387, 390, 393):
            for x in range(864, 920, 7):
                line(x, y, x + 4, y, "#5b7681")
        rect(939, 385, 13, 9, "#587984", 4)
        polygon([(799, 383), (812, 358), (825, 383)], "#9a8065")
        rect(795, 358, 19, 21, "#f1ebd9", 2)
        line(796, 363, 812, 363, "#bd8269", 3)
        rect(1008, 377, 12, 13, "#8d9d8b", 3)

    @staticmethod
    def draw_front_desk(p: QPainter) -> None:
        def shape(points, color):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawPolygon(QPolygonF([QPointF(*point) for point in points]))

        shape([(280, 405), (682, 405), (728, 460), (227, 460)], "#ddd2b9")
        shape([(227, 460), (728, 460), (728, 474), (227, 474)], "#a5967f")
        shape([(246, 474), (270, 474), (264, 580), (250, 580)], "#51697a")
        shape([(681, 474), (704, 474), (709, 583), (695, 583)], "#51697a")
        shape([(309, 427), (406, 427), (424, 448), (321, 448)], "#8da8ac")
        shape([(313, 417), (390, 415), (415, 441), (325, 445)], "#f9f3e2")
        for y in (423, 427, 431, 435):
            p.setPen(QPen(QColor("#9ba9aa"), 0.8))
            p.drawLine(QPointF(334, y), QPointF(392, y - 2))
        shape([(485, 434), (520, 434), (528, 444), (490, 444)], "#597f99")
        shape([(491, 432), (521, 432), (523, 436), (490, 436)], "#a7c7d4")
        shape([(610, 430), (663, 430), (676, 447), (620, 447)], "#b58f76")
        shape([(612, 425), (653, 424), (661, 435), (617, 436)], "#f0e8cf")
