"""Custom-painted, Google-Calendar-style dark week/day time grid."""

from __future__ import annotations

import html
import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QScrollBar, QSizePolicy, QToolTip, QWidget

from calendar_reader import Event

__all__ = ["WeekGrid", "calendar_color", "EVENT_PALETTE", "WEEKDAY_LABELS"]

BG = "#0f1722"
GUTTER_TEXT = "#9fb3c8"
GRID_LINE = "#2b3b4f"
TODAY_FILL = "#5aa9e6"
TODAY_TEXT = "#0b1a2a"
NOW_LINE = "#ea5b4a"
EVENT_TEXT = "#0b1a2a"
DATE_TEXT = "#e3ecf5"
HOVER_CIRCLE = "#1d2b3c"
SELECTED_OUTLINE = "#f2f8ff"
PLAN_FILL = "#16283d"
PLAN_OUTLINE = "#6fb1ea"
PLAN_TEXT = "#cfe8ff"
DIM_TEXT = "#bccbdb"
MASKED_TEXT = "#92a3b7"

EVENT_PALETTE = (
    "#4a96d6",  # blue
    "#d9533a",  # red
    "#8a8f98",  # grey
    "#3f9a63",  # green
    "#8b6fd1",  # purple
    "#2fa39b",  # teal
    "#d98a2b",  # orange
    "#d9619b",  # pink
)

# Indexed by date.weekday() (Monday == 0).
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

NO_TITLE = "（无标题）"
MAX_CHIPS = 3
MIN_BLOCK_H = 18
SCROLLBAR_W = 8
DEFAULT_HOUR_H = 44
DEFAULT_START_HOUR = 7.0
MIN_TIMED_MINUTES = 15.0
OVERLAY_SHARE = 0.38
OVERLAY_MIN_W = 56
DIM_FADE = 0.55
MASKED_FADE = 0.65
LANE_TEXT_GAP = 4

NOTE_LOCAL = "本机安排（不会写入 Google 日历）"
NOTE_MASKED = "已在本机取消（Google 日历没有改动）"

