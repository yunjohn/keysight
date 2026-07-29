from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from keysight_scope_app.ui.waveform_theme import INSTRUMENT_DARK_THEME, WaveformTheme


class WaveformNavigator(QWidget):
    rangeChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None, *, theme: WaveformTheme = INSTRUMENT_DARK_THEME) -> None:
        super().__init__(parent)
        self.theme = theme
        self.full_range = (0.0, 1.0)
        self.view_range = (0.0, 1.0)
        self._drag_mode: str | None = None
        self._drag_origin_x = 0.0
        self._drag_origin_range = self.view_range
        self.setMinimumHeight(28)
        self.setMaximumHeight(34)
        self.setCursor(Qt.OpenHandCursor)

    def set_ranges(
        self,
        full_range: tuple[float, float],
        view_range: tuple[float, float],
    ) -> None:
        if full_range[1] <= full_range[0]:
            return
        self.full_range = full_range
        self.view_range = self._clamp_range(view_range)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = QRectF(6, self.height() / 2 - 4, max(self.width() - 12, 1), 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.theme.grid_minor))
        painter.drawRoundedRect(track, 4, 4)
        left = self._value_to_x(self.view_range[0])
        right = self._value_to_x(self.view_range[1])
        selection = QRectF(left, 4, max(right - left, 3), max(self.height() - 8, 1))
        fill = QColor(self.theme.border)
        fill.setAlpha(155)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(self.theme.channels["CHANnel3"].color), 1))
        painter.drawRoundedRect(selection, 4, 4)
        painter.setBrush(QColor(self.theme.axis_text))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(left - 3, 2, 6, self.height() - 4), 3, 3)
        painter.drawRoundedRect(QRectF(right - 3, 2, 6, self.height() - 4), 3, 3)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            return
        left = self._value_to_x(self.view_range[0])
        right = self._value_to_x(self.view_range[1])
        x_value = event.position().x()
        if abs(x_value - left) <= 8:
            self._drag_mode = "left"
            self.setCursor(Qt.SizeHorCursor)
        elif abs(x_value - right) <= 8:
            self._drag_mode = "right"
            self.setCursor(Qt.SizeHorCursor)
        elif left <= x_value <= right:
            self._drag_mode = "window"
            self.setCursor(Qt.ClosedHandCursor)
        else:
            center = self._x_to_value(x_value)
            span = self.view_range[1] - self.view_range[0]
            self.view_range = self._clamp_range((center - span / 2, center + span / 2))
            self.rangeChanged.emit(*self.view_range)
            self.update()
            return
        self._drag_origin_x = x_value
        self._drag_origin_range = self.view_range

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_mode is None:
            return
        delta = self._x_to_value(event.position().x()) - self._x_to_value(self._drag_origin_x)
        left, right = self._drag_origin_range
        minimum_span = max((self.full_range[1] - self.full_range[0]) * 1e-6, 1e-15)
        if self._drag_mode == "left":
            candidate = (min(left + delta, right - minimum_span), right)
        elif self._drag_mode == "right":
            candidate = (left, max(right + delta, left + minimum_span))
        else:
            candidate = (left + delta, right + delta)
        self.view_range = self._clamp_range(candidate)
        self.rangeChanged.emit(*self.view_range)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._drag_mode = None
        self.setCursor(Qt.OpenHandCursor)

    def _value_to_x(self, value: float) -> float:
        left, right = self.full_range
        ratio = (value - left) / (right - left)
        return 6 + min(max(ratio, 0.0), 1.0) * max(self.width() - 12, 1)

    def _x_to_value(self, x_value: float) -> float:
        ratio = (x_value - 6) / max(self.width() - 12, 1)
        left, right = self.full_range
        return left + min(max(ratio, 0.0), 1.0) * (right - left)

    def _clamp_range(self, value_range: tuple[float, float]) -> tuple[float, float]:
        full_left, full_right = self.full_range
        left, right = value_range
        span = min(max(right - left, 0.0), full_right - full_left)
        left = max(min(left, full_right - span), full_left)
        return left, left + span
