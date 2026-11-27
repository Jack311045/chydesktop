from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBitmap, QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QRadialGradient, QRegion, QTransform,
)
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter, QWidget

WIDTH, HEIGHT = 1080, 590
BLUE = "#d6ebff"
PORTRAIT_RECT = QRectF(418, 122, 310, 414)
VISIBLE_BOTTOM = 440


def silhouette_path(image: QImage) -> QPainterPath:
    """Outline of the opaque part of a portrait, in the image's own pixel coordinates."""
    if image.isNull():
        return QPainterPath()
    small = image.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation)
    region = QRegion(QBitmap.fromImage(small.createAlphaMask()))
    path = QPainterPath()
    path.addRegion(region)
    path = path.simplified()
    return QTransform.fromScale(image.width() / small.width(), image.height() / small.height()).map(path)


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
        p.setPen(QColor("#ffffff" if self.property("tone") in ("blue", "screen") else "#e8f2fb"))
        alignment = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if text_left > 6
                                                       else Qt.AlignmentFlag.AlignHCenter)
        available = max(0, self.width() - text_left - 6)
        for i, line in enumerate(lines):
            shown = p.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, available)
            p.drawText(text_left, text_top + i * line_height, available, line_height, alignment, shown)


class SilhouetteButton(SceneButton):
    """A hotspot shaped like the character; the scene draws its outline when it is hovered or focused."""
    state_changed = Signal()

    def active(self) -> bool:
        return self.underMouse() or self.hasFocus()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.state_changed.emit()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.state_changed.emit()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.state_changed.emit()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.state_changed.emit()

    def paintEvent(self, event) -> None:
        pass


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
    elif kind == "settings":
        for y, x in ((8, 12), (16, 21), (24, 15)):
            p.drawLine(4, y, 28, y)
            p.setBrush(QColor("#eef6fa"))
            p.drawEllipse(QPointF(x, y), 3, 3)
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
        self.composer: QWidget | None = None
        self.conversation: QWidget | None = None
        self.setAccessibleName("事务所")
        self.setMinimumSize(680, 400)
        self.silhouette = QPainterPath()
        self.setStyleSheet("""
            QPushButton { background: rgba(13, 25, 40, 226); color: #e8f2fb; border: 1px solid #3b5a7c;
                border-radius: 10px; padding: 3px 7px; }
            QPushButton:hover { background: rgba(32, 60, 93, 238); border-color: #72b6ec; }
            QPushButton:focus { border: 2px solid #8cc9f5; }
            QPushButton:pressed { background: #19324f; }
            QPushButton[hotspot="true"] { background: transparent; border: 1px solid transparent; }
            QPushButton[hotspot="true"]:hover, QPushButton[hotspot="true"]:focus {
                background: rgba(140, 200, 255, 28); border: 2px solid #72b6ec; }
            QPushButton[hotspot="shape"], QPushButton[hotspot="shape"]:hover, QPushButton[hotspot="shape"]:focus {
                background: transparent; border: none; }
            QPushButton[tone="blue"] { background: #1d4f7e; border-color: #7cb4e0; color: white; }
            QPushButton[tone="blue"]:hover, QPushButton[tone="blue"]:focus { background: #2a6aa3; }
        """)
        for key, label, route, bounds, kind, tone in (
            ("clock", "日期与时间", "calendar", (20, 12, 304, 47), "calendar", "blue"),
            ("calendar_status", "日历", "calendar", (334, 12, 150, 47), "", ""),
            ("mail_status", "Gmail", "mail", (494, 12, 145, 47), "", ""),
            ("today_status", "今日状态", "brief", (803, 12, 257, 47), "tasks", "blue"),
            ("board", "墙上白板", "calendar", (248, 91, 193, 175), "", "hit"),
            ("character", "与千早对话", "chat", (453, 166, 227, 231), "", "shape"),
            ("monitor", "信箱", "mail", (827, 288, 142, 78), "", "hit"),
            ("event", "下一个行程", "event", (782, 78, 278, 79), "calendar", ""),
            ("task", "空档可安排", "task", (782, 165, 278, 79), "tasks", ""),
            ("opening", "下一段空档", "brief", (20, 450, 319, 116), "calendar", "blue"),
        ):
            self.add_button(key, label, route, bounds, kind, tone)
        shortcuts = (
            ("brief", "今日简报", "brief", "tasks"), ("due", "今日到期", "due", "today"),
            ("unread", "未读邮件", "mail", "mail"), ("follow", "待回复", "follow", "follow"),
            ("resume", "继续对话", "chat", "chat"), ("schedule", "完整行程", "calendar", "calendar"),
        )
        for i, (key, label, route, kind) in enumerate(shortcuts):
            self.add_button(key, label, route, (356 + 117 * i, 491, 106, 75), kind, "", tile=True)
        self.buttons["character"].state_changed.connect(self.update)

    def add_button(self, key: str, label: str, route: str, bounds: tuple[int, int, int, int],
                   kind: str, tone: str, *, tile: bool = False) -> None:
        silent = tone in ("hit", "shape")
        button = (SilhouetteButton if tone == "shape" else SceneButton)("" if silent else label, self)
        button.setAccessibleName(label)
        if not silent:
            button.setToolTip(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setProperty("hotspot", "shape" if tone == "shape" else tone == "hit")
        button.setProperty("tone", tone)
        button.setProperty("tile", tile)
        if kind:
            button.setIcon(office_icon(kind, "#eff9ff" if tone in ("blue", "screen") else BLUE))
            button.setIconSize(QSize(20, 20))
        button.clicked.connect(lambda: self.opened.emit(route, button))
        self.buttons[key] = button
        self.regions[key] = QRectF(*bounds)

    def set_copy(self, key: str, text: str) -> None:
        button = self.buttons[key]
        spoken = text.replace("\n", " — ")
        if button.property("tone") in ("hit", "shape"):
            button.setAccessibleName(spoken)
            return
        if button.text() != text:
            button.setText(text)
            button.setAccessibleName(spoken)
            button.setToolTip(spoken)

    def set_silhouette(self, image: QImage) -> None:
        """Use the portrait's opaque outline as the character's hover outline and hit area."""
        path = silhouette_path(image)
        scene = QTransform()
        scene.translate(PORTRAIT_RECT.x(), PORTRAIT_RECT.y())
        scene.scale(PORTRAIT_RECT.width() / max(1, image.width()), PORTRAIT_RECT.height() / max(1, image.height()))
        visible = QPainterPath()
        visible.addRect(QRectF(0, 0, WIDTH, VISIBLE_BOTTOM))
        self.silhouette = scene.map(path).intersected(visible)
        self.position_controls()
        self.update()

    def scene_transform(self) -> tuple[float, float, float]:
        height = self.height() - (52 if self.composer else 0)
        scale = min(self.width() / WIDTH, height / HEIGHT)
        return scale, (self.width() - WIDTH * scale) / 2, (height - HEIGHT * scale) / 2

    def mount_chat(self, composer: QWidget, conversation: QWidget) -> None:
        self.composer, self.conversation = composer, conversation
        for widget in (composer, conversation):
            widget.setParent(self)
            widget.raise_()
        conversation.hide()
        composer.show()
        self.position_controls()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.position_controls()

    def position_controls(self) -> None:
        scale, x, y = self.scene_transform()
        for key, bounds in self.regions.items():
            button = self.buttons[key]
            font = button.font()
            font.setPixelSize(max(10, min(15, round(13 * scale))))
            button.setFont(font)
            button.setIconSize(QSize(max(12, round(20 * scale)), max(12, round(20 * scale))))
            geometry = QRectF(x + bounds.x() * scale, y + bounds.y() * scale,
                              bounds.width() * scale, bounds.height() * scale).toAlignedRect()
            if key == "character" and not self.silhouette.isEmpty():
                to_view = QTransform()
                to_view.translate(x, y)
                to_view.scale(scale, scale)
                shape = to_view.map(self.silhouette)
                geometry = shape.boundingRect().toAlignedRect().intersected(self.rect())
                mask = QBitmap(geometry.size())
                mask.fill(Qt.GlobalColor.color0)
                painter = QPainter(mask)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(Qt.GlobalColor.color1)
                painter.drawPath(shape.translated(-geometry.x(), -geometry.y()))
                painter.end()
                button.setGeometry(geometry)
                button.setMask(mask)
                continue
            button.setGeometry(geometry)
        if self.composer is not None and self.conversation is not None:
            self.composer.setGeometry(10, self.height() - 50, self.width() - 20, 46)
            self.conversation.setGeometry(QRectF(x + 20 * scale, y + 75 * scale,
                                                  423 * scale, 370 * scale).toAlignedRect())

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#0b1522"))
        scale, x, y = self.scene_transform()
        p.translate(x, y)
        p.scale(scale, scale)
        self.draw_room(p)
        p.save()
        p.setClipRect(QRectF(0, 0, WIDTH, 460))
        self.paint_portrait(p, PORTRAIT_RECT)
        p.restore()
        character = self.buttons["character"]
        if isinstance(character, SilhouetteButton) and character.active() and not self.silhouette.isEmpty():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(114, 182, 236, 70), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(self.silhouette)
            p.setPen(QPen(QColor("#a6dcff"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(self.silhouette)
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
        wall.setColorAt(0, QColor("#1a2b41"))
        wall.setColorAt(0.45, QColor("#22364f"))
        wall.setColorAt(1, QColor("#142234"))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), wall)
        polygon([(0, 0), (232, 62), (232, 347), (0, 442)], "#14233a")
        rect(224, 58, 12, 305, "#0e1a2b")
        rect(231, 339, 849, 12, "#0c1827")
        polygon([(0, 422), (233, 349), (1080, 349), (1080, 590), (0, 590)], "#192736")
        for y in (369, 399, 440, 499, 571):
            line(0, y, 1080, y, "#223444")
        for x in range(-350, 1550, 143):
            line(576 + (x - 576) * .36, 350, x, 590, "#1d2d3d")

        # Night window: sky, stars, moon and a few lit windows in the distant buildings.
        rect(454, 72, 311, 252, "#0c1626", 8)
        sky = QLinearGradient(452, 80, 452, 321)
        sky.setColorAt(0, QColor("#060b1a"))
        sky.setColorAt(1, QColor("#1d3358"))
        p.fillRect(QRectF(463, 80, 293, 235), sky)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(235, 242, 255, 210))
        for x, y, r in ((480, 96, 1.4), (512, 124, 1.1), (545, 92, 1.6), (590, 110, 1.2), (628, 98, 1.5),
                        (660, 132, 1.1), (606, 150, 1.0), (500, 170, 1.2), (570, 188, 1.0), (744, 104, 1.3),
                        (688, 96, 1.2), (528, 210, 1.0), (636, 196, 1.1), (736, 178, 1.0)):
            p.drawEllipse(QPointF(x, y), r, r)
        moon_glow = QRadialGradient(QPointF(716, 120), 46)
        moon_glow.setColorAt(0, QColor(255, 244, 205, 120))
        moon_glow.setColorAt(1, QColor(255, 244, 205, 0))
        p.setBrush(moon_glow)
        p.drawEllipse(QPointF(716, 120), 46, 46)
        p.setBrush(QColor("#f4efd6"))
        p.drawEllipse(QPointF(716, 120), 14, 14)
        p.setBrush(QColor("#d9d2b4"))
        p.drawEllipse(QPointF(711, 116), 3, 3)
        for x, y, w, h in ((470, 262, 34, 53), (508, 244, 22, 71), (534, 276, 28, 39), (580, 268, 40, 47),
                           (654, 252, 46, 63), (705, 232, 28, 83), (730, 270, 26, 45)):
            rect(x, y, w, h, "#0a1324")
        p.setBrush(QColor("#f0d27a"))
        for x, y in ((476, 272), (488, 286), (514, 254), (514, 270), (520, 292), (538, 286), (588, 280),
                     (604, 294), (662, 262), (680, 276), (690, 296), (712, 244), (718, 266), (716, 296),
                     (738, 284)):
            p.drawRect(QRectF(x, y, 4, 5))
        for x in (557, 659):
            rect(x, 80, 6, 235, "#13223a")
        for y in range(91, 251, 17):
            rect(463, y, 293, 2, QColor(120, 150, 205, 26))
        rect(448, 70, 324, 12, "#1b2c45", 3)
        rect(447, 313, 326, 9, "#22364f", 3)

        # Whiteboard, notice board and shelf on the left (the single lamp stands on the shelf).
        rect(244, 91, 203, 174, "#22344a", 5)
        rect(250, 96, 191, 161, "#cdd7d6", 3)
        p.setPen(QColor("#38566f"))
        font = p.font()
        font.setPixelSize(14)
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRectF(264, 97, 163, 34), Qt.AlignmentFlag.AlignCenter, "PRODUCER'S BOARD")
        for y in range(136, 240, 20):
            line(263, y, 428, y, "#aebcc0")
        line(315, 132, 315, 242, "#aebcc0")
        for x, y, color in ((275, 144, "#5f8fb0"), (333, 166, "#cf9568"), (369, 206, "#6ea39e")):
            rect(x, y, 33, 7, color, 3)
        rect(245, 255, 203, 8, "#3a5268", 2)
        rect(384, 253, 27, 4, "#267aa2", 2)
        rect(25, 100, 65, 104, "#22364b", 3)
        rect(30, 105, 55, 95, "#c9bd9c", 2)
        rect(30, 105, 55, 19, "#55778d", 2)
        for y in range(137, 190, 10):
            line(36, y, 79, y, "#a8916f")
        rect(97, 236, 121, 130, "#3a352f", 4)
        rect(103, 243, 110, 117, "#8d826c", 2)
        for y in (275, 318, 360):
            rect(101, y, 114, 6, "#5e4f3c")
        for x, w, h, color in ((112, 17, 25, "#456f8c"), (132, 18, 28, "#6f8379"),
                               (153, 20, 22, "#b8a784"), (177, 15, 26, "#80564f")):
            rect(x, 275 - h, w, h, color, 2)
        for y in (286, 296, 306):
            rect(110, y, 66, 6, "#c9c2b2", 1)
        rect(115, 327, 45, 27, "#7d8d81", 2)
        rect(169, 324, 34, 32, "#98764f", 2)
        rect(192, 220, 23, 19, "#4f6d78", 2)
        for i in range(4):
            line(195 + i * 5, 221, 192 + i * 6, 208, ("#1f4059", "#8f6850")[i % 2], 2)
        rect(118, 232, 30, 5, "#2d3947", 2)
        line(133, 232, 133, 214, "#a8b6c6", 3)
        polygon([(112, 214), (154, 214), (146, 193), (120, 193)], "#f3c46f")
        light = QRadialGradient(QPointF(133, 208), 330)
        light.setColorAt(0, QColor(255, 198, 112, 125))
        light.setColorAt(0.45, QColor(255, 190, 105, 48))
        light.setColorAt(1, QColor(255, 190, 105, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(light)
        p.drawRect(QRectF(0, 0, 470, 470))

        # The chair is behind the work surface; its feet remain on the floor.
        rect(730, 323, 76, 74, "#22364a", 19)
        rect(739, 331, 57, 49, "#304a63", 15)
        line(768, 387, 768, 484, "#1d2d3c", 8)
        for x in (740, 795):
            line(768, 474, x, 487, "#1d2d3c", 5)
        rect(766, 398, 279, 24, "#33424f", 5)
        rect(776, 419, 12, 125, "#1f2d3a", 2)
        rect(1017, 416, 12, 125, "#1f2d3a", 2)
        rect(970, 424, 42, 88, "#3b4a55", 3)
        for y in (451, 479):
            line(974, y, 1008, y, "#26343f")
        polygon([(765, 399), (1047, 399), (1007, 370), (796, 370)], "#6f6352")
        rect(820, 281, 156, 93, "#0f1b2a", 7)
        rect(826, 287, 144, 80, "#07111d", 5)
        screen = QLinearGradient(838, 298, 958, 353)
        screen.setColorAt(0, QColor("#16466e"))
        screen.setColorAt(1, QColor("#0b2038"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(screen)
        p.drawRoundedRect(QRectF(830, 291, 136, 72), 4, 4)
        glow = QRadialGradient(QPointF(898, 327), 52)
        glow.setColorAt(0, QColor(120, 200, 255, 70))
        glow.setColorAt(1, QColor(120, 200, 255, 0))
        p.setBrush(glow)
        p.drawEllipse(QPointF(898, 327), 52, 40)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#9ad6ff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawRoundedRect(QRectF(874, 312, 48, 31), 4, 4)
        p.drawPolyline(QPolygonF([QPointF(876, 316), QPointF(898, 331), QPointF(920, 316)]))
        rect(886, 374, 21, 9, "#0f1b2a", 2)
        rect(870, 381, 53, 4, "#0f1b2a", 2)
        polygon([(862, 385), (921, 385), (928, 394), (854, 394)], "#2c3d4c")
        for y in (387, 390, 393):
            for x in range(864, 920, 7):
                line(x, y, x + 4, y, "#1d2c39")
        rect(939, 385, 13, 9, "#33495a", 4)
        polygon([(799, 383), (812, 358), (825, 383)], "#6a5844")
        rect(795, 358, 19, 21, "#c8bfa9", 2)
        line(796, 363, 812, 363, "#9a6a58", 3)
        rect(1008, 377, 12, 13, "#556459", 3)

    @staticmethod
    def draw_front_desk(p: QPainter) -> None:
        def shape(points, color):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawPolygon(QPolygonF([QPointF(*point) for point in points]))

        top = [(280, 405), (682, 405), (728, 460), (227, 460)]
        shape(top, "#6f634f")
        warm = QLinearGradient(240, 430, 700, 430)
        warm.setColorAt(0, QColor(255, 196, 110, 95))
        warm.setColorAt(1, QColor(255, 196, 110, 0))
        p.setBrush(warm)
        p.drawPolygon(QPolygonF([QPointF(*point) for point in top]))
        shape([(227, 460), (728, 460), (728, 474), (227, 474)], "#3f372c")
        shape([(246, 474), (270, 474), (264, 580), (250, 580)], "#1f3042")
        shape([(681, 474), (704, 474), (709, 583), (695, 583)], "#1f3042")
        shape([(309, 427), (406, 427), (424, 448), (321, 448)], "#4d6264")
        shape([(313, 417), (390, 415), (415, 441), (325, 445)], "#d6cfbd")
        for y in (423, 427, 431, 435):
            p.setPen(QPen(QColor("#8b9696"), 0.8))
            p.drawLine(QPointF(334, y), QPointF(392, y - 2))
        shape([(485, 434), (520, 434), (528, 444), (490, 444)], "#3d5b73")
        shape([(491, 432), (521, 432), (523, 436), (490, 436)], "#7ea1b3")
        shape([(610, 430), (663, 430), (676, 447), (620, 447)], "#7e604d")
        shape([(612, 425), (653, 424), (661, 435), (617, 436)], "#cfc7ad")