SCROLLBAR_STYLE = f"""
QScrollBar:vertical {{ background: transparent; width: {SCROLLBAR_W}px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {GRID_LINE}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #3d5470; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; width: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


def _flags(*parts) -> int:
    return sum(int(part.value) for part in parts)


ALIGN_CENTER = _flags(Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignVCenter)
ALIGN_LEFT = _flags(Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignVCenter)
ALIGN_RIGHT = _flags(Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter)
ALIGN_WRAP_CENTER = _flags(Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignVCenter, Qt.TextFlag.TextWordWrap)


def calendar_color(name: str, overrides: dict[str, str] | None = None) -> str:
    """Stable fill color for a calendar name, honoring an optional override map."""
    if overrides:
        custom = overrides.get(name)
        if custom and QColor(custom).isValid():
            return custom
    return EVENT_PALETTE[zlib.crc32(name.encode("utf-8")) % len(EVENT_PALETTE)]


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    return value.replace(tzinfo=zone) if value.tzinfo is None else value


def _wall_minutes(value: datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60 + value.microsecond / 6e7


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _scaled_font(base: QFont, factor: float, bold: bool = False) -> QFont:
    font = QFont(base)
    if base.pointSizeF() > 0:
        font.setPointSizeF(base.pointSizeF() * factor)
    elif base.pixelSize() > 0:
        font.setPixelSize(max(1, round(base.pixelSize() * factor)))
    font.setBold(bold)
    return font


def _blend(color: QColor, other: QColor, amount: float) -> QColor:
    """`color` moved `amount` (0..1) of the way towards `other`."""
    def mix(a: int, b: int) -> int:
        return round(a + (b - a) * amount)

    return QColor(mix(color.red(), other.red()), mix(color.green(), other.green()), mix(color.blue(), other.blue()))


def _date_text(day: date) -> str:
    return f"{day.month}月{day.day}日 {WEEKDAY_LABELS[day.weekday()]}"


def _gmt_caption(offset: timedelta | None) -> str:
    minutes = int((offset or timedelta()).total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours}" + (f":{rest:02d}" if rest else "")


@dataclass(eq=False)
class _Seg:
    event: Event
    day: int
    start: float
    end: float
    all_day: bool
    color: QColor
    title: str
    time_text: str
    col: int = 0
    span: int = 1
    ncols: int = 1
    row: int = 0
    visible: bool = True
    source: int = 0
    overlay: bool = False  # a local plan drawn as a narrow right-hand lane over real events
    dim: bool = False  # a real event covered by at least one local plan
    lanes: list[_Seg] = field(default_factory=list)  # overlay plans drawn over this block


def _effective_end(seg: _Seg, min_minutes: float) -> float:
    return max(seg.end, seg.start + min_minutes)


def _overlaps(a: _Seg, b: _Seg, min_minutes: float) -> bool:
    return a.start < _effective_end(b, min_minutes) and b.start < _effective_end(a, min_minutes)


def _finish_cluster(cluster: list[_Seg], min_minutes: float) -> None:
    ends: list[float] = []
    columns: list[list[_Seg]] = []
    for seg in cluster:
        end = _effective_end(seg, min_minutes)
        for index, last_end in enumerate(ends):
            if last_end <= seg.start + 1e-9:
                seg.col = index
                ends[index] = end
                columns[index].append(seg)
                break
        else:
            seg.col = len(ends)
            ends.append(end)
            columns.append([seg])
    total = len(columns)
    for seg in cluster:
        seg.ncols = total
        span = 1
        for index in range(seg.col + 1, total):
            if any(_overlaps(seg, o, min_minutes) for o in columns[index]):
                break
            span += 1
        seg.span = span


def _pack_columns(segs: list[_Seg], min_minutes: float) -> None:
    """Google-style packing: equal sub-columns per overlap cluster, widened into free columns."""
    segs.sort(key=lambda s: (s.start, -s.end, s.title, s.event.calendar))
    cluster: list[_Seg] = []
    cluster_end = -1.0
    for seg in segs:
        if cluster and seg.start >= cluster_end - 1e-9:
            _finish_cluster(cluster, min_minutes)
            cluster = []
            cluster_end = -1.0
        cluster.append(seg)
        cluster_end = max(cluster_end, _effective_end(seg, min_minutes))
    if cluster:
        _finish_cluster(cluster, min_minutes)


def _layout_day(segs: list[_Seg], min_minutes: float) -> list[_Seg]:
    """Pack one day's blocks and return them bottom-to-top.

    Real events pack among themselves. A local plan touching a real event becomes an overlay lane
    (packed only among the overlays); other plans pack full width among themselves.
    """
    real = [s for s in segs if not s.event.local]
    plans = [s for s in segs if s.event.local]
    overlays = [p for p in plans if any(_overlaps(p, r, min_minutes) for r in real)]
    for plan in overlays:
        plan.overlay = True
    flat = [p for p in plans if not p.overlay]
    for seg in real + flat:
        seg.lanes = [o for o in overlays if _overlaps(seg, o, min_minutes)]
        seg.dim = bool(seg.lanes) and not seg.event.local
    for group in (real, flat, overlays):
        _pack_columns(group, min_minutes)
    return real + flat + overlays


def _look(seg: _Seg) -> tuple[QColor, QPen, QColor]:
    """Fill, outline pen and text colour of an item."""
    base, background = seg.color, QColor(BG)
    if seg.event.local:
        return QColor(PLAN_FILL), QPen(QColor(PLAN_OUTLINE), 1.5), QColor(PLAN_TEXT)
    outline = QPen(base.darker(140), 1)
    if seg.event.masked:
        outline.setColor(_blend(base, background, 0.4))
        outline.setDashPattern([3.0, 2.0])
        return _blend(base, background, MASKED_FADE), outline, QColor(MASKED_TEXT)
    if seg.dim:
        return _blend(base, background, DIM_FADE), outline, QColor(DIM_TEXT)
    fill = QColor(base)
    if not seg.event.busy:
        fill.setAlpha(205)
    return fill, outline, QColor(EVENT_TEXT)


class WeekGrid(QWidget):
    event_selected = Signal(object)
    event_activated = Signal(object)
    day_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._days: list[date] = []
        self._events: list[Event] = []
        self._zone: ZoneInfo | None = None
        self._now: datetime | None = None
        self._colors: dict[str, str] | None = None
        self._today = -1
        self._now_minutes = 0.0
        self._gmt = ""
        self._message = ""
        self._timed: list[list[_Seg]] = []
        self._allday: list[list[_Seg]] = []
        self._nav: list[_Seg] = []
        self._order: list[_Seg] = []
        self._allday_rows = 0
        self._allday_h = 0
        self._sel: _Seg | None = None
        self._hover: _Seg | None = None
        self._hover_header = -1
        self._desired_scroll = 0
        self._auto_pending = True

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("行程时间表")
        self.setAccessibleDescription("共 0 个行程")

        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        self._scrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scrollbar.valueChanged.connect(self._on_scrollbar)
        self._scrollbar.hide()

        self._update_metrics()
        self._update_allday_height()
        self._sync_scrollbar()

    # ------------------------------------------------------------------ public API

    def set_range(
        self,
        days: Sequence[date],
        events: Sequence[Event],
        zone: ZoneInfo,
        now: datetime | None = None,
        colors: dict[str, str] | None = None,
    ) -> None:
        selection = self._selection_key()

        self._days = [d.date() if isinstance(d, datetime) else d for d in days]
        self._events = list(events)
        self._zone = zone
        self._colors = dict(colors) if colors else None
        now_local = datetime.now(zone) if now is None else _aware(now, zone).astimezone(zone)
        self._now = now_local
        self._now_minutes = _wall_minutes(now_local)
        self._today = self._days.index(now_local.date()) if now_local.date() in self._days else -1
        self._gmt = _gmt_caption(now_local.utcoffset())
        self._hover = None
        self._hover_header = -1

        count = self._rebuild()
        self._update_allday_height()
        self.setAccessibleDescription(f"共 {count} 个行程")

        self._restore_selection(selection)
        if self._auto_pending:
            self._apply_scroll(self._auto_target_hour() * self._hour_h)
            # Keep adapting while the widget is hidden or still being laid out, so the first real size decides.
            if self.isVisible() and (self._today >= 0 or any(self._timed)):
                self._auto_pending = False
        self._sync_scrollbar()
        self.update()
        if selection is not None and self._sel is None:
            self.event_selected.emit(None)

    def set_message(self, text: str) -> None:
        text = text or ""
        if text != self._message:
            self._message = text
            self.update()

    def message(self) -> str:
        return self._message

    @property
    def selected_event(self) -> Event | None:
        return self._sel.event if self._sel else None

    def select_event(self, event: Event | None) -> None:
        seg = None
        if event is not None:
            seg = next((s for s in self._nav if s.event == event), None)
        self._select(seg, ensure_visible=True)

    def event_rects(self) -> list[tuple[Event, QRectF]]:
        """Widget-coordinate rectangles of all timed blocks, bottom-to-top (overlay lanes last), unclipped."""
        return [(seg.event, self._block_rect(seg)) for day in self._timed for seg in day]

    def describe_layout(self) -> list[dict]:
        """Plain per-block layout facts (same order as `event_rects`), for tests and diagnostics."""
        return [
            {"event": seg.event, "day": self._days[seg.day], "col": seg.col, "ncols": seg.ncols, "span": seg.span,
             "overlay": seg.overlay, "dim": seg.dim, "masked": seg.event.masked, "local": seg.event.local}
            for day in self._timed for seg in day
        ]

    def all_day_rects(self) -> list[tuple[Event, QRectF]]:
        """Widget-coordinate rectangles of the visible all-day chips."""
        return [(seg.event, self._chip_rect(seg)) for day in self._allday for seg in day if seg.visible]

    def all_day_overflow(self) -> dict[date, int]:
        """Number of hidden all-day events per day (days without overflow are omitted)."""
        return {self._days[i]: n for i in range(len(self._days)) if (n := self._overflow(i)) > 0}

    def day_column_rects(self) -> list[QRectF]:
        """Widget-coordinate x-ranges of the day columns over the scrolling grid area."""
        top = self._grid_top()
        return [QRectF(x0, top, x1 - x0, max(0, self.height() - top)) for x0, x1 in map(self._col_x, range(len(self._days)))]

    def event_at(self, pos: QPointF | QPoint) -> Event | None:
        hit = self._hit(QPointF(pos))
        if hit and hit[0] in ("block", "chip"):
            return hit[1].event
        return None

    def tooltip_at(self, pos: QPointF | QPoint) -> str:
        """Plain-text tooltip for a position ('' when nothing is under it)."""
        hit = self._hit(QPointF(pos))
        if not hit:
            return ""
        kind, payload = hit
        if kind in ("block", "chip"):
            ev = payload.event
            lines = [payload.title, self._when_text(ev)]
            if ev.local:
                lines.append(NOTE_LOCAL)
            else:
                lines.append(f"日历：{ev.calendar}")
                if ev.masked:
                    lines.append(NOTE_MASKED)
            return "\n".join(lines)
        if kind == "more":
            hidden = [s.title for s in self._allday[payload] if not s.visible]
            return "\n".join([f"还有 {len(hidden)} 个全天行程："] + hidden[:12] + (["…"] if len(hidden) > 12 else []))
        return ""

    def gutter_caption(self) -> str:
        return self._gmt

    def scroll_to_hour(self, hour: float) -> None:
        self._auto_pending = False
        self._apply_scroll(hour * self._hour_h)

    def scroll_hour(self) -> float:
        """Hour (0..24) currently aligned with the top of the scrolling grid."""
        return self._scroll() / self._hour_h

    def visible_hours(self) -> tuple[float, float]:
        top = (self._scroll() - self._top_pad) / self._hour_h
        bottom = (self._scroll() - self._top_pad + self._viewport_h()) / self._hour_h
        return (_clamp(top, 0.0, 24.0), _clamp(bottom, 0.0, 24.0))

    def sizeHint(self) -> QSize:
        return QSize(960, 640)

    def minimumSizeHint(self) -> QSize:
        return QSize(320, 240)

    # ------------------------------------------------------------------ metrics

    def _update_metrics(self) -> None:
        base = self.font()
        self._font_small = _scaled_font(base, 0.88)
        self._font_caption = _scaled_font(base, 0.8)
        self._font_event = _scaled_font(base, 0.94)
        self._font_number = _scaled_font(base, 1.5)
        small = QFontMetrics(self._font_small)
        number = QFontMetrics(self._font_number)
        self._small_h = small.height()
        self._event_h = QFontMetrics(self._font_event).height()
        self._chip_h = self._event_h + 4
        self._hour_h = round(DEFAULT_HOUR_H * max(1.0, QFontMetrics(base).height() / 16.0))
        self._gutter_w = max(56, small.horizontalAdvance("00:00") + 18)
        self._circle_d = max(32, round(number.height() * 1.3))
        self._header_h = 6 + self._small_h + 2 + self._circle_d + 8
        self._top_pad = self._small_h // 2 + 3
        self._bottom_pad = 8

    def _update_allday_height(self) -> None:
        self._allday_h = 8 if self._allday_rows == 0 else 4 + self._allday_rows * (self._chip_h + 2) + 2

    def _grid_top(self) -> int:
        return self._header_h + self._allday_h

    def _viewport_h(self) -> int:
        return max(0, self.height() - self._grid_top())

    def _content_h(self) -> int:
        return self._top_pad + 24 * self._hour_h + self._bottom_pad

    def _max_scroll(self) -> int:
        return max(0, self._content_h() - self._viewport_h())

    def _scroll(self) -> int:
        return int(_clamp(self._desired_scroll, 0, self._max_scroll()))

    def _content_w(self) -> float:
        reserved = SCROLLBAR_W if self._max_scroll() > 0 else 0
        return max(0.0, self.width() - self._gutter_w - reserved)

    def _col_x(self, index: int) -> tuple[float, float]:
        count = max(1, len(self._days))
        width = self._content_w() / count
        return (self._gutter_w + index * width, self._gutter_w + (index + 1) * width)

    def _y_of(self, minutes: float) -> float:
        return self._grid_top() + self._top_pad + minutes / 60.0 * self._hour_h - self._scroll()

    # ------------------------------------------------------------------ layout build

    def _rebuild(self) -> int:
        days, zone = self._days, self._zone
        self._timed = [[] for _ in days]
        self._allday = [[] for _ in days]
        bounds = []
        for day in days:
            start = datetime.combine(day, time.min, zone).timestamp()
            end = datetime.combine(day + timedelta(days=1), time.min, zone).timestamp()
            bounds.append((start, end))

        shown = 0
        for source, ev in enumerate(self._events):
            color = QColor(calendar_color(ev.calendar, self._colors))
            title = " ".join((ev.title or "").split()) or NO_TITLE
            start = _aware(ev.start, zone).astimezone(zone)
            end = _aware(ev.end, zone).astimezone(zone)
            placed = False
            if ev.all_day:
                first = start.date()
                last_excl = end.date() if end.time() == time.min else end.date() + timedelta(days=1)
                if last_excl <= first:
                    last_excl = first + timedelta(days=1)
                for index, day in enumerate(days):
                    if first <= day < last_excl:
                        self._allday[index].append(_Seg(ev, index, -1.0, -1.0, True, color, title, "全天", source=source))
                        placed = True
            else:
                start_ts, end_ts = start.timestamp(), end.timestamp()
                zero = end_ts <= start_ts
                if zero:
                    end_ts = start_ts + 1
                start_local = datetime.fromtimestamp(start_ts, zone)
                end_local = datetime.fromtimestamp(end_ts, zone)
                time_text = f"{start_local:%H:%M}" if zero else f"{start_local:%H:%M}–{end_local:%H:%M}"
                for index, (day_start, day_end) in enumerate(bounds):
                    if start_ts >= day_end or end_ts <= day_start:
                        continue
                    s_min = 0.0 if start_ts <= day_start else min(_wall_minutes(start_local), 1439.0)
                    e_min = 1440.0 if end_ts >= day_end else _wall_minutes(end_local)
                    if zero:
                        e_min = min(1440.0, s_min + MIN_TIMED_MINUTES)
                    elif e_min <= s_min:
                        e_min = min(1440.0, s_min + max(MIN_TIMED_MINUTES, (end_ts - start_ts) / 60.0))
                    self._timed[index].append(_Seg(ev, index, s_min, e_min, False, color, title, time_text, source=source))
                    placed = True
            shown += placed and not ev.local

        min_minutes = MIN_BLOCK_H * 60.0 / self._hour_h
        rows = 0
        for index in range(len(days)):
            self._timed[index] = _layout_day(self._timed[index], min_minutes)
            chips = self._allday[index]
            chips.sort(key=lambda s: (-(s.event.end - s.event.start).total_seconds(), s.title, s.event.calendar))
            for row, seg in enumerate(chips):
                seg.row = row
                seg.visible = row < MAX_CHIPS
            rows = max(rows, min(len(chips), MAX_CHIPS) + (1 if len(chips) > MAX_CHIPS else 0))
        self._allday_rows = rows

        self._nav = sorted(
            (seg for index in range(len(days)) for seg in self._allday[index] + self._timed[index]),
            key=lambda s: (s.day, s.start, s.end, s.title),
        )
        first_seen: set[int] = set()
        self._order = []
        for seg in self._nav:
            if seg.source not in first_seen:
                first_seen.add(seg.source)
                self._order.append(seg)
        return shown

    def _overflow(self, index: int) -> int:
        return max(0, len(self._allday[index]) - MAX_CHIPS)

    def _auto_target_hour(self) -> float:
        starts = [seg.start for day in self._timed for seg in day]
        visible = self._viewport_h() / max(1, self._hour_h)
        base = DEFAULT_START_HOUR
        if starts:
            earliest = max(0.0, math.floor(min(starts) / 60.0) - 0.5)
            if earliest < base or earliest + 1.0 > base + visible:
                base = earliest
        if self._today >= 0:
            # Show the morning like Google Calendar, unless "now" would fall below the visible area.
            now_hour = self._now_minutes / 60.0
            if now_hour + 1.0 > base + visible:
                return float(max(0, int(now_hour) - 1))
        return base

    # ------------------------------------------------------------------ geometry of items

    def _block_rect(self, seg: _Seg) -> QRectF:
        x0, x1 = self._col_x(seg.day)
        left, right = x0 + 1, x1 - 3
        if seg.overlay:
            left = max(left, right - max(OVERLAY_SHARE * (x1 - x0), OVERLAY_MIN_W))
        sub = (right - left) / seg.ncols
        x_left = left + seg.col * sub + (0 if seg.col == 0 else 1)
        x_right = left + (seg.col + seg.span) * sub - (0 if seg.col + seg.span == seg.ncols else 1)
        top = self._y_of(seg.start)
        height = max(MIN_BLOCK_H, self._y_of(seg.end) - top - 1)
        return QRectF(x_left, top, max(1.0, x_right - x_left), height)

    def _chip_rect(self, seg: _Seg) -> QRectF:
        x0, x1 = self._col_x(seg.day)
        return QRectF(x0 + 3, self._header_h + 4 + seg.row * (self._chip_h + 2), max(1.0, x1 - x0 - 6), self._chip_h)

    def _more_rect(self, index: int) -> QRectF:
        x0, x1 = self._col_x(index)
        row = min(len(self._allday[index]), MAX_CHIPS)
        return QRectF(x0 + 3, self._header_h + 4 + row * (self._chip_h + 2), max(1.0, x1 - x0 - 6), self._chip_h)

    def _hit(self, pos: QPointF):
        count = len(self._days)
        width = self._content_w()
        if count == 0 or width <= 0:
            return None
        x, y = pos.x(), pos.y()
        if x < self._gutter_w or x >= self._gutter_w + width or y < 0 or y >= self.height():
            return None
        index = min(count - 1, int((x - self._gutter_w) / (width / count)))
        if y < self._header_h:
            return ("header", index)
        if y < self._grid_top():
            for seg in self._allday[index]:
                if seg.visible and self._chip_rect(seg).contains(pos):
                    return ("chip", seg)
            if self._overflow(index) and self._more_rect(index).contains(pos):
                return ("more", index)
            return None
        for seg in reversed(self._timed[index]):  # topmost first: overlay lanes are stored last
            if self._block_rect(seg).contains(pos):
                return ("block", seg)
        return None

    # ------------------------------------------------------------------ text helpers

    def _when_text(self, ev: Event) -> str:
        zone = self._zone
        start = _aware(ev.start, zone).astimezone(zone)
        end = _aware(ev.end, zone).astimezone(zone)
        if ev.all_day:
            last = end.date() if end.time() == time.min else end.date() + timedelta(days=1)
            last = max(start.date(), last - timedelta(days=1))
            if last > start.date():
                return f"{_date_text(start.date())} – {_date_text(last)}（全天）"
            return f"{_date_text(start.date())}（全天）"
        if end <= start or end.timestamp() <= start.timestamp():
            return f"{_date_text(start.date())} {start:%H:%M}"
        if start.date() == end.date():
            return f"{_date_text(start.date())} {start:%H:%M}–{end:%H:%M}"
        return f"{_date_text(start.date())} {start:%H:%M} – {_date_text(end.date())} {end:%H:%M}"

    # ------------------------------------------------------------------ scrolling

    def _apply_scroll(self, pixels: float) -> None:
        self._desired_scroll = int(round(pixels))
        self._sync_scrollbar()
        self.update()

    def _sync_scrollbar(self) -> None:
        maximum = self._max_scroll()
        bar = self._scrollbar
        bar.blockSignals(True)
        bar.setRange(0, maximum)
        bar.setPageStep(max(1, self._viewport_h()))
        bar.setSingleStep(max(1, self._hour_h // 2))
        bar.setValue(self._scroll())
        bar.blockSignals(False)
        bar.setGeometry(self.width() - SCROLLBAR_W, self._grid_top(), SCROLLBAR_W, self._viewport_h())
        bar.setVisible(maximum > 0)

    def _on_scrollbar(self, value: int) -> None:
        self._auto_pending = False
        self._desired_scroll = value
        self.update()

    def _scroll_by(self, delta: float) -> bool:
        if self._max_scroll() <= 0:
            return False
        self._auto_pending = False
        self._apply_scroll(self._scroll() + delta)
        return True

    def _ensure_visible(self, seg: _Seg) -> None:
        if seg.all_day:
            return
        margin = 6
        top = self._top_pad + seg.start / 60.0 * self._hour_h
        bottom = self._top_pad + max(seg.end / 60.0 * self._hour_h, top + MIN_BLOCK_H)
        current, viewport = self._scroll(), self._viewport_h()
        if top - margin < current:
            self._auto_pending = False
            self._apply_scroll(top - margin)
        elif bottom + margin > current + viewport:
            self._auto_pending = False
            self._apply_scroll(bottom + margin - viewport)

    # ------------------------------------------------------------------ selection

    def _select(self, seg: _Seg | None, ensure_visible: bool = False) -> None:
        old = self._sel.event if self._sel else None
        self._sel = seg
        if seg is not None and ensure_visible:
            self._ensure_visible(seg)
        self.update()
        new = seg.event if seg else None
        if old != new:
            self.event_selected.emit(new)

    def _selection_key(self) -> tuple[Event, date | None] | None:
        if self._sel is None:
            return None
        day = self._days[self._sel.day] if self._sel.day < len(self._days) else None
        return (self._sel.event, day)

    def _restore_selection(self, key: tuple[Event, date | None] | None) -> None:
        self._sel = None
        if key is None:
            return
        event, day = key
        candidates = [s for s in self._nav if s.event == event]
        same_day = [s for s in candidates if self._days[s.day] == day]
        if candidates:
            self._sel = (same_day or candidates)[0]

    def _is_selected(self, seg: _Seg) -> bool:
        return self._sel is not None and seg.event == self._sel.event

    def _step(self, direction: int) -> None:
        order = self._order
        if not order:
            return
        if self._sel is None:
            self._select(order[0] if direction > 0 else order[-1], ensure_visible=True)
            return
        position = next((i for i, s in enumerate(order) if s.source == self._sel.source), None)
        if position is None:
            return
        target = position + direction
        if 0 <= target < len(order):
            self._select(order[target], ensure_visible=True)

    def _step_day(self, direction: int) -> None:
        if self._sel is None:
            self._step(direction)
            return
        ref = self._sel
        day = ref.day + direction
        while 0 <= day < len(self._days):
            candidates = [s for s in self._nav if s.day == day and s.event != ref.event]
            if candidates:
                best = min(candidates, key=lambda s: (abs(s.start - ref.start), s.start, s.title))
                self._select(best, ensure_visible=True)
                return
            day += direction

    # ------------------------------------------------------------------ Qt events

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self._auto_pending and self._zone is not None and self._hour_h:
            self._apply_scroll(self._auto_target_hour() * self._hour_h)
        self._sync_scrollbar()

    def changeEvent(self, ev) -> None:
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.FontChange and hasattr(self, "_font_small"):
            old_hour = self._hour_h
            self._update_metrics()
            self._desired_scroll = round(self._desired_scroll * self._hour_h / old_hour)
            if self._zone is not None:
                selection = self._selection_key()
                self._rebuild()
                self._restore_selection(selection)
                self._hover = None
            self._update_allday_height()
            self._sync_scrollbar()
            self.update()

    def focusInEvent(self, ev) -> None:
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:
        super().focusOutEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:
        super().leaveEvent(ev)
        if self._hover is not None or self._hover_header >= 0:
            self._hover, self._hover_header = None, -1
            self.unsetCursor()
            self.update()

    def wheelEvent(self, ev) -> None:
        pixels = ev.pixelDelta().y()
        delta = pixels if pixels else ev.angleDelta().y() / 120.0 * self._hour_h * 0.75
        if delta and self._scroll_by(-delta):
            ev.accept()
        else:
            ev.ignore()

    def mouseMoveEvent(self, ev) -> None:
        hit = self._hit(ev.position())
        hover = hit[1] if hit and hit[0] in ("block", "chip") else None
        header = hit[1] if hit and hit[0] == "header" else -1
        if hover is not self._hover or header != self._hover_header:
            self._hover, self._hover_header = hover, header
            self.update()
        if hit and hit[0] in ("block", "chip", "header", "more"):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        hit = self._hit(ev.position())
        if hit and hit[0] == "header":
            self.day_selected.emit(self._days[hit[1]])
        elif hit and hit[0] == "more":
            self.day_selected.emit(self._days[hit[1]])
        elif hit and hit[0] in ("block", "chip"):
            self._select(hit[1])
        else:
            self._select(None)
        ev.accept()

    def mouseDoubleClickEvent(self, ev) -> None:
        hit = self._hit(ev.position()) if ev.button() == Qt.MouseButton.LeftButton else None
        if hit and hit[0] in ("block", "chip"):
            self._select(hit[1])
            self.event_activated.emit(hit[1].event)
            ev.accept()
        else:
            self.mousePressEvent(ev)

    def keyPressEvent(self, ev) -> None:
        blocked = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        if ev.modifiers() & blocked:
            super().keyPressEvent(ev)
            return
        key = ev.key()
        handled = True
        if key == Qt.Key.Key_Down:
            self._step(1)
        elif key == Qt.Key.Key_Up:
            self._step(-1)
        elif key == Qt.Key.Key_Right:
            self._step_day(1)
        elif key == Qt.Key.Key_Left:
            self._step_day(-1)
        elif key == Qt.Key.Key_Home and self._order:
            self._select(self._order[0], ensure_visible=True)
        elif key == Qt.Key.Key_End and self._order:
            self._select(self._order[-1], ensure_visible=True)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self._sel is not None:
            self.event_activated.emit(self._sel.event)
        elif key == Qt.Key.Key_Escape and self._sel is not None:
            self._select(None)
        elif key == Qt.Key.Key_PageDown:
            handled = self._scroll_by(max(1, self._viewport_h() - self._hour_h))
        elif key == Qt.Key.Key_PageUp:
            handled = self._scroll_by(-max(1, self._viewport_h() - self._hour_h))
        else:
            handled = False
        if handled:
            ev.accept()
        else:
            super().keyPressEvent(ev)

    def event(self, ev) -> bool:
        if ev.type() == QEvent.Type.ToolTip:
            text = self.tooltip_at(ev.pos())
            if text:
                rich = "<br/>".join(html.escape(line) for line in text.split("\n"))
                QToolTip.showText(ev.globalPos(), rich, self)
            else:
                QToolTip.hideText()
            ev.accept()
            return True
        return super().event(ev)

    # ------------------------------------------------------------------ painting

    def paintEvent(self, ev) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), QColor(BG))
        self._paint_grid(painter)
        self._paint_header(painter)
        self._paint_allday(painter)
        if self._message:
            self._paint_message(painter)
        if self.hasFocus():
            painter.setPen(QPen(QColor(TODAY_FILL), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        painter.end()

    def _line_pen(self) -> QPen:
        return QPen(QColor(GRID_LINE), 1)

    def _paint_grid(self, p: QPainter) -> None:
        top, height = self._grid_top(), self._viewport_h()
        if height <= 0:
            return
        right = self._gutter_w + self._content_w()
        p.save()
        p.setClipRect(QRectF(0, top, self.width(), height))

        p.setPen(self._line_pen())
        for hour in range(25):
            y = round(self._y_of(hour * 60.0)) + 0.5
            if top - 1 <= y <= top + height + 1:
                p.drawLine(QPointF(self._gutter_w - 6, y), QPointF(right, y))
        for index in range(len(self._days)):
            x = round(self._col_x(index)[0]) + 0.5
            p.drawLine(QPointF(x, top), QPointF(x, top + height))

        p.setFont(self._font_small)
        p.setPen(QColor(GUTTER_TEXT))
        for hour in range(24):
            y = self._y_of(hour * 60.0)
            if top - self._small_h <= y <= top + height + self._small_h:
                p.drawText(QRectF(0, y - self._small_h / 2, self._gutter_w - 10, self._small_h), ALIGN_RIGHT, f"{hour}:00")

        p.setFont(self._font_event)
        for day in self._timed:
            for seg in day:
                rect = self._block_rect(seg)
                if rect.bottom() >= top and rect.top() <= top + height:
                    self._paint_block(p, seg, rect)

        if self._today >= 0:
            y = round(self._y_of(self._now_minutes))
            x0, x1 = self._col_x(self._today)
            p.setPen(QPen(QColor(NOW_LINE), 2))
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(NOW_LINE))
            p.drawEllipse(QPointF(x0 + 1, y), 5.0, 5.0)
        p.restore()

    def _paint_item(self, p: QPainter, seg: _Seg, rect: QRectF, two_lines: bool) -> None:
        fill, outline, text = _look(seg)
        half = outline.widthF() / 2
        p.setPen(outline)
        p.setBrush(fill)
        p.drawRoundedRect(rect.adjusted(half, half, -half, -half), 4, 4)
        if self._is_selected(seg) or seg is self._hover:
            ring = QColor(SELECTED_OUTLINE) if self._is_selected(seg) else seg.color.lighter(150)
            p.setPen(QPen(ring, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

        inner = rect.adjusted(6, 0, -4, 0)
        if seg.lanes:
            inner.setRight(min(inner.right(), min(self._block_rect(o).left() for o in seg.lanes) - LANE_TEXT_GAP))
        if inner.width() < 6:
            return
        p.save()
        p.setClipRect(rect.adjusted(1, 1, -1, -1))
        p.setPen(text)
        plain = p.font()
        struck = QFont(plain)
        struck.setStrikeOut(seg.event.masked)
        fm = p.fontMetrics()
        title = fm.elidedText(seg.title, Qt.TextElideMode.ElideRight, int(inner.width()))
        p.setFont(struck)
        if two_lines:
            p.drawText(QRectF(inner.left(), rect.top() + 3, inner.width(), fm.height()), ALIGN_LEFT, title)
            p.setFont(plain)
            when = fm.elidedText(seg.time_text, Qt.TextElideMode.ElideRight, int(inner.width()))
            p.drawText(QRectF(inner.left(), rect.top() + 3 + fm.height(), inner.width(), fm.height()), ALIGN_LEFT, when)
        else:
            p.drawText(QRectF(inner.left(), rect.top(), inner.width(), rect.height()), ALIGN_LEFT, title)
        p.restore()

    def _paint_block(self, p: QPainter, seg: _Seg, rect: QRectF) -> None:
        self._paint_item(p, seg, rect, rect.height() >= 2 * self._event_h + 2)

    def _paint_header(self, p: QPainter) -> None:
        p.save()
        p.setClipRect(QRectF(0, 0, self.width(), self._header_h))
        p.setFont(self._font_caption)
        p.setPen(QColor(GUTTER_TEXT))
        fm = p.fontMetrics()
        caption = fm.elidedText(self._gmt, Qt.TextElideMode.ElideRight, self._gutter_w - 10)
        p.drawText(QRectF(0, self._header_h - fm.height() - 6, self._gutter_w - 10, fm.height()), ALIGN_RIGHT, caption)

        for index, day in enumerate(self._days):
            x0, x1 = self._col_x(index)
            is_today = index == self._today
            p.setFont(self._font_small)
            p.setPen(QColor(TODAY_FILL if is_today else GUTTER_TEXT))
            p.drawText(QRectF(x0, 6, x1 - x0, self._small_h), ALIGN_CENTER, WEEKDAY_LABELS[day.weekday()])
            center = QPointF((x0 + x1) / 2, 6 + self._small_h + 2 + self._circle_d / 2)
            if is_today or index == self._hover_header:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(TODAY_FILL if is_today else HOVER_CIRCLE))
                p.drawEllipse(center, self._circle_d / 2, self._circle_d / 2)
            p.setFont(self._font_number)
            p.setPen(QColor(TODAY_TEXT if is_today else DATE_TEXT))
            p.drawText(QRectF(center.x() - self._circle_d, center.y() - self._circle_d / 2, self._circle_d * 2, self._circle_d), ALIGN_CENTER, str(day.day))
        p.restore()

    def _paint_allday(self, p: QPainter) -> None:
        top, bottom = self._header_h, self._grid_top()
        p.save()
        p.setClipRect(QRectF(0, top, self.width(), bottom - top))
        p.setPen(self._line_pen())
        for index in range(len(self._days)):
            x = round(self._col_x(index)[0]) + 0.5
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
        y = bottom - 0.5
        p.drawLine(QPointF(self._gutter_w - 6, y), QPointF(self._gutter_w + self._content_w(), y))

        p.setFont(self._font_event)
        for day in self._allday:
            for seg in day:
                if seg.visible:
                    self._paint_item(p, seg, self._chip_rect(seg), False)
        for index in range(len(self._days)):
            hidden = self._overflow(index)
            if hidden:
                rect = self._more_rect(index)
                selected = any(self._is_selected(s) for s in self._allday[index] if not s.visible)
                if selected:
                    p.setPen(QPen(QColor(SELECTED_OUTLINE), 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
                p.setPen(QColor(GUTTER_TEXT))
                p.drawText(rect.adjusted(6, 0, -4, 0), ALIGN_LEFT, f"+{hidden}")
        p.restore()

    def _paint_message(self, p: QPainter) -> None:
        area = QRectF(self._gutter_w, self._grid_top(), self._content_w(), self._viewport_h())
        if area.width() < 40 or area.height() < 20:
            return
        p.save()
        p.setClipRect(area)
        p.setFont(self.font())
        text_area = area.adjusted(24, 12, -24, -12)
        bounds = p.boundingRect(text_area, ALIGN_WRAP_CENTER, self._message)
        panel = bounds.adjusted(-18, -10, 18, 10)
        p.setPen(QPen(QColor(GRID_LINE), 1))
        panel_fill = QColor(BG)
        panel_fill.setAlpha(225)
        p.setBrush(panel_fill)
        p.drawRoundedRect(panel, 8, 8)
        p.setPen(QColor(GUTTER_TEXT))
        p.drawText(text_area, ALIGN_WRAP_CENTER, self._message)
        p.restore()
